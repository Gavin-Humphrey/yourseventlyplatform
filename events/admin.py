from django.contrib import admin, messages
from django import forms
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
import logging

from .models import (
    Event,
    GalleryImage,
    OrganizerProfile,
    EventAttendee,
    FreeAttendeeGuest,
    Feedback,
    PlatformFee,
    FavoriteUpcomingEvent,
    TicketOrder,
    TicketTier,
)


logger = logging.getLogger(__name__)

class GuestInline(admin.TabularInline):
    model = EventAttendee
    extra = 1

class GalleryImageInline(admin.TabularInline):
    model = GalleryImage
    extra = 1
    fields = ['image', 'description', 'is_post_event', 'order'] # <--- Added is_post_event here!

@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ('title', 'host', 'event_date', 'slug', 'vault_code', 'pre_reminder_sent', 'post_event_email_sent', 'created_at')
    search_fields = ('title', 'host__username', 'slug')
    prepopulated_fields = {'slug': ('title',)}
    inlines = [GuestInline, GalleryImageInline]

@admin.register(EventAttendee)
class EventAttendeeAdmin(admin.ModelAdmin):
    list_display = ('full_name', 'email', 'event', 'status', 'checked_in', 'plus_ones_count', 'dietary_restrictions', 'guest_message')
    list_filter = ('status', 'checked_in', 'event')
    search_fields = ('first_name', 'last_name', 'email', 'guest_message', 'event__title')
    list_per_page = 25

@admin.register(Feedback)
class FeedbackAdmin(admin.ModelAdmin):
    list_display = ('event', 'get_guest_name', 'rating', 'created_at')
    list_filter = ('rating', 'created_at', 'event')
    search_fields = ('comment', 'event__title')
    list_per_page = 25

    def get_guest_name(self, obj):
        attendee = getattr(obj, "event_attendee", None)
        return attendee.full_name if attendee else "Anonymous"

@admin.register(OrganizerProfile)
class OrganizerProfileAdmin(admin.ModelAdmin):

    list_display = ('user', 'created_at', 'subscription')
    search_fields = ('user__username', 'user__email')
    list_filter = ('subscription_status', 'created_at')
    def subscription(self, obj):
        return obj.is_subscriber
    subscription.boolean = True
    subscription.short_description = 'Unlimited Pro'



@admin.register(PlatformFee)
class PlatformFeeAdmin(admin.ModelAdmin):
    list_display = (
        'name',
        'percentage',
        'applies_to',
        'active',
        'updated_at',
    )

    list_filter = (
        'applies_to',
        'active',
    )

    search_fields = ('name',)

######
@admin.register(TicketOrder)
class TicketOrderAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "event",
        "email",
        "ticket_tier",
        "ticket_quantity",
        "status",
        "stripe_session_id",
        "stripe_refund_id",
        "inventory_released",
        "created_at",
        "paid_at",
        "refunded_at",
    )

    list_filter = (
        "status",
        "created_at",
        "paid_at",
        "refunded_at",
    )

    search_fields = (
        "email",
        "first_name",
        "last_name",
        "event__title",
        "stripe_session_id",
        "stripe_refund_id",
    )

    readonly_fields = (
        "stripe_session_id",
        "stripe_refund_id",
        "created_at",
        "updated_at",
        "paid_at",
        "refunded_at",
        "inventory_released",
    )

    actions = (
        "cancel_ticket_orders",
        "refund_paid_orders",
    )

    @admin.action(description="Cancel selected pending ticket orders")
    def cancel_ticket_orders(self, request, queryset):
        cancelled_count = 0
        skipped_count = 0

        for order_id in queryset.values_list("pk", flat=True):
            with transaction.atomic():
                order = (
                    TicketOrder.objects
                    .select_for_update()
                    .get(pk=order_id)
                )

                if order.status != TicketOrder.STATUS_PENDING:
                    skipped_count += 1
                    continue

                # This checkout flow does not reserve inventory.
                order.status = TicketOrder.STATUS_CANCELLED
                order.save(update_fields=["status", "updated_at"])
                cancelled_count += 1

        if cancelled_count:
            self.message_user(
                request,
                f"Cancelled {cancelled_count} pending order(s). "
                "Ticket inventory was not changed.",
                level=messages.SUCCESS,
            )

        if skipped_count:
            self.message_user(
                request,
                f"Skipped {skipped_count} order(s) that were not pending.",
                level=messages.WARNING,
            )

    @admin.action(
        description="Refund selected paid or cancelled ticket orders"
    )
    def refund_paid_orders(self, request, queryset):
        from .stripe_utils import refund_ticket_order

        refunded_count = 0
        skipped_count = 0
        failed_count = 0

        for order_id in queryset.values_list("pk", flat=True):
            try:
                order = TicketOrder.objects.get(pk=order_id)

                if order.status not in (
                    TicketOrder.STATUS_PAID,
                    TicketOrder.STATUS_CANCELLED,
                ):
                    skipped_count += 1
                    continue

                refunded_order = refund_ticket_order(order_id)

                if (
                    refunded_order.status
                    == TicketOrder.STATUS_REFUNDED
                ):
                    refunded_count += 1
                else:
                    failed_count += 1

            except Exception:
                failed_count += 1
                logger.exception(
                    "Admin refund failed for ticket order %s",
                    order_id,
                )

        if refunded_count:
            self.message_user(
                request,
                f"Stripe-confirmed refund recorded for "
                f"{refunded_count} order(s).",
                level=messages.SUCCESS,
            )

        if skipped_count:
            self.message_user(
                request,
                f"Skipped {skipped_count} order(s) that were not "
                "eligible for refunds.",
                level=messages.WARNING,
            )

        if failed_count:
            self.message_user(
                request,
                f"Could not confirm or record {failed_count} refund(s). "
                "Check the application logs and Stripe Dashboard before "
                "retrying.",
                level=messages.ERROR,
            )

@admin.register(TicketTier)
class TicketTierAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "event",
        "name",
        "price",
        "capacity",
        "sold_count",
        "reserved_count",
        "pending_ticket_quantity",
        "refunded_ticket_order_quantity",
        "declined_attendee_ticket_quantity",
        "remaining_count",
    )

    list_filter = ("event",)
    search_fields = ("name", "event__title")

    readonly_fields = (
        "sold_count",
        "reserved_count",
        "pending_ticket_quantity",
        "refunded_ticket_order_quantity",
        "declined_attendee_ticket_quantity",
        "remaining_count",
    )

    @admin.display(description="Pending tickets")
    def pending_ticket_quantity(self, obj):
        return (
            TicketOrder.objects.filter(
                ticket_tier=obj,
                status__iexact="pending",
            ).aggregate(
                total=Sum("ticket_quantity")
            )["total"] or 0
        )

    @admin.display(description="Refunded tickets")
    def refunded_ticket_order_quantity(self, obj):
        return (
            TicketOrder.objects.filter(
                ticket_tier=obj,
                status__iexact="refunded",
            ).aggregate(
                total=Sum("ticket_quantity")
            )["total"] or 0
        )

    @admin.display(description="Declined attendee tickets")
    def declined_attendee_ticket_quantity(self, obj):
        return (
            EventAttendee.objects.filter(
                ticket_tier=obj,
                status__iexact="declined",
            ).aggregate(
                total=Sum("ticket_quantity")
            )["total"] or 0
        )

    @admin.display(description="Remaining tickets")
    def remaining_count(self, obj):
        return max(
            0,
            obj.capacity - obj.sold_count - obj.reserved_count,
        )

######

FAVORITE_BADGE_CHOICES = [
    ('', '— Default (Ticketed / Open RSVP) —'),
    ("Editor's Pick", "✦ Editor's Pick"),
    ('Featured', '★ Featured'),
    ('VIP Event', '◆ VIP Event'),
    ('Special Highlight', '✦ Special Highlight'),
    ('Community Favorite', '♥ Community Favorite'),
    ('Trending', '↗ Trending'),
    ('Popular', '✧ Popular'),
    ('Coming Soon', '⏳ Coming Soon'),
    ('Happening Soon', '! Happening Soon'),
    ('Upcoming', '✦ Upcoming'),
]


class FavoriteUpcomingEventAdminForm(forms.ModelForm):
    badge_label = forms.ChoiceField(
        choices=FAVORITE_BADGE_CHOICES,
        required=False,
        label='Badge',
    )

    class Meta:
        model = FavoriteUpcomingEvent
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # ONLY upcoming events belonging to active subscribers.
        # Do not use Event.is_pro here because it includes Single-event Pro.
        self.fields['event'].queryset = Event.objects.filter(
            event_date__gte=timezone.now(),
            host__organizer_profile__subscription_status='active',
        ).order_by('event_date')


@admin.register(FavoriteUpcomingEvent)
class FavoriteUpcomingEventAdmin(admin.ModelAdmin):
    form = FavoriteUpcomingEventAdminForm

    list_display = (
        'event',
        'get_event_date',
        'badge_label',
        'priority_order',
        'is_active',
    )

    list_filter = ('is_active',)

    list_editable = (
        'badge_label',
        'priority_order',
        'is_active',
    )

    search_fields = (
        'event__title',
        'event__location_name',
        'short_blurb',
    )

    # Intentionally omit autocomplete_fields.
    # The regular event selector respects the form queryset.

    def get_queryset(self, request):
        # Show only upcoming events hosted by active subscribers.
        return super().get_queryset(request).filter(
            event__event_date__gte=timezone.now(),
            event__host__organizer_profile__subscription_status='active',
        ).select_related(
            'event',
            'event__host',
            'event__host__organizer_profile',
        )

    @admin.display(
        description='Event Date',
        ordering='event__event_date',
    )
    def get_event_date(self, obj):
        return obj.event.event_date
