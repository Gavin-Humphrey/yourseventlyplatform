import uuid
from django.conf import settings
from django.db import models
from django.utils import timezone
from django.contrib.auth.models import User
from django.utils.text import slugify
from urllib.parse import urlencode
import qrcode
import base64
from io import BytesIO
from django.urls import reverse
from decimal import Decimal
from django.core.validators import MinValueValidator, MaxValueValidator
from django_countries.fields import CountryField



class OrganizerProfile(models.Model):
    ACCOUNT_TYPE_CHOICES = [
        ('individual', 'Individual'),
        ('organization', 'Organization / Company'),
    ]
     
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='organizer_profile')
    display_name = models.CharField(max_length=255, blank=True, null=True)

    account_type = models.CharField(max_length=20, choices=ACCOUNT_TYPE_CHOICES, default='individual')
    organization_name = models.CharField(max_length=255, blank=True)

    phone = models.CharField(max_length=30, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state_province = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, blank=True)
    
    # --- Tier 2: Stripe Connect (For Ticket Payouts) ---
    stripe_account_id = models.CharField(max_length=255, blank=True, null=True)
    stripe_onboarding_complete = models.BooleanField(default=False)
    
    # --- Tier 4: Stripe Billing (For Monthly SaaS Subscriptions) ---
    stripe_customer_id = models.CharField(max_length=255, blank=True, null=True)
    stripe_subscription_id = models.CharField(max_length=255, blank=True, null=True)
    subscription_status = models.CharField(max_length=50, default='inactive') # e.g., 'active', 'canceled', 'past_due'
    
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.display_name or self.user.username

    @property
    def is_subscriber(self):
        """Returns True if the organizer has an active monthly SaaS subscription."""
        return self.subscription_status == 'active'


class OrganizerFollow(models.Model):
    """
    Represents an attendee following an event organizer / host.
    Completely independent of RSVP or ticketing status.
    """
    follower = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='following_organizers'
    )
    organizer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='organizer_followers'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('follower', 'organizer')
        indexes = [
            models.Index(fields=['organizer']),
            models.Index(fields=['follower']),
        ]
        verbose_name = 'Organizer Follow'
        verbose_name_plural = 'Organizer Follows'

    def __str__(self):
        return f"{self.follower.username} follows {self.organizer.username}"


class FollowNotificationPreference(models.Model):
    """
    User email and in-app preferences for organizer event notifications.
    Includes an unsubscribe token for zero-friction one-click email opt-out.
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='follow_notification_prefs'
    )
    email_new_events = models.BooleanField(
        default=True,
        help_text="Receive email alerts when followed organizers publish new events."
    )
    in_app_new_events = models.BooleanField(
        default=True,
        help_text="Receive in-app notification alerts in header bell."
    )
    unsubscribe_token = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Preferences for {self.user.username}"


class InAppNotification(models.Model):
    """
    In-app alert for attendees when an organizer publishes a new event.
    """
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notifications'
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='triggered_notifications'
    )
    event = models.ForeignKey(
        'events.Event',
        on_delete=models.CASCADE,
        related_name='published_notifications',
        null=True,
        blank=True
    )
    title = models.CharField(max_length=255)
    message = models.TextField()
    url = models.CharField(max_length=500)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Notification for {self.recipient.username}: {self.title}"


class EventFollowerAnnouncement(models.Model):
    event = models.ForeignKey(
        'events.Event',
        on_delete=models.CASCADE,
        related_name='follower_announcements',
    )
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='event_follower_announcements',
    )
    in_app_sent = models.BooleanField(default=False)
    email_sent = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['event', 'recipient'],
                name='unique_event_follower_announcement',
            )
        ]

EVENT_TYPE_CHOICES = [
    ("CONFERENCE", "Conference"),
    ("SEMINAR", "Seminar"),
    ("WORKSHOP", "Workshop"),
    ("CONCERT", "Concert"),
    ("GALA", "Gala"),
    ("BIRTHDAY", "Birthday Celebration"),
    ("WEDDING", "Wedding"),
    ("NETWORKING", "Networking"),
    ("FUNDRAISER", "Fundraiser"),
    ("PARTY", "Party"),
    ("WEBINAR", "Webinar"),
    ("EXHIBITION", "Exhibition"),
    ("SPORTS", "Sports"),
    ("COMMUNITY", "Community Event"),
    ("OTHER", "Other"),
]    
class Event(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    host = models.ForeignKey(User, on_delete=models.CASCADE, related_name='events')
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="", help_text=("Describe the event, its purpose, activities, intended audience,"  "and what attendees can expect."),) ## NOW
    event_type = models.CharField(max_length=20, choices=EVENT_TYPE_CHOICES, default="OTHER", help_text="Select the category that best describes this event.",) ## NOW
    food_served = models.BooleanField(default=False) # Now
    slug = models.SlugField(max_length=255, unique=True, blank=True, help_text="Unique URL path")
    event_date = models.DateTimeField()
    location_name = models.CharField(max_length=255, blank=True)
    address = models.TextField(blank=True, null=True)
    city = models.CharField(max_length=150, blank=True) # NEW
    country = CountryField() # NEW
    is_ticketed = models.BooleanField(
        default=False, 
        help_text="Enable paid ticketing and advanced inventory tracking for this event."
    )
    ########
    # Tier 3: One-time fee paid specifically for this event
    is_pro_upgraded = models.BooleanField(default=False)
    ########
    max_capacity = models.PositiveIntegerField(null=True, blank=True, help_text="Maximum total venue capacity")
    
    theme_settings = models.JSONField(
        default=dict, 
        blank=True, 
        help_text="Custom presentation options (primary_color, font_family, layout, page_bg_color, section_bg_color)"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    show_about_section = models.BooleanField(
        default=False,
        help_text="Display the host bio and story section on the public event page"
    )
    about_title = models.CharField(
        max_length=150,
        blank=True,
        default="About the Host",
        help_text="Heading title for the bio section"
    )
    about_text = models.TextField(
        blank=True,
        help_text="Detailed host biography or background story"
    )

    allow_plus_ones = models.BooleanField(
        default=False, 
        help_text="Allow guests to bring plus-ones"
    )
    max_plus_ones_per_guest = models.PositiveIntegerField(
        default=0, 
        help_text="Maximum number of plus-ones allowed per RSVP"
    )

    allow_guest_messages = models.BooleanField(
        default=False, 
        help_text="Allow guests to leave a special message for the celebrant"
    )
    announcement_title = models.CharField(max_length=150, blank=True, null=True, default="Announcement")
    announcement_text = models.TextField(blank=True, null=True)
    show_announcement = models.BooleanField(default=False)

    # Automation flags
    pre_reminder_sent = models.BooleanField(default=False)
    post_event_email_sent = models.BooleanField(default=False)

    enable_qr_checkins = models.BooleanField(
        default=False,
        help_text="Enable digital ticket QR codes and door scanner interface"
    )

    # Post-Event Deliverables & Certificate Fields ---
    presentation_file = models.FileField(
        upload_to='event_presentations/', 
        blank=True, 
        null=True, 
        help_text="Upload presentation slides, code zip, or resource guide."
    )
    resource_link = models.URLField(
        blank=True, 
        null=True, 
        help_text="External link to GitHub repo, Google Drive, Figma, etc."
    )
    enable_certificates = models.BooleanField(
        default=False, 
        help_text="Enable automatic completion certificate generation for attendees."
    )
    vault_code = models.CharField(
        max_length=50, 
        blank=True, 
        null=True, 
        help_text="Secret code to unlock the post-event photo vault."
    )
    show_countdown = models.BooleanField(
        default=True,
        verbose_name="Show event countdown",
        help_text="Display a live countdown on the public event page.",
    )

    @property
    def is_pro(self):
        """
        Safely checks if the event is Pro. If the user is a free-tier 
        user without an OrganizerProfile, it fails safely without crashing.
        """
        # Safely grab the profile if it exists; return None if it doesn't
        profile = getattr(self.host, 'organizer_profile', None)
        has_active_subscription = profile.is_subscriber if profile else False
        
        return self.is_pro_upgraded or has_active_subscription

    @property
    def is_past(self):
        """Returns True if the event date has already passed."""
        if not self.event_date:
            return False
        return self.event_date < timezone.now()

    def google_calendar_url(self):
        start_date = self.event_date.strftime('%Y%m%d') if hasattr(self.event_date, 'strftime') else str(self.event_date)
        base_url = "https://calendar.google.com/calendar/render"
        params = {
            'action': 'TEMPLATE',
            'text': self.title,
            'details': getattr(self, 'description', ''),
            'location': getattr(self, 'location_name', ''),
            'dates': f"{start_date}/{start_date}"
        }
        return f"{base_url}?{urlencode(params)}"

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.title) or 'event'
            slug = base_slug
            counter = 1
            while Event.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base_slug}-{counter}"
                counter += 1
            self.slug = slug
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.title} ({self.host.username})"


class GalleryImage(models.Model):
    event = models.ForeignKey('events.Event', related_name='gallery_images', on_delete=models.CASCADE)
    image = models.ImageField(upload_to='slide_gallery/')
    description = models.CharField(max_length=200, blank=True, null=True, help_text="Image description (max 200 chars)")
    
    # --- NEW: Distinguish between preview teaser and full post-event vault dump ---
    is_post_event = models.BooleanField(
        default=False, 
        help_text="True if uploaded via management dashboard for post-event view."
    )
    
    order = models.PositiveIntegerField(default=0, help_text="Display order sequence")

    class Meta:
        ordering = ['order', '-id']

    def __str__(self):
        status = "Post-Event" if self.is_post_event else "Preview"
        return f"{self.description or 'Gallery Image'} ({status}) for {self.event.title}"

class PendingUpgrade(models.Model):
    PLAN_CHOICES = [
        ('single', 'Single Pro'),
        ('unlimited', 'Unlimited Pro'),
    ]

    ACTION_CHOICES = [
        ('create', 'Create'),
        ('edit', 'Edit'),
    ]

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='pending_upgrades'
    )
    event = models.ForeignKey(
        Event,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='pending_upgrades'
    )
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    plan = models.CharField(max_length=20, choices=PLAN_CHOICES)
    data = models.JSONField(default=dict, blank=True)
    stripe_session_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        unique=True
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

class TicketTier(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='ticket_tiers')
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    capacity = models.PositiveIntegerField()
    sold_count = models.PositiveIntegerField(default=0)
    reserved_count = models.PositiveIntegerField(default=0) ###

class PlatformFee(models.Model):
    FEE_TYPE_CHOICES = [
        ('TICKET', 'Ticket Sales'),
    ]

    name = models.CharField(max_length=100)

    percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        validators=[
            MinValueValidator(Decimal('0')),
            MaxValueValidator(Decimal('100')),
        ],
    )

    applies_to = models.CharField(
        max_length=20,
        choices=FEE_TYPE_CHOICES,
    )

    active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.percentage}%)"
  

class EventAttendee(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('ATTENDING', 'Attending'),
        ('DECLINED', 'Declined'),
    ]

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='attendees')
    # ticket relation and payment status tracking
    ticket_tier = models.ForeignKey('TicketTier', on_delete=models.SET_NULL, null=True, blank=True, related_name='attendees')
    ticket_quantity = models.PositiveIntegerField(default=1) ##
    is_paid = models.BooleanField(default=False)

    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(blank=True, null=True)
    phone = models.CharField(max_length=20, blank=True, null=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='PENDING')
    plus_ones_allowed = models.IntegerField(default=0)
    plus_ones_count = models.IntegerField(default=0)
    dietary_restrictions = models.TextField(blank=True, null=True)
    guest_message = models.TextField(blank=True, null=True) 
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    confirmation_sent = models.BooleanField(default=False)

    checkin_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    checked_in = models.BooleanField(default=False)
    checked_in_at = models.DateTimeField(null=True, blank=True)

    # ANALYTICS FIELDS:
    # Campaign attribution
    campaign_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    campaign_platform = models.CharField(max_length=30, blank=True, default="")
    campaign_source = models.CharField(max_length=50, blank=True, default="")
    campaign_medium = models.CharField(max_length=50, blank=True, default="")

    class Meta:
        unique_together = ('event', 'email')

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"

    def __str__(self):
        return f"{self.full_name} - {self.event.title}"
  
    
    def get_qr_code_data_uri(self):
        if not self.event.enable_qr_checkins:
            return None

        checkin_url = reverse(
            'events:verify_checkin',
            kwargs={
                'slug': self.event.slug,
                'token': self.checkin_token,
            }
        )

        full_url = f"http://127.0.0.1:8000{checkin_url}"

        qr = qrcode.QRCode(version=1, box_size=10, border=2)
        qr.add_data(full_url)
        qr.make(fit=True)

        img = qr.make_image(fill_color="black", back_color="white")
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        encoded_img = base64.b64encode(buffer.getvalue()).decode("utf-8")

        return f"data:image/png;base64,{encoded_img}"


class FreeAttendeeGuest(models.Model):
    event_attendee = models.ForeignKey(EventAttendee, on_delete=models.CASCADE, related_name='plus_ones',)
    full_name = models.CharField(max_length=255)
    dietary_restrictions = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return (
            f"{self.full_name} (Guest of "
            f"{self.event_attendee.first_name} "
            f"{self.event_attendee.last_name})"
        )


class IndividualTicket(models.Model):
    event_attendee = models.ForeignKey(
        "EventAttendee",
        on_delete=models.CASCADE,
        related_name="individual_tickets",
    )
    ticket_number = models.PositiveIntegerField()
    checkin_token = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
    )
    checked_in = models.BooleanField(default=False)
    checked_in_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["ticket_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["event_attendee", "ticket_number"],
                name="unique_ticket_number_per_attendee",
            )
        ]

    def __str__(self):
        return (
            f"Ticket {self.ticket_number} - "
            f"Attendee {self.event_attendee_id}"
        )

    def get_qr_code_data_uri(self):
        """Return this ticket's check-in QR code as a PNG data URI."""

        if not self.event_attendee.event.enable_qr_checkins:
            return None

        checkin_url = reverse(
            "events:verify_checkin",
            kwargs={
                "slug": self.event_attendee.event.slug,
                "token": self.checkin_token,
            },
        )

        site_url = getattr(settings, "SITE_URL", "").rstrip("/")

        if not site_url:
            # Do not generate a QR code pointing to localhost in production.
            return None

        full_url = f"{site_url}{checkin_url}"

        qr = qrcode.QRCode(version=1, box_size=10, border=2)
        qr.add_data(full_url)
        qr.make(fit=True)

        image = qr.make_image(
            fill_color="black",
            back_color="white",
        )

        buffer = BytesIO()
        image.save(buffer, format="PNG")

        encoded_img = base64.b64encode(
            buffer.getvalue()
        ).decode("utf-8")

        return f"data:image/png;base64,{encoded_img}"

class TicketOrder(models.Model):
    STATUS_PENDING = "pending"
    STATUS_PAID = "paid"
    STATUS_CANCELLED = "cancelled"
    STATUS_REFUNDED = "refunded"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_PAID, "Paid"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_REFUNDED, "Refunded"),
    ]

    event = models.ForeignKey(
        "Event",
        on_delete=models.CASCADE,
        related_name="ticket_orders",
    )

    ticket_tier = models.ForeignKey(
        "TicketTier",
        on_delete=models.PROTECT,
        related_name="ticket_orders",
    )

    ticket_quantity = models.PositiveIntegerField(default=1)

    first_name = models.CharField(max_length=150)
    last_name = models.CharField(max_length=150, blank=True)
    email = models.EmailField()
    phone = models.CharField(max_length=50, blank=True)

    guest_message = models.TextField(blank=True)
    dietary_restrictions = models.TextField(blank=True)

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        db_index=True,
    )

    stripe_session_id = models.CharField(
        max_length=255,
        unique=True,
        null=True,
        blank=True,
    )

    event_attendee = models.OneToOneField(
        "EventAttendee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ticket_order",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    # Refund tracking and inventory-release protection.
    refunded_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    stripe_refund_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )

    inventory_released = models.BooleanField(default=False)

    # Campaign attribution.
    campaign_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    campaign_platform = models.CharField(max_length=30, blank=True, default="")
    campaign_source = models.CharField(max_length=50, blank=True, default="")
    campaign_medium = models.CharField(max_length=50, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return (
            f"{self.event} — {self.email} — "
            f"{self.ticket_quantity} ticket(s) — {self.status}"
        )


class Feedback(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='feedbacks')
    event_attendee = models.ForeignKey("EventAttendee", on_delete=models.SET_NULL, null=True, blank=True, related_name='feedbacks')
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True) # Optional if guests are unauthenticated
    rating = models.PositiveIntegerField()
    comment = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.event_attendee} - {self.rating} Stars"


# -------------------------------------------------------------------------
# Dedicated Curation Model in events/models.py
# Allows Admin curation, custom short blurbs, badge labels, & ordering
# while automatically verifying Pro / Subscriber & Upcoming eligibility.
# -------------------------------------------------------------------------
class FavoriteUpcomingEvent(models.Model):
    event = models.OneToOneField(
        'Event',
        on_delete=models.CASCADE,
        related_name='favorite_showcase',
        help_text="Select an upcoming Pro / Subscriber event to feature on the homepage."
    )
    short_blurb = models.CharField(
        max_length=140,
        blank=True,
        help_text="Optional short teaser for the micro-card."
    )
    badge_label = models.CharField(
        max_length=30,
        blank=True,
        help_text="Optional custom badge (e.g. 'Editor’s Pick', 'VIP Night', 'Featured')."
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Toggle visibility in the 'Favorite Upcoming Events' homepage showcase."
    )
    priority_order = models.PositiveIntegerField(
        default=0,
        help_text="Higher numbers appear first (ties broken by soonest event_date)."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-priority_order', 'event__event_date']
        verbose_name = "Favorite Upcoming Event"
        verbose_name_plural = "Favorite Upcoming Events"

    def __str__(self):
        return f"Featured: {self.event.title}"

    @property
    def is_eligible(self):
        """
        Eligible only when the event is upcoming and its host has
        an active Unlimited Pro / Subscriber subscription.

        Single-event Pro upgrades do not qualify.
        """
        if not self.event.event_date:
            return False

        if self.event.event_date < timezone.now():
            return False

        profile = getattr(
            self.event.host,
            'organizer_profile',
            None,
        )

        is_host_subscriber = (
            profile.is_subscriber if profile else False
        )

        return bool(is_host_subscriber)



class VisitorProfile(models.Model):
    """
    Profile for people who follow event organizers.
    Separate from OrganizerProfile.
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="visitor_profile",
    )
    email_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Visitor: {self.user.email or self.user.username}"


class VisitorFollowVerification(models.Model):
    """
    Tracks a visitor's request to follow an organizer while
    their email ownership is being verified.
    """
    email = models.EmailField(db_index=True)

    organizer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="visitor_follow_verifications",
    )

    token = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
    )

    return_url = models.CharField(
        max_length=500,
        default="/",
    )

    expires_at = models.DateTimeField()
    is_used = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def is_valid(self):
        return (
            not self.is_used
            and self.expires_at > timezone.now()
        )

    def __str__(self):
        return f"{self.email} → {self.organizer}"


class TranslationCache(models.Model):
    source_text = models.TextField()
    source_language = models.CharField(max_length=20, default='en')
    target_language = models.CharField(max_length=20)
    translated_text = models.TextField()

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    'source_text',
                    'source_language',
                    'target_language',
                ],
                name='unique_translation_cache_entry',
            ),
        ]

    def __str__(self):
        return (
            f'{self.source_language} → {self.target_language}: '
            f'{self.source_text[:50]}'
        )


##### CAMPAIGN ANALYTICS
class CampaignVisit(models.Model):
    """
    A deduplicated campaign visit for an event within a Django session.
    No IP address or other unnecessary visitor-identifying data is stored.
    """

    event = models.ForeignKey(
        "Event",
        on_delete=models.CASCADE,
        related_name="campaign_visits",
    )
    campaign_id = models.PositiveIntegerField(db_index=True)
    platform = models.CharField(max_length=30)
    source = models.CharField(max_length=50, blank=True, default="")
    medium = models.CharField(max_length=50, blank=True, default="")
    session_key = models.CharField(max_length=40)
    visited_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-visited_at"]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "event",
                    "campaign_id",
                    "platform",
                    "session_key",
                ],
                name="unique_event_campaign_platform_session",
            ),
        ]

    def __str__(self):
        return (
            f"Event {self.event_id} — campaign {self.campaign_id} — "
            f"{self.platform}"
        )