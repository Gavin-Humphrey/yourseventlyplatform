import os
from django.shortcuts import render, get_object_or_404, redirect
from django.conf import settings
from django.urls import reverse
import stripe
import json
import logging
from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.models import User
from .models import (
    Event,
    GalleryImage,
    PendingUpgrade,
    OrganizerProfile,
    EventAttendee,
    PlatformFee,
    TicketOrder,
    IndividualTicket,
    TicketTier,
)
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils.dateparse import parse_datetime
from decimal import Decimal
from .utils import notify_followers_of_new_event


stripe.api_key = settings.STRIPE_SECRET_KEY

logger = logging.getLogger(__name__)

def prepare_pending_data(form):
    data = form.cleaned_data.copy()

    data.pop('hero_image', None)
    data.pop('presentation_file', None)

    if data.get('event_date'):
        data['event_date'] = data['event_date'].isoformat()

    return data


def store_pending_files(request, form, formset, token, is_edit=False):
    files = {}

    if 'hero_image' in request.FILES:
        f = request.FILES['hero_image']
        path = default_storage.save(
            f'temp_uploads/{token}_{f.name}',
            ContentFile(f.read())
        )
        files['hero_image'] = path

    if 'presentation_file' in request.FILES:
        f = request.FILES['presentation_file']
        path = default_storage.save(
            f'temp_uploads/{token}_{f.name}',
            ContentFile(f.read())
        )
        files['presentation_file'] = path

    gallery = []

    for index, gallery_form in enumerate(formset.forms):
        cleaned = gallery_form.cleaned_data

        if cleaned.get('DELETE'):
            if is_edit and gallery_form.instance.pk:
                gallery.append({
                    'id': gallery_form.instance.pk,
                    'delete': True,
                })
            continue

        image = cleaned.get('image')

        item = {
            'description': cleaned.get('description', ''),
            'order': cleaned.get('order', 0),
        }

        if is_edit and gallery_form.instance.pk:
            item['id'] = gallery_form.instance.pk

        if image:
            path = default_storage.save(
                f'temp_uploads/{token}_gallery_{index}_{image.name}',
                ContentFile(image.read())
            )
            item['path'] = path

        gallery.append(item)

    files['gallery_images'] = gallery

    return files


def _restore_event_files(event, files, is_edit=False):
    hero_path = files.get('hero_image')

    if hero_path and default_storage.exists(hero_path):
        with default_storage.open(hero_path, 'rb') as f:
            permanent_path = default_storage.save(
                f'hero_images/{event.id}_{os.path.basename(hero_path)}',
                ContentFile(f.read())
            )

        theme_settings = event.theme_settings or {}
        theme_settings['hero_image_url'] = default_storage.url(permanent_path)
        event.theme_settings = theme_settings
        default_storage.delete(hero_path)

    presentation_path = files.get('presentation_file')

    if presentation_path and default_storage.exists(presentation_path):
        with default_storage.open(presentation_path, 'rb') as f:
            event.presentation_file.save(
                os.path.basename(presentation_path),
                ContentFile(f.read()),
                save=False
            )

        default_storage.delete(presentation_path)

    for gallery in files.get('gallery_images', []):
        gallery_id = gallery.get('id')

        if gallery.get('delete') and gallery_id:
            GalleryImage.objects.filter(
                pk=gallery_id,
                event=event
            ).delete()
            continue

        path = gallery.get('path')

        if path and default_storage.exists(path):
            with default_storage.open(path, 'rb') as f:
                image = ContentFile(
                    f.read(),
                    name=os.path.basename(path)
                )

            if gallery_id:
                gallery_image = GalleryImage.objects.filter(
                    pk=gallery_id,
                    event=event
                ).first()

                if gallery_image:
                    gallery_image.image.save(
                        os.path.basename(path),
                        image,
                        save=False
                    )
                    gallery_image.description = gallery.get('description', '')
                    gallery_image.order = gallery.get('order', 0)
                    gallery_image.save()

            else:
                GalleryImage.objects.create(
                    event=event,
                    image=image,
                    description=gallery.get('description', ''),
                    order=gallery.get('order', 0),
                )

            default_storage.delete(path)

        elif gallery_id:
            GalleryImage.objects.filter(
                pk=gallery_id,
                event=event
            ).update(
                description=gallery.get('description', ''),
                order=gallery.get('order', 0),
            )

    event.save()


def create_or_get_stripe_customer(user):
    profile, _ = OrganizerProfile.objects.get_or_create(user=user)

    if profile.stripe_customer_id:
        return stripe.Customer.modify(
            profile.stripe_customer_id,
            email=user.email,
            name=user.get_full_name() or user.username,
        )

    customer = stripe.Customer.create(
        email=user.email,
        name=user.get_full_name() or user.username,
        metadata={
            'user_id': str(user.pk),
        },
    )

    profile.stripe_customer_id = customer.id
    profile.save(update_fields=['stripe_customer_id'])

    return customer


def create_upgrade_checkout(request, pending):
    stripe.api_key = settings.STRIPE_SECRET_KEY

    customer = create_or_get_stripe_customer(request.user)

    if pending.plan == 'unlimited':
        mode = 'subscription'
        price_id = settings.STRIPE_PRO_UNLIMITED_PRICE_ID
    else:
        mode = 'payment'
        price_id = settings.STRIPE_SINGLE_PRO_PRICE_ID

    success_url = request.build_absolute_uri(
        reverse('events:payment_success')
    )

    cancel_url = request.build_absolute_uri(
        reverse('events:upgrade_options', args=[pending.pk])
    )

    session = stripe.checkout.Session.create(
        mode=mode,
        line_items=[
            {
                'price': price_id,
                'quantity': 1,
            }
        ],
        success_url=f'{success_url}?session_id={{CHECKOUT_SESSION_ID}}',
        cancel_url=cancel_url,
        customer=customer.id,
        metadata={
            'pending_upgrade_id': str(pending.pk),
            'user_id': str(request.user.pk),
            'plan': pending.plan,
        },
    )

    pending.stripe_session_id = session.id
    pending.save(
        update_fields=[
            'stripe_session_id',
            'updated_at',
        ]
    )

    return session.url


def finalize_upgrade(pending_id, session):
    pending = PendingUpgrade.objects.filter(
        pk=pending_id,
        status='pending'
    ).first()

    if not pending:
        return

    if pending.stripe_session_id != session.id:
        return

    if pending.plan == 'single' and session.payment_status != 'paid':
        return

    with transaction.atomic():
        if pending.plan == 'unlimited':
            customer_id = session.customer
            subscription_id = session.subscription

            if customer_id and subscription_id:
                profile, _ = OrganizerProfile.objects.get_or_create(
                    user=pending.user
                )

                profile.stripe_customer_id = customer_id
                profile.stripe_subscription_id = subscription_id
                profile.subscription_status = 'active'

                profile.save(
                    update_fields=[
                        'stripe_customer_id',
                        'stripe_subscription_id',
                        'subscription_status',
                    ]
                )

        if pending.action == 'create':
            return finalize_create_upgrade(pending)

        return finalize_edit_upgrade(pending)


def finalize_create_upgrade(pending):
    data = pending.data
    files = data.get('files', {})

    with transaction.atomic():
        event = Event(
            host=pending.user,
            is_pro_upgraded=True,
            title=data.get('title', ''),
            event_date=parse_datetime(data.get('event_date')),
            location_name=data.get('location_name', ''),
            address=data.get('address', ''),
            max_capacity=data.get('max_capacity'),
            is_ticketed=data.get('is_ticketed', False),
            show_about_section=data.get('show_about_section', False),
            about_title=data.get('about_title', 'About the Host'),
            about_text=data.get('about_text', ''),
            allow_plus_ones=data.get('allow_plus_ones', False),
            max_plus_ones_per_guest=data.get('max_plus_ones_per_guest', 0),
            allow_guest_messages=data.get('allow_guest_messages', False),
            announcement_title=data.get('announcement_title', 'Announcement'),
            announcement_text=data.get('announcement_text', ''),
            show_announcement=data.get('show_announcement', False),
            enable_qr_checkins=data.get('enable_qr_checkins', False),
            enable_certificates=data.get('enable_certificates', False),
            resource_link=data.get('resource_link', ''),
        )

        event.save()

        _restore_event_files(
            event,
            files,
            is_edit=False
        )

        pending.event = event
        pending.status = 'completed'

        pending.save(
            update_fields=[
                'event',
                'status',
                'updated_at',
            ]
        )

        notify_followers_of_new_event(event)

        return event


def finalize_edit_upgrade(pending):
    event = pending.event

    if not event:
        pending.status = 'failed'
        pending.save(
            update_fields=[
                'status',
                'updated_at',
            ]
        )
        return None

    data = pending.data
    files = data.get('files', {})

    with transaction.atomic():
        event.title = data.get(
            'title',
            event.title
        )

        if data.get('event_date'):
            event.event_date = parse_datetime(
                data['event_date']
            )

        event.location_name = data.get(
            'location_name',
            event.location_name
        )

        event.address = data.get(
            'address',
            event.address
        )

        event.max_capacity = data.get(
            'max_capacity',
            event.max_capacity
        )

        event.is_ticketed = data.get(
            'is_ticketed',
            event.is_ticketed
        )

        event.show_about_section = data.get(
            'show_about_section',
            event.show_about_section
        )

        event.about_title = data.get(
            'about_title',
            event.about_title
        )

        event.about_text = data.get(
            'about_text',
            event.about_text
        )

        event.allow_plus_ones = data.get(
            'allow_plus_ones',
            event.allow_plus_ones
        )

        event.max_plus_ones_per_guest = data.get(
            'max_plus_ones_per_guest',
            event.max_plus_ones_per_guest
        )

        event.allow_guest_messages = data.get(
            'allow_guest_messages',
            event.allow_guest_messages
        )

        event.announcement_title = data.get(
            'announcement_title',
            event.announcement_title
        )

        event.announcement_text = data.get(
            'announcement_text',
            event.announcement_text
        )

        event.show_announcement = data.get(
            'show_announcement',
            event.show_announcement
        )

        event.enable_qr_checkins = data.get(
            'enable_qr_checkins',
            event.enable_qr_checkins
        )

        event.enable_certificates = data.get(
            'enable_certificates',
            event.enable_certificates
        )

        event.resource_link = data.get(
            'resource_link',
            event.resource_link
        )

        event.is_pro_upgraded = True
        event.save()

        _restore_event_files(
            event,
            files,
            is_edit=True
        )

        pending.status = 'completed'

        pending.save(
            update_fields=[
                'status',
                'updated_at',
            ]
        )

    return event

@csrf_exempt
def stripe_webhook(request):
    payload = request.body
    sig_header = request.META.get(
        'HTTP_STRIPE_SIGNATURE'
    )

    try:
        stripe_event = stripe.Webhook.construct_event(
            payload,
            sig_header,
            settings.STRIPE_WEBHOOK_SECRET
        )
    except (
        ValueError,
        stripe.error.SignatureVerificationError
    ):
        return HttpResponse(status=400)

    if stripe_event['type'] == 'checkout.session.completed':
        session = stripe_event['data']['object']

        metadata = (
            session.metadata.to_dict()
            if session.metadata
            else {}
        )

        # Finalize a paid ticket order created before checkout.
        ticket_order_id = metadata.get('pending_order_id')

        if ticket_order_id:
            try:
                finalize_ticket_order(session)

            except TicketOrder.DoesNotExist:
                print(
                    f"Ticket order {ticket_order_id} "
                    "was not found."
                )
                return HttpResponse(status=500)

            except Exception as exc:
                print(
                    "Ticket order finalization failed: "
                    f"{exc}"
                )
                return HttpResponse(status=500)

        # Legacy attendee payment handling.
        attendee_id = metadata.get('rsvp_id')

        if attendee_id:
            try:
                attendee = EventAttendee.objects.get(id=attendee_id)

                if not attendee.is_paid:
                    attendee.is_paid = True
                    attendee.save()

                    if attendee.ticket_tier:
                        attendee.ticket_tier.sold_count += 1
                        attendee.ticket_tier.save()

            except EventAttendee.DoesNotExist:
                pass

        # Pending upgrade handling.
        pending_id = metadata.get(
            'pending_upgrade_id'
        )

        if pending_id:
            finalize_upgrade(
                pending_id,
                session
            )

        # Dashboard Unlimited Pro subscription handling.
        dashboard_unlimited_pro = metadata.get(
            'dashboard_unlimited_pro'
        )

        if dashboard_unlimited_pro == 'true':
            user_id = metadata.get('user_id')

            if user_id:
                try:
                    user = User.objects.get(pk=user_id)

                    customer_id = session.customer
                    subscription_id = session.subscription

                    if customer_id and subscription_id:
                        profile, _ = OrganizerProfile.objects.get_or_create(
                            user=user
                        )

                        profile.stripe_customer_id = customer_id
                        profile.stripe_subscription_id = subscription_id
                        profile.subscription_status = 'active'

                        profile.save(
                            update_fields=[
                                'stripe_customer_id',
                                'stripe_subscription_id',
                                'subscription_status',
                            ]
                        )

                except User.DoesNotExist:
                    pass

    return HttpResponse(status=200)

############PLATFORM PERCENTAGE##############
def get_active_ticket_fee_percent():
    fee = (
        PlatformFee.objects
        .filter(
            applies_to='TICKET',
            active=True,
        )
        .order_by('-id')
        .first()
    )

    return fee.percentage if fee else Decimal('0')

def create_checkout_session(request, ticket_order):
    from decimal import Decimal, ROUND_HALF_UP

    try:
        stripe.api_key = settings.STRIPE_SECRET_KEY

        event = ticket_order.event
        selected_tier = ticket_order.ticket_tier
        ticket_quantity = ticket_order.ticket_quantity

        organizer_profile = OrganizerProfile.objects.filter(
            user=event.host
        ).first()

        stripe_account_id = (
            organizer_profile.stripe_account_id
            if organizer_profile
            else None
        )

        if not stripe_account_id:
            print("Stripe Connect account is missing for this organizer.")
            return None

        ticket_amount = Decimal(str(selected_tier.price))
        subtotal = ticket_amount * ticket_quantity

        fee_percent = Decimal(str(get_active_ticket_fee_percent()))
        platform_fee = (
            subtotal * fee_percent / Decimal("100")
        ).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )

        unit_amount_cents = int(
            (ticket_amount * 100).quantize(
                Decimal("1"),
                rounding=ROUND_HALF_UP,
            )
        )
        platform_fee_cents = int(
            (platform_fee * 100).quantize(
                Decimal("1"),
                rounding=ROUND_HALF_UP,
            )
        )

        success_url = request.build_absolute_uri(
            reverse(
                "events:ticket_order_confirmed",
                args=[ticket_order.pk],
            )
        ) + "?session_id={CHECKOUT_SESSION_ID}"

        checkout_session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "product_data": {
                            "name": f"{event.title} - {selected_tier.name}",
                        },
                        "unit_amount": unit_amount_cents,
                    },
                    "quantity": ticket_quantity,
                }
            ],
            success_url=success_url,
            cancel_url=request.build_absolute_uri(
                reverse("events:public_event_attendance", args=[event.slug])
            ),
            client_reference_id=str(ticket_order.pk),
            metadata={
                "pending_order_id": str(ticket_order.pk),
                "event_id": str(event.pk),
                "tier_id": str(selected_tier.pk),
                "ticket_quantity": str(ticket_quantity),
                "platform_fee_percent": str(fee_percent),
                "platform_fee_amount": str(platform_fee),
            },
            payment_intent_data={
                "application_fee_amount": platform_fee_cents,
                "transfer_data": {
                    "destination": stripe_account_id,
                },
            },
        )

        ticket_order.stripe_session_id = checkout_session.id
        ticket_order.save(update_fields=["stripe_session_id", "updated_at"])

        return checkout_session

    except stripe.error.StripeError as exc:
        print(f"Stripe checkout error: {exc}")
        return None
    except Exception as exc:
        print(f"Unexpected checkout error: {exc}")
        return None

def finalize_ticket_order(checkout_session):
    """Finalize a paid Stripe ticket order and send its RSVP confirmation."""

    # Stripe Session objects must be converted before using dict methods.
    if hasattr(checkout_session, "to_dict"):
        checkout_session = checkout_session.to_dict()

    metadata = checkout_session.get("metadata") or {}
    ticket_order_id = metadata.get("pending_order_id")

    if not ticket_order_id:
        logger.warning(
            "Cannot finalize ticket order: Stripe session has no "
            "pending_order_id. Session ID: %s",
            checkout_session.get("id"),
        )
        return None

    if checkout_session.get("payment_status") != "paid":
        return None

    session_id = checkout_session.get("id")
    attendee = None
    refund_late_payment = False 

    with transaction.atomic():
        ticket_order = (
            TicketOrder.objects
            .select_for_update()
            .select_related("event", "ticket_tier", "event_attendee")
            .get(pk=ticket_order_id)
        )

        # Verify that the Stripe Session belongs to this order.
        if (
            ticket_order.stripe_session_id
            and ticket_order.stripe_session_id != session_id
        ):
            raise ValueError(
                "Stripe session does not match the pending order."
            )

        # A cancelled order may have received a late successful payment.
        # Never create an RSVP, issue tickets, or increment sold_count.
        if ticket_order.status == TicketOrder.STATUS_CANCELLED:
            refund_late_payment = True

        # A refunded order must never be finalized again.
        elif ticket_order.status == TicketOrder.STATUS_REFUNDED:
            logger.info(
                "Skipping finalization for refunded ticket order %s.",
                ticket_order_id,
            )
            return None

        # Already finalized: do not create duplicate tickets or increment
        # sold_count again. The confirmation email may still be retried.
        elif ticket_order.status == TicketOrder.STATUS_PAID:
            attendee = ticket_order.event_attendee

        # Only an eligible, unfinalized order reaches this section.
        elif ticket_order.status == TicketOrder.STATUS_PENDING:
            attendee = ticket_order.event_attendee

            if attendee is None:
                attendee = EventAttendee.objects.create(
                    event=ticket_order.event,
                    ticket_tier=ticket_order.ticket_tier,
                    ticket_quantity=ticket_order.ticket_quantity,
                    is_paid=True,
                    email=ticket_order.email,
                    first_name=ticket_order.first_name,
                    last_name=ticket_order.last_name,
                    phone=ticket_order.phone,
                    status="ATTENDING",
                    guest_message=ticket_order.guest_message,
                    dietary_restrictions=(
                        ticket_order.dietary_restrictions
                    ),
                )
                ticket_order.event_attendee = attendee

            else:
                update_fields = []

                if not attendee.is_paid:
                    attendee.is_paid = True
                    update_fields.append("is_paid")

                if update_fields:
                    if hasattr(attendee, "updated_at"):
                        update_fields.append("updated_at")

                    attendee.save(update_fields=update_fields)

            # Increase sold inventory exactly once when finalizing
            # the order for the first time.
            ticket_order.ticket_tier.sold_count += (
                ticket_order.ticket_quantity
            )
            ticket_order.ticket_tier.save(
                update_fields=["sold_count"]
            )

            # Preserve the existing individual-ticket QR workflow.
            if ticket_order.event.enable_qr_checkins:
                for ticket_number in range(
                    1, ticket_order.ticket_quantity + 1
                ):
                    IndividualTicket.objects.get_or_create(
                        event_attendee=attendee,
                        ticket_number=ticket_number,
                    )

            ticket_order.status = TicketOrder.STATUS_PAID
            ticket_order.paid_at = timezone.now()
            ticket_order.save(
                update_fields=[
                    "event_attendee",
                    "status",
                    "paid_at",
                    "updated_at",
                ]
            )

        else:
            logger.error(
                "Unexpected status '%s' for ticket order %s. "
                "Finalization skipped.",
                ticket_order.status,
                ticket_order_id,
            )
            return None

    # The transaction has committed. Handle late payments separately.
    if refund_late_payment:
        try:
            refund_ticket_order(ticket_order.pk)

            logger.warning(
                "Late payment refund processed without issuing tickets: "
                "order=%s, session=%s",
                ticket_order_id,
                session_id,
            )

        except Exception:
            logger.exception(
                "Could not refund late payment for cancelled order %s. "
                "No RSVP or tickets were issued. Manual review is required.",
                ticket_order_id,
            )

        return None

    # An already-paid order should have an associated RSVP.
    if attendee is None:
        logger.error(
            "Ticket order %s is paid, but no RSVP is associated with it.",
            ticket_order_id,
        )
        return None

    # Send confirmation only for a successfully finalized paid order.
    if not attendee.confirmation_sent:
        try:
            from .views import send_attendance_confirmation

            logger.info(
                "Sending paid RSVP confirmation: order=%s, RSVP=%s, "
                "recipient=%s",
                ticket_order_id,
                attendee.pk,
                attendee.email,
            )

            send_attendance_confirmation(attendee)

            # Verify whether the email function marked it as sent.
            attendee.refresh_from_db(fields=["confirmation_sent"])

            if attendee.confirmation_sent:
                logger.info(
                    "Paid RSVP confirmation sent successfully: "
                    "order=%s, RSVP=%s, recipient=%s",
                    ticket_order_id,
                    attendee.pk,
                    attendee.email,
                )
            else:
                logger.error(
                    "Paid RSVP confirmation was not marked as sent: "
                    "order=%s, RSVP=%s, recipient=%s. "
                    "Check send_attendance_confirmation() logs.",
                    ticket_order_id,
                    attendee.pk,
                    attendee.email,
                )

        except Exception:
            logger.exception(
                "Failed to send paid RSVP confirmation: "
                "order=%s, RSVP=%s, recipient=%s",
                ticket_order_id,
                attendee.pk,
                attendee.email,
            )

    else:
        logger.info(
            "Skipping duplicate confirmation email: RSVP %s "
            "is already marked as sent.",
            attendee.pk,
        )

    return attendee


def get_active_ticket_fee_percent():
    """
    Return the active ticket-sales platform fee percentage.
    Returns Decimal('0') if no active ticket fee is configured.
    """
    fee = PlatformFee.objects.filter(
        applies_to='TICKET',
        active=True,
    ).order_by('-updated_at').first()

    return fee.percentage if fee else Decimal('0')



def refund_ticket_order(order_id):
    """
    Refund a paid ticket order through Stripe Connect.

    A refund is recorded as successful only when Stripe confirms
    refund.status == "succeeded".

    Sold inventory is released only for a previously finalized paid
    order, and only once.

    A cancelled order with a late Stripe-confirmed payment is refunded
    without creating an RSVP or releasing inventory.
    """
    stripe.api_key = settings.STRIPE_SECRET_KEY

    # Read and validate the order while holding a short database lock.
    with transaction.atomic():
        ticket_order = (
            TicketOrder.objects
            .select_for_update()
            .get(pk=order_id)
        )

        if ticket_order.status == TicketOrder.STATUS_REFUNDED:
            return ticket_order

        if ticket_order.status not in (
            TicketOrder.STATUS_PAID,
            TicketOrder.STATUS_CANCELLED,
        ):
            raise ValueError(
                "Only paid orders or cancelled orders with a late "
                "payment can be refunded."
            )

        if not ticket_order.stripe_session_id:
            raise ValueError(
                "The order has no Stripe Checkout Session ID."
            )

        session_id = ticket_order.stripe_session_id

    checkout_session = stripe.checkout.Session.retrieve(session_id)

    if checkout_session.payment_status != "paid":
        raise ValueError(
            "Stripe has not confirmed payment. "
            "This order cannot be refunded as a paid order."
        )

    payment_intent = checkout_session.payment_intent

    if not payment_intent:
        raise ValueError(
            "The Stripe Checkout Session has no PaymentIntent."
        )

    # Stripe may return a PaymentIntent ID or an expanded object.
    if not isinstance(payment_intent, str):
        payment_intent = payment_intent.id

    refund = stripe.Refund.create(
        payment_intent=payment_intent,
        reverse_transfer=True,
        refund_application_fee=True,
        idempotency_key=(
            f"yoursevently-ticket-refund-{order_id}"
        ),
    )

    # Never mark the order refunded or release inventory for a
    # pending, failed, or otherwise unsuccessful Stripe refund.
    if refund.status != "succeeded":
        raise ValueError(
            f"Stripe has not confirmed a successful refund. "
            f"Refund ID: {refund.id}; status: {refund.status}."
        )

    # Stripe confirmed the refund. Now update the database atomically.
    with transaction.atomic():
        ticket_order = (
            TicketOrder.objects
            .select_for_update()
            .get(pk=order_id)
        )

        if ticket_order.status == TicketOrder.STATUS_REFUNDED:
            return ticket_order

        if ticket_order.status not in (
            TicketOrder.STATUS_PAID,
            TicketOrder.STATUS_CANCELLED,
        ):
            raise ValueError(
                "The order status changed while the refund was "
                "being processed. Review the Stripe refund and order."
            )

        # Only an order that was finalized as paid sold inventory.
        was_finalized_paid = (
            ticket_order.status == TicketOrder.STATUS_PAID
        )

        if (
            was_finalized_paid
            and not ticket_order.inventory_released
        ):
            ticket_tier = (
                TicketTier.objects
                .select_for_update()
                .get(pk=ticket_order.ticket_tier_id)
            )

            if ticket_tier.sold_count < ticket_order.ticket_quantity:
                logger.warning(
                    "Inventory count is lower than the quantity being "
                    "released for ticket order %s. sold_count=%s, "
                    "order_quantity=%s. Reconcile inventory.",
                    ticket_order.pk,
                    ticket_tier.sold_count,
                    ticket_order.ticket_quantity,
                )

            ticket_tier.sold_count = max(
                0,
                ticket_tier.sold_count - ticket_order.ticket_quantity,
            )
            ticket_tier.save(update_fields=["sold_count"])

            ticket_order.inventory_released = True

        ticket_order.status = TicketOrder.STATUS_REFUNDED
        ticket_order.refunded_at = timezone.now()
        ticket_order.stripe_refund_id = refund.id

        ticket_order.save(
            update_fields=[
                "status",
                "refunded_at",
                "stripe_refund_id",
                "inventory_released",
                "updated_at",
            ]
        )

        return ticket_order


# Connect
def create_stripe_express_account(user):
    """Creates a Stripe Express account for a given organizer."""
    # Fallback to a test email if user.email is blank
    user_email = user.email if user.email else f"user_{user.id}nexussolutech@gmail.com"
    
    account = stripe.Account.create(
        type="express",
        country="US",
        email=user_email,
        capabilities={
            "transfers": {"requested": True},
        },
    )
    return account.id

def generate_account_link(account_id, refresh_url, return_url):
    """Generates the secure Stripe-hosted onboarding link."""
    account_link = stripe.AccountLink.create(
        account=account_id,
        refresh_url=refresh_url,
        return_url=return_url,
        type="account_onboarding",
    )
    return account_link.url

def check_stripe_onboarding_status(account_id):
    """Checks if the account has finished submitting its required details."""
    account = stripe.Account.retrieve(account_id)
    return account.details_submitted

def generate_stripe_login_link(account_id):
    """Generates a single-use login link to the user's Stripe Express Dashboard."""
    login_link = stripe.Account.create_login_link(account_id)
    return login_link.url



## PRO. Platform
def create_upgrade_checkout(request, pending):
    stripe.api_key = settings.STRIPE_SECRET_KEY

    if pending.plan == 'unlimited':
        mode = 'subscription'
        price_id = settings.STRIPE_PRO_UNLIMITED_PRICE_ID
    else:
        mode = 'payment'
        price_id = settings.STRIPE_SINGLE_PRO_PRICE_ID

    success_url = request.build_absolute_uri(
        reverse('events:payment_success')
    )
    cancel_url = request.build_absolute_uri(
        reverse('events:upgrade_options', args=[pending.pk])
    )

    session = stripe.checkout.Session.create(
        mode=mode,
        line_items=[{
            'price': price_id,
            'quantity': 1,
        }],
        success_url=f'{success_url}?session_id={{CHECKOUT_SESSION_ID}}',
        cancel_url=cancel_url,
        customer_email=request.user.email,
        metadata={
            'pending_upgrade_id': str(pending.pk),
            'user_id': str(request.user.pk),
            'plan': pending.plan,
        },
    )

    pending.stripe_session_id = session.id
    pending.save(update_fields=['stripe_session_id', 'updated_at'])

    return session.url