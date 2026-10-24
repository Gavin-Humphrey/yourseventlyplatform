from django.conf import settings
from django.core.mail import send_mail
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.urls import reverse
from .models import InAppNotification, EventFollowerAnnouncement, FollowNotificationPreference, OrganizerFollow, TicketOrder
import base64
import logging
import uuid
from decimal import Decimal, ROUND_HALF_UP
from email.mime.image import MIMEImage
import stripe
from django.db import transaction



logger = logging.getLogger(__name__)


def send_attendance_confirmation(attendee, request=None):
    """Send an RSVP confirmation with payment details and ticket QR codes."""

    event = attendee.event

    subject = f"Confirmation: You're attending {event.title}!"

    theme = getattr(event, "theme", None)
    style_card_bg = (
        getattr(theme, "card_bg", "#ffffff")
        if theme else "#ffffff"
    )
    style_body = (
        getattr(theme, "body_color", "#334155")
        if theme else "#334155"
    )
    style_muted = (
        getattr(theme, "muted_color", "#64748b")
        if theme else "#64748b"
    )

    context = {
        "rsvp": attendee,
        "event": event,
        "style_card_bg": style_card_bg,
        "style_body": style_body,
        "style_muted": style_muted,
        "pass_absolute_url": "",
        "ticket_info": None,
        "payment_info": None,
        "email_tickets": [],
    }

    try:
        paid_order = None

        if attendee.is_paid:
            paid_order = (
                TicketOrder.objects
                .filter(
                    event_attendee=attendee,
                    status=TicketOrder.STATUS_PAID,
                )
                .select_related("ticket_tier")
                .first()
            )

        if paid_order:
            quantity = paid_order.ticket_quantity
            tier = paid_order.ticket_tier

            unit_price = Decimal(str(tier.price))
            subtotal = (
                unit_price * quantity
            ).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )

            ticket_info = {
                "tier_name": tier.name,
                "quantity": quantity,
                "unit_price": f"${unit_price:,.2f}",
                "subtotal": f"${subtotal:,.2f}",
            }

            payment_info = {
                "currency": "USD",
                "platform_fee": None,
                "total_paid": None,
                "payment_reference": paid_order.stripe_session_id or "",
                "payment_status": "Paid",
            }

            # Retrieve Stripe's recorded Checkout Session totals rather
            # than assuming the ticket subtotal equals the amount charged.
            if paid_order.stripe_session_id:
                try:
                    stripe.api_key = settings.STRIPE_SECRET_KEY

                    session = stripe.checkout.Session.retrieve(
                        paid_order.stripe_session_id
                    )

                    currency = (
                        session.get("currency") or "usd"
                    ).upper()

                    amount_total = session.get("amount_total")
                    payment_intent = session.get("payment_intent")
                    metadata = session.get("metadata") or {}

                    payment_info["currency"] = currency

                    if amount_total is not None:
                        actual_total = (
                            Decimal(str(amount_total)) / Decimal("100")
                        ).quantize(
                            Decimal("0.01"),
                            rounding=ROUND_HALF_UP,
                        )
                        payment_info["total_paid"] = (
                            f"{currency} {actual_total:,.2f}"
                        )

                    # The fee is already recorded in Stripe metadata
                    # by create_checkout_session().
                    fee_value = metadata.get("platform_fee_amount")

                    if fee_value is not None:
                        fee = Decimal(str(fee_value)).quantize(
                            Decimal("0.01"),
                            rounding=ROUND_HALF_UP,
                        )
                        payment_info["platform_fee"] = (
                            f"{currency} {fee:,.2f}"
                        )

                    if isinstance(payment_intent, str):
                        payment_info["payment_reference"] = (
                            payment_intent
                        )
                    elif payment_intent:
                        payment_info["payment_reference"] = (
                            payment_intent.get("id")
                            or paid_order.stripe_session_id
                        )

                except Exception:
                    # Preserve the confirmation email even if Stripe's
                    # session details cannot be retrieved.
                    logger.exception(
                        "Could not retrieve Stripe receipt details "
                        "for pending order %s",
                        paid_order.pk,
                    )

            context["ticket_info"] = ticket_info
            context["payment_info"] = payment_info

            # Include each existing individual ticket and its own QR.
            if event.enable_qr_checkins:
                for ticket in attendee.individual_tickets.all():
                    data_uri = ticket.get_qr_code_data_uri()
                    qr_cid = None

                    if data_uri and data_uri.startswith(
                        "data:image/png;base64,"
                    ):
                        qr_cid = (
                            f"ticket-{ticket.pk}-"
                            f"{uuid.uuid4().hex}@yoursevently"
                        )

                    context["email_tickets"].append({
                        "number": ticket.ticket_number,
                        "token": str(ticket.checkin_token),
                        "qr_cid": qr_cid,
                        "qr_data": (
                            data_uri.split(",", 1)[1]
                            if qr_cid else None
                        ),
                    })

        html_message = render_to_string(
            "events/emails/rsvp_confirmation.html",
            context,
        )
        plain_message = strip_tags(html_message)

        email = EmailMultiAlternatives(
            subject=subject,
            body=plain_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[attendee.email],
        )
        email.attach_alternative(html_message, "text/html")

        # Attach QR codes inline so email clients can display them using
        # cid: references, rather than relying on base64 HTML images.
        for ticket in context["email_tickets"]:
            if not ticket["qr_cid"] or not ticket["qr_data"]:
                continue

            image_bytes = base64.b64decode(ticket["qr_data"])
            image_part = MIMEImage(image_bytes, _subtype="png")
            image_part.add_header(
                "Content-ID",
                f'<{ticket["qr_cid"]}>',
            )
            image_part.add_header(
                "Content-Disposition",
                "inline",
                filename=f'ticket-{ticket["number"]}-qr.png',
            )
            email.attach(image_part)

        sent_count = email.send(fail_silently=False)

        logger.info(
            "RSVP CONFIRMATION EMAIL: recipient=%s, sent_count=%s, "
            "paid=%s, order_id=%s, ticket_qr_count=%s, backend=%s",
            attendee.email,
            sent_count,
            bool(paid_order),
            paid_order.pk if paid_order else None,
            sum(
                1 for ticket in context["email_tickets"]
                if ticket["qr_cid"]
            ),
            settings.EMAIL_BACKEND,
        )

        if sent_count == 1:
            attendee.confirmation_sent = True
            attendee.save(update_fields=["confirmation_sent"])
            return True

        return False

    except Exception:
        logger.exception(
            "RSVP confirmation email failed for recipient %s",
            attendee.email,
        )
        return False

def send_visitor_follow_verification(verification, request):
    organizer = verification.organizer

    subject = "Verify your email to follow an organizer on YoursEvently"

    verify_relative_url = reverse(
        "events:verify_visitor_follow",
        kwargs={"token": verification.token},
    )
    verify_url = request.build_absolute_uri(verify_relative_url)

    message = (
        f"Someone requested to follow "
        f"{organizer.get_full_name() or organizer.username} "
        f"on YoursEvently.\n\n"
        f"Verify your email using this link:\n{verify_url}\n\n"
        "This link expires in 24 hours. "
        "If you did not make this request, you can ignore this email."
    )

    send_mail(
        subject=subject,
        message=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[verification.email],
        fail_silently=False,
    )



def notify_organizer_of_new_follower(follower, organizer):
    """Notify the organizer only when a new follow is created."""
    follower_name = (
        follower.get_full_name().strip()
        or follower.username
        or "A new visitor"
    )

    InAppNotification.objects.create(
        recipient=organizer,
        actor=follower,
        event=None,
        title="New follower",
        message=f"{follower_name} is now following you.",
        url="/",
    )

def notify_followers_of_new_event(event):
    """Notify an organizer's followers about a newly created event."""

    followers = OrganizerFollow.objects.filter(
        organizer=event.host
    ).select_related('follower')

    event_path = reverse(
        'events:event_detail',
        kwargs={'slug': event.slug},
    )

    for follow in followers:
        follower = follow.follower

        # Do not notify the organizer about their own event.
        if follower.pk == event.host_id:
            continue

        preferences = FollowNotificationPreference.objects.filter(
            user=follower
        ).first()

        # Model defaults apply when no preference record exists.
        in_app_enabled = (
            preferences.in_app_new_events
            if preferences else True
        )
        email_enabled = (
            preferences.email_new_events
            if preferences else True
        )

        if not in_app_enabled and not email_enabled:
            continue

        with transaction.atomic():
            announcement, _ = (
                EventFollowerAnnouncement.objects.get_or_create(
                    event=event,
                    recipient=follower,
                )
            )

            announcement = (
                EventFollowerAnnouncement.objects
                .select_for_update()
                .get(pk=announcement.pk)
            )

            if in_app_enabled and not announcement.in_app_sent:
                InAppNotification.objects.get_or_create(
                    recipient=follower,
                    actor=event.host,
                    event=event,
                    title='New event from an organizer you follow',
                    defaults={
                        'message': (
                            f'{event.host.get_full_name() or event.host.username} '
                            f'published a new event: {event.title}'
                        ),
                        'url': event_path,
                    },
                )

                announcement.in_app_sent = True

            if (
                email_enabled
                and not announcement.email_sent
                and follower.email
            ):
                subject = f'New event: {event.title}'
                message = (
                    f'{event.host.get_full_name() or event.host.username} '
                    f'has published a new event: {event.title}.\n\n'
                    f'Date: {event.event_date:%B %d, %Y at %I:%M %p}\n'
                    f'Location: {event.location_name}\n\n'
                    f'View the event: {event_path}\n\n'
                    'You received this email because you follow this organizer. '
                    'You can update your notification preferences in YoursEvently.'
                )

                sent = send_mail(
                    subject,
                    message,
                    settings.DEFAULT_FROM_EMAIL,
                    [follower.email],
                    fail_silently=False,
                )

                if sent:
                    announcement.email_sent = True

            announcement.save(
                update_fields=['in_app_sent', 'email_sent']
            )

def country_flag_emoji(country_code):
    """Generate a flag emoji from a two-letter ISO country code."""
    code = str(country_code).upper()

    if len(code) != 2 or not code.isascii() or not code.isalpha():
        return ''

    return ''.join(chr(127397 + ord(char)) for char in code)
