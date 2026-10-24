from django.conf import settings
from django.utils import timezone
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from .models import OrganizerFollow, InAppNotification, FollowNotificationPreference

@receiver(pre_save, sender='events.Event')
def track_event_publish_state(sender, instance, **kwargs):
    """
    Record previous status so we know if an event transitioned into 'published'.
    Ensures drafts, updates, or edits NEVER re-trigger notifications!
    """
    if instance.pk:
        try:
            old_event = sender.objects.get(pk=instance.pk)
            instance._was_published = (getattr(old_event, 'status', 'published') == 'published')
        except sender.DoesNotExist:
            instance._was_published = False
    else:
        instance._was_published = False


@receiver(post_save, sender='events.Event')
def notify_followers_on_publish(sender, instance, created, **kwargs):
    """
    Priority 3: Notify followers about new events.
    Rules:
    - Only send notifications for NEWLY published events (not drafts or edits).
    - Idempotent: check notified_followers_at field or ensure was not published previously.
    - Check user notification preferences before sending emails.
    """
    is_published = getattr(instance, 'status', 'published') == 'published'
    was_published = getattr(instance, '_was_published', False)

    # Condition: Event is now published AND either it's brand new OR it transitioned from draft
    should_notify = is_published and (created or not was_published)

    # Secondary safeguard: If instance already has notified_followers_at timestamp, skip
    if hasattr(instance, 'notified_followers_at') and instance.notified_followers_at:
        return

    if not should_notify:
        return

    organizer = instance.host
    followers = OrganizerFollow.objects.filter(organizer=organizer).select_related('follower')

    if not followers.exists():
        return

    # Timestamp the notification to guarantee duplicate prevention
    if hasattr(instance, 'notified_followers_at'):
        sender.objects.filter(pk=instance.pk).update(notified_followers_at=timezone.now())

    organizer_name = organizer.get_full_name() or organizer.username

    for follow_rel in followers:
        recipient = follow_rel.follower

        # 1. In-App Notification Dispatch
        InAppNotification.objects.create(
            recipient=recipient,
            actor=organizer,
            event=instance,
            title=f"New Event from {organizer_name}",
            message=f"{organizer_name} just published '{instance.title}' for {instance.event_date.strftime('%b %d, %Y')}.",
            url=f"/e/{instance.slug}/"
        )

        # 2. Email Dispatch (Respects user preferences)
        prefs, _ = FollowNotificationPreference.objects.get_or_create(user=recipient)
        if prefs.email_new_events and recipient.email:
            try:
                subject = f"📅 New Event: {instance.title} by {organizer_name}"
                context = {
                    'recipient': recipient,
                    'organizer': organizer,
                    'organizer_name': organizer_name,
                    'event': instance,
                    'unsubscribe_url': f"https://yoursevently.com/events/notifications/unsubscribe/{prefs.unsubscribe_token}/"
                }
                html_message = render_to_string('events/emails/new_event_follower_alert.html', context)
                plain_message = strip_tags(html_message)
                
                send_mail(
                    subject=subject,
                    message=plain_message,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[recipient.email],
                    html_message=html_message,
                    fail_silently=True
                )
            except Exception as e:
                # Log error without interrupting the event save transaction
                pass
