import uuid
from django.db import models
from django.utils import timezone
from django.contrib.auth.models import User
from django.utils.text import slugify
from urllib.parse import urlencode

import qrcode
import base64
from io import BytesIO
from django.urls import reverse



class OrganizerProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    display_name = models.CharField(max_length=255, blank=True, null=True)
    stripe_account_id = models.CharField(max_length=255, blank=True, null=True)
    stripe_onboarding_complete = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.display_name or self.user.username

class Event(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    host = models.ForeignKey(User, on_delete=models.CASCADE, related_name='events')
    title = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True, blank=True, help_text="Unique URL path")
    event_date = models.DateTimeField()
    location_name = models.CharField(max_length=255)
    address = models.TextField(blank=True, null=True)
    #######
    is_ticketed = models.BooleanField(
        default=False, 
        help_text="Enable paid ticketing and advanced inventory tracking for this event."
    )
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

    enable_qr_checkins = models.BooleanField(
        default=False,
        help_text="Enable digital ticket QR codes and door scanner interface"
    )

    @property
    def is_past(self):
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
    order = models.PositiveIntegerField(default=0, help_text="Display order sequence")

    class Meta:
        ordering = ['order', '-id']

    def __str__(self):
        return self.description or f"Gallery Image {self.pk}"


class TicketTier(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='ticket_tiers')
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    capacity = models.PositiveIntegerField()
    sold_count = models.PositiveIntegerField(default=0)

class RSVP(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('ATTENDING', 'Attending'),
        ('DECLINED', 'Declined'),
    ]

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='rsvps')
    # ticket relation and payment status tracking
    ticket_tier = models.ForeignKey('TicketTier', on_delete=models.SET_NULL, null=True, blank=True, related_name='rsvps')
    is_paid = models.BooleanField(default=False)

    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(blank=True, null=True)
    phone = models.CharField(max_length=20, blank=True, null=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='PENDING')
    plus_ones_allowed = models.IntegerField(default=0)
    plus_ones_count = models.IntegerField(default=0)
    dietary_restrictions = models.TextField(blank=True, null=True)
    guest_message = models.TextField(blank=True, null=True)  # Added for celebrant messages
    updated_at = models.DateTimeField(auto_now=True)

    confirmation_sent = models.BooleanField(default=False)

    checkin_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    checked_in = models.BooleanField(default=False)
    checked_in_at = models.DateTimeField(null=True, blank=True)

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
            
        checkin_url = reverse('events:verify_checkin', kwargs={
            'slug': self.event.slug, 
            'token': self.checkin_token
        })
        full_url = f"http://127.0.0.1:8000{checkin_url}"  # Update domain for production
        
        qr = qrcode.QRCode(version=1, box_size=10, border=2)
        qr.add_data(full_url)
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        encoded_img = base64.b64encode(buffer.getvalue()).decode('utf-8')
        return f"data:image/png;base64,{encoded_img}"
    

class RSVPGuest(models.Model):
    rsvp = models.ForeignKey(RSVP, on_delete=models.CASCADE, related_name='plus_ones')
    full_name = models.CharField(max_length=255)
    dietary_restrictions = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.full_name} (Guest of {self.rsvp.first_name} {self.rsvp.last_name})"