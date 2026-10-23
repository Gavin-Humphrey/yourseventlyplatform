from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from django.core.mail import send_mail
from django.conf import settings
from events.models import Event

class Command(BaseCommand):
    help = 'Automatically sends pre-event reminders and post-event feedback/certificate drips.'

    def handle(self, *args, **options):
        now = timezone.now()
        
        # 1. PRE-EVENT REMINDERS (Triggered ~24 hours before event starts)
        reminder_window_start = now + timedelta(hours=23)
        reminder_window_end = now + timedelta(hours=25)
        
        upcoming_events = Event.objects.filter(
            start_date__gte=reminder_window_start,
            start_date__lte=reminder_window_end,
            pre_reminder_sent=False
        )
        
        for event in upcoming_events:
            attending_rsvps = event.rsvps.filter(status='ATTENDING', email__isnull=False)
            for rsvp in attending_rsvps:
                public_url = f"http://127.0.0.1:8000/rsvp/{event.slug}/" # Update domain for production
                subject = f"Reminder: {event.title} is coming up tomorrow!"
                message = f"Hi {rsvp.first_name},\n\nThis is a quick reminder that {event.title} is happening tomorrow! We can't wait to see you there.\n\nView event details and your pass here: {public_url}\n\nBest,\nThe Team"
                
                try:
                    send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [rsvp.email], fail_silently=False)
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f"Failed pre-reminder to {rsvp.email}: {e}"))
            
            event.pre_reminder_sent = True
            event.save()
            self.stdout.write(self.style.SUCCESS(f"Pre-reminders sent for: {event.title}"))

        # 2. POST-EVENT DRIPS (Triggered ~24 hours after event ends)
        post_window_start = now - timedelta(hours=25)
        post_window_end = now - timedelta(hours=23)
        
        concluded_events = Event.objects.filter(
            end_date__gte=post_window_start,
            end_date__lte=post_window_end,
            post_event_email_sent=False
        )
        
        for event in concluded_events:
            attending_rsvps = event.rsvps.filter(status='ATTENDING', email__isnull=False)
            for rsvp in attending_rsvps:
                public_url = f"http://127.0.0.1:8000/rsvp/{event.slug}/"
                subject = f"Thank you for attending {event.title}! Share your thoughts & get your certificate"
                message = (
                    f"Hi {rsvp.first_name},\n\n"
                    f"Thank you for joining us at {event.title}! We hope you had a wonderful experience.\n\n"
                    f"You can now share your feedback, view guest wishes, and download your certificate of attendance here:\n"
                    f"{public_url}\n\n"
                    f"Thank you for being part of our community!\n\n"
                    f"Best regards,\nThe Team"
                )
                
                try:
                    send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [rsvp.email], fail_silently=False)
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f"Failed post-drip to {rsvp.email}: {e}"))
            
            event.post_event_email_sent = True
            event.save()
            self.stdout.write(self.style.SUCCESS(f"Post-event drips sent for: {event.title}"))