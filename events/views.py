import os
import io
import uuid
from django.shortcuts import render, get_object_or_404, redirect
from django.conf import settings
from django.http import Http404, HttpResponse
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from django.contrib import messages
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm
from django.contrib.auth.decorators import login_required
from .models import (
    Event, FavoriteUpcomingEvent, PendingUpgrade, OrganizerProfile, 
    OrganizerFollow, FollowNotificationPreference, InAppNotification, 
    GalleryImage, RSVP, RSVPGuest, TicketTier, PlatformFee, IndividualTicket, 
    PendingTicketOrder, CampaignVisit,
)
from .forms import (
    HostRegistrationForm, EventForm, RSVPForm, TicketTierFormSet, 
    EventTicketingForm, FeedbackForm,
) 
from django.forms import inlineformset_factory
from django.utils.text import slugify
from django.db.models import Count, Q, Value, Sum
from decimal import Decimal
from django.db.models.functions import Concat
import csv
from .utils import send_rsvp_confirmation, notify_organizer_of_new_follower, notify_followers_of_new_event, country_flag_emoji
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from .wallet import generate_google_wallet_link, get_ticket_context
import stripe
from .pro_utils import needs_pro_upgrade, user_has_unlimited_pro
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.http import JsonResponse
from django.utils.http import url_has_allowed_host_and_scheme
from .models import VisitorProfile, VisitorFollowVerification
from .utils import send_visitor_follow_verification
from django.db import transaction
import logging
from collections import defaultdict
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.views.decorators.http import require_POST
from reportlab.lib.pagesizes import letter, landscape
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from django.db.models import OuterRef, Subquery
from django.core.mail import send_mail

from .stripe_utils import (
    create_checkout_session,
    create_or_get_stripe_customer,
    create_upgrade_checkout,
    get_active_ticket_fee_percent,
    prepare_pending_data,
    store_pending_files,

    finalize_pending_ticket_order,

    create_stripe_express_account,
    generate_account_link,
    check_stripe_onboarding_status,
    generate_stripe_login_link
)

logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY

User = get_user_model()

# -------------------------------------------------------------------
# HOST AUTHENTICATION VIEWS
# -------------------------------------------------------------------
def register_host(request):
    if request.user.is_authenticated:
        return redirect('events:dashboard')

    if request.method == 'POST':
        form = HostRegistrationForm(request.POST)

        if form.is_valid():
            user = form.save()
            login(request, user)

            messages.success(
                request,
                "Account created successfully! Welcome to your dashboard."
            )

            return redirect('events:dashboard')
    else:
        form = HostRegistrationForm()

    return render(
        request,
        'registration/register.html',
        {'form': form}
    )

def login_host(request):
    if request.user.is_authenticated:
        return redirect('dashboard')

    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            username = form.cleaned_data.get('username')
            password = form.cleaned_data.get('password')
            user = authenticate(username=username, password=password)
            if user is not None:
                login(request, user)
                return redirect('dashboard')
    else:
        form = AuthenticationForm()
    return render(request, 'registration/register.html', {'form': form})

def logout_host(request):
    logout(request)
    messages.info(request, "You have been logged out.")
    return redirect('login')

# -------------------------------------------------------------------
# PUBLIC & GUEST VIEWS
# -------------------------------------------------------------------
def home(request):
    now = timezone.now()

    # Preserve the raw selection so both country names and codes work.
    requested_fav_country = request.GET.get(
        'fav_country', ''
    ).strip()

    # Build the eligible curated showcase.
    curated_qs = (
        FavoriteUpcomingEvent.objects
        .select_related(
            'event',
            'event__host',
            'event__host__organizer_profile',
        )
        .filter(
            is_active=True,
            event__event_date__gte=now,
        )
        .filter(
            Q(event__is_pro_upgraded=True)
            | Q(
                event__host__organizer_profile__subscription_status='active'
            )
        )
        .order_by(
            '-priority_order',
            'event__event_date',
        )
    )

    # Use curated events when eligible upcoming curated entries exist.
    # Otherwise, use the existing automatic-discovery fallback.
    has_curated_entries = curated_qs.exists()

    if has_curated_entries:
        showcase_list = curated_qs
        country_field_name = 'event__country'
        location_field_name = 'event__location_name'
        address_field_name = 'event__address'
        city_field_name = 'event__city'
    else:
        showcase_list = (
            Event.objects
            .select_related(
                'host',
                'host__organizer_profile',
            )
            .filter(event_date__gte=now)
            .filter(
                Q(is_pro_upgraded=True)
                | Q(
                    host__organizer_profile__subscription_status='active'
                )
            )
            .order_by('event_date')
        )
        country_field_name = 'country'
        location_field_name = 'location_name'
        address_field_name = 'address'
        city_field_name = 'city'

    # Get official country names from Django's CountryField.
    all_country_choices = {
        str(code).upper(): str(name)
        for code, name in Event._meta.get_field('country').choices
    }

    # Only show countries configured in settings.py.
    configured_country_codes = [
        str(code).strip().upper()
        for code in getattr(
            settings,
            'FAVORITE_UPCOMING_COUNTRIES',
            [],
        )
        if code
    ]

    fav_country_choices = [
        (
            code,
            all_country_choices[code],
            country_flag_emoji(code),
        )
        for code in configured_country_codes
        if code in all_country_choices
    ]

    # Accept either ISO codes (FR) or country names (France).
    country_lookup = {}

    for code, name, flag in fav_country_choices:
        country_lookup[code.casefold()] = code
        country_lookup[name.casefold()] = code

    selected_fav_country = country_lookup.get(
        requested_fav_country.casefold(),
        '',
    )

    country_names = {
        code: name
        for code, name, flag in fav_country_choices
    }

    selected_fav_country_name = country_names.get(
        selected_fav_country,
        '',
    )

    # Filter before pagination. An empty selection means all locations.
    if selected_fav_country:
        selected_country_name = country_names[selected_fav_country]

        # CountryField stores the ISO country code, such as FR.
        country_query = Q(
            **{country_field_name: selected_fav_country}
        )

        # Support older events where the country was included in
        # the location, address, or city instead of the country field.
        missing_country = (
            Q(**{country_field_name: ''})
            | Q(**{f'{country_field_name}__isnull': True})
        )

        legacy_location_query = (
            Q(**{
                f'{location_field_name}__icontains': selected_country_name
            })
            | Q(**{
                f'{address_field_name}__icontains': selected_country_name
            })
            | Q(**{
                f'{city_field_name}__icontains': selected_country_name
            })
        )

        country_query |= missing_country & legacy_location_query

        showcase_list = showcase_list.filter(country_query)

    # Filter first, then paginate: three events per page.
    paginator = Paginator(showcase_list, 3)
    page_number = request.GET.get('fav_page', 1)

    try:
        favorite_events = paginator.page(page_number)
    except PageNotAnInteger:
        favorite_events = paginator.page(1)
    except EmptyPage:
        favorite_events = paginator.page(paginator.num_pages)

    context = {
        'favorite_events': favorite_events,
        'selected_fav_country': selected_fav_country,
        'selected_fav_country_name': selected_fav_country_name,
        'fav_country_choices': fav_country_choices,
    }

    return render(request, 'events/home.html', context)


def event_detail(request, slug):
    event = get_object_or_404(Event, slug=slug)

    # --- VAULT UNLOCK LOGIC ---
    vault_unlocked = request.session.get(f'vault_unlocked_{event.id}', False)
    
    if request.method == 'POST' and 'unlock_vault_code' in request.POST:
        entered_code = request.POST.get('vault_code', '').strip()
        expected_code = getattr(event, 'vault_code', 'YOURSECRETCODE') 
        
        if entered_code == expected_code:
            request.session[f'vault_unlocked_{event.id}'] = True
            vault_unlocked = True
            messages.success(request, "Photo vault successfully unlocked!")
            return redirect(request.path)
        else:
            messages.error(request, "Incorrect unlock code. Please try again.")

    # Instantiate the form for the template
    form = RSVPForm(event=event) 
    
    # Safely retrieve theme settings
    raw_theme = event.theme_settings() if callable(getattr(event, 'theme_settings', None)) else getattr(event, 'theme_settings', None)
    
    theme = {}
    if isinstance(raw_theme, dict):
        theme = raw_theme.copy()
        theme['theme_color'] = theme.get('theme_color') or theme.get('primary_color') or '#3b82f6'
    elif raw_theme is not None:
        theme = raw_theme

    # --- MODEL-DRIVEN SYNCHRONIZED GALLERY LOGIC ---
    if event.is_past and vault_unlocked:
        active_db_images = GalleryImage.objects.filter(event=event, is_post_event=True)
    else:
        active_db_images = GalleryImage.objects.filter(event=event, is_post_event=False)[:5]

    # Map database images directly to the normalized structure expected by templates
    normalized_gallery = []
    for img_obj in active_db_images:
        normalized_gallery.append({
            'url': img_obj.image.url, 
            'caption': img_obj.description or '', 
            'is_post_event': img_obj.is_post_event
        })
                
    guest_messages = event.rsvps.exclude(guest_message__isnull=True).exclude(guest_message__exact='')

    # 1. Check if the authenticated user has already submitted feedback
    user_has_submitted = False
    if request.user.is_authenticated:
        user_has_submitted = event.feedbacks.filter(user=request.user).exists()

    # 2. Paginate existing feedbacks (5 per page)
    feedback_list = event.feedbacks.all().order_by('-created_at')
    paginator = Paginator(feedback_list, 2)
    page_number = request.GET.get('page')
    feedbacks = paginator.get_page(page_number)

    # 3. Instantiate the form for new feedback
    feedback_form = FeedbackForm()
    
    return render(request, 'events/event_detail.html', {
        'event': event, 
        'form': form, 
        'theme': theme,
        'guest_messages': guest_messages,
        'gallery_images': active_db_images,
        'normalized_gallery': normalized_gallery,
        'feedback_form': feedback_form,  
        'feedbacks': feedbacks,
        'user_has_submitted': user_has_submitted,
        'vault_unlocked': vault_unlocked,
    })

# -------------------------------------------------------------------
# HOST DASHBOARD & EVENT MANAGEMENT
# -------------------------------------------------------------------
@login_required
def dashboard(request):
    events = Event.objects.filter(host=request.user).order_by('-created_at')
    
    # Safely get or create the organizer profile so it's never None
    organizer_profile, created = OrganizerProfile.objects.get_or_create(user=request.user)
    
    # Check if the host has created at least one ticketed event
    has_ticketed_events = events.filter(is_ticketed=True).exists()
    
    total_events = events.count()
    total_guests = RSVP.objects.filter(event__host=request.user).count()
    attending_guests = RSVP.objects.filter(event__host=request.user, status='ATTENDING').count()

    # Debug print using the safe profile instance
    print(f"DEBUG DASHBOARD - Onboarding Complete Status: {organizer_profile.stripe_onboarding_complete}")

    context = {
        'events': events,
        'total_events': total_events,
        'total_guests': total_guests,
        'attending_guests': attending_guests,
        'organizer_profile': organizer_profile,
        'has_ticketed_events': has_ticketed_events,
    }
    return render(request, 'events/dashboard.html', context)


# Pro
@login_required
def create_event(request):
    GalleryFormSet = inlineformset_factory(
        Event,
        GalleryImage,
        fields=['image', 'description', 'order'],
        extra=0,
        can_delete=True
    )

    if request.method == 'POST':
        form = EventForm(request.POST, request.FILES)
        formset = GalleryFormSet(
            request.POST,
            request.FILES,
            prefix='gallery_images'
        )

        if form.is_valid() and formset.is_valid():
            event = form.save(commit=False)
            event.host = request.user

            if needs_pro_upgrade(event, request.user):
                token = str(uuid.uuid4())
                data = prepare_pending_data(form)
                data['files'] = store_pending_files(
                    request,
                    form,
                    formset,
                    token
                )

                pending = PendingUpgrade.objects.create(
                    user=request.user,
                    action='create',
                    plan='single',
                    data=data
                )

                return redirect(
                    'events:upgrade_options',
                    pk=pending.pk
                )

            event.is_pro_upgraded = False
            event.save()

            formset.instance = event
            formset.save()

            notify_followers_of_new_event(event) #

            messages.success(
                request,
                f"Event '{event.title}' created successfully!"
            )

            return redirect('events:dashboard')

    else:
        form = EventForm()
        formset = GalleryFormSet(prefix='gallery_images')

    return render(request, 'events/event_form.html', {
        'form': form,
        'formset': formset,
        'action': 'Create'
    })


@login_required
def edit_event(request, slug=None):
    event = get_object_or_404(
        Event,
        slug=slug,
        host=request.user
    )

    if event.is_past:
        messages.error(request, "Past events cannot be edited.")
        return redirect('events:event_detail', slug=event.slug)

    GalleryFormSet = inlineformset_factory(
        Event,
        GalleryImage,
        fields=['image', 'description', 'order'],
        extra=0,
        can_delete=True
    )

    if request.method == 'POST':
        form = EventForm(
            request.POST,
            request.FILES,
            instance=event
        )

        formset = GalleryFormSet(
            request.POST,
            request.FILES,
            instance=event,
            queryset=event.gallery_images.all(),
            prefix='gallery_images'
        )

        if form.is_valid() and formset.is_valid():
            updated_event = form.save(commit=False)

            if needs_pro_upgrade(updated_event, request.user):
                token = str(uuid.uuid4())
                data = prepare_pending_data(form)
                data['files'] = store_pending_files(
                    request,
                    form,
                    formset,
                    token,
                    is_edit=True
                )

                pending = PendingUpgrade.objects.create(
                    user=request.user,
                    event=event,
                    action='edit',
                    plan='single',
                    data=data
                )

                return redirect(
                    'events:upgrade_options',
                    pk=pending.pk
                )

            if updated_event.is_ticketed:
                profile, _ = OrganizerProfile.objects.get_or_create(
                    user=request.user
                )

                if not profile.stripe_onboarding_complete:
                    messages.warning(
                        request,
                        "You must connect your payout account before updating a ticketed event."
                    )
                    return redirect('events:connect_stripe_onboarding')

            saved_event = form.save()
            formset.save()

            messages.success(
                request,
                f"Event '{saved_event.title}' updated successfully!"
            )

            return redirect(
                'events:event_detail',
                slug=saved_event.slug
            )

    else:
        form = EventForm(instance=event)
        formset = GalleryFormSet(
            instance=event,
            queryset=event.gallery_images.all(),
            prefix='gallery_images'
        )

    return render(request, 'events/event_form.html', {
        'form': form,
        'formset': formset,
        'action': 'Edit'
    })


def event_form_view(request, pk=None):
    # Fetch existing instance if editing; otherwise, create a new one (None)
    event = get_object_or_404(Event, pk=pk) if pk else None

    if event and event.is_past:
        messages.error(request, "This event has concluded and can no longer be edited.")
        return redirect('events:dashboard')

    if request.method == 'POST':
        form = EventForm(request.POST, request.FILES, instance=event)
        if form.is_valid():
            saved_event = form.save()
            return redirect('event_detail', pk=saved_event.pk)
    else:
        form = EventForm(instance=event)

    return render(request, 'event_form.html', {
        'form': form,
        'is_edit': event is not None
    })

# 3. PUBLIC RSVP PAGE (Guest view)

def public_rsvp(request, slug):
    event = get_object_or_404(Event, slug=slug)

    capture_event_campaign_attribution(request, event) # NEWLY ADDED ANALYTICS
    
    # --- VAULT UNLOCK LOGIC ---
    vault_unlocked = request.session.get(f'vault_unlocked_{event.id}', False)
    
    # Always initialize form at the top so it's never unbound
    form = RSVPForm(event=event)
    
    if request.method == 'POST':
        if 'unlock_vault_code' in request.POST:
            entered_code = request.POST.get('vault_code', '').strip()
            expected_code = getattr(event, 'vault_code', 'YOURSECRETCODE') 
            
            if entered_code == expected_code:
                request.session[f'vault_unlocked_{event.id}'] = True
                vault_unlocked = True
                messages.success(request, "Photo vault successfully unlocked!")
                return redirect(request.path)
            else:
                messages.error(request, "Incorrect unlock code. Please try again.")
        else:
            form = RSVPForm(request.POST, event=event)
            if form.is_valid():
                rsvp = form.save(commit=False)
                rsvp.event = event
                rsvp.status = 'ATTENDING'
                
                requested_plus_ones = form.cleaned_data.get('plus_ones_count', 0)
                total_requested = 1 + requested_plus_ones
                
                current_headcount = sum(r.plus_ones.count() + 1 for r in event.rsvps.filter(status='ATTENDING'))
                if event.max_capacity and (current_headcount + total_requested > event.max_capacity):
                    messages.error(request, "Sorry, this event does not have enough remaining capacity for your requested plus-ones.")
                else:
                    ### NEWLY ADDED ANALYTICS
                    attribution = get_event_campaign_attribution(request, event)

                    rsvp.campaign_id = attribution.get("campaign_id")
                    rsvp.campaign_platform = attribution.get("platform", "")
                    rsvp.campaign_source = attribution.get("source", "")
                    rsvp.campaign_medium = attribution.get("medium", "")
                    #########
                    rsvp.save()
                    request.session[f'rsvp_verified_{event.pk}'] = rsvp.pk
                    
                    plus_one_names = request.POST.getlist('plus_one_names[]')
                    for name in plus_one_names:
                        if name.strip():
                            RSVPGuest.objects.create(rsvp=rsvp, full_name=name.strip())
                    
                    try:
                        send_rsvp_confirmation(rsvp)
                    except Exception as e:
                        print(f"Email failed to send: {e}")
                        
                    return redirect('events:rsvp_confirmed', pk=rsvp.pk)

    raw_theme = event.theme_settings() if callable(getattr(event, 'theme_settings', None)) else getattr(event, 'theme_settings', None)
    
    theme = {}
    if isinstance(raw_theme, dict):
        theme = raw_theme.copy()
        theme['theme_color'] = theme.get('theme_color') or theme.get('primary_color') or '#3b82f6'
    elif raw_theme is not None:
        theme = raw_theme

    # --- MODEL-DRIVEN SYNCHRONIZED GALLERY LOGIC ---
    if event.is_past and vault_unlocked:
        active_db_images = GalleryImage.objects.filter(event=event, is_post_event=True)
    else:
        active_db_images = GalleryImage.objects.filter(event=event, is_post_event=False)[:5]

    normalized_gallery = []
    for img_obj in active_db_images:
        normalized_gallery.append({
            'url': img_obj.image.url, 
            'caption': img_obj.description or '', 
            'is_post_event': img_obj.is_post_event
        })

    guest_messages = event.rsvps.exclude(guest_message__isnull=True).exclude(guest_message__exact='')

    user_rsvp = None
    user_has_submitted = False
    
    rsvp_id = request.session.get(f'rsvp_verified_{event.pk}')
    if rsvp_id:
        user_rsvp = event.rsvps.filter(pk=rsvp_id, status='ATTENDING').first()
    
    if user_rsvp:
        user_has_submitted = event.feedbacks.filter(rsvp=user_rsvp).exists()

    feedback_list = event.feedbacks.all().order_by('-created_at')
    paginator = Paginator(feedback_list, 2)
    page_number = request.GET.get('page')
    feedbacks = paginator.get_page(page_number)

    feedback_form = FeedbackForm()

    ######
    organizer_followers_count = OrganizerFollow.objects.filter(
        organizer=event.host
    ).count()
    ##########

    context = {
        'event': event,
        'form': form,
        'theme': theme,
        'guest_messages': guest_messages,
        'gallery_images': active_db_images,       
        'normalized_gallery': normalized_gallery, 
        'feedback_form': feedback_form,  
        'feedbacks': feedbacks,
        'user_has_submitted': user_has_submitted,
        'user_rsvp': user_rsvp,
        'vault_unlocked': vault_unlocked,  
        'ticket_fee_percent': get_active_ticket_fee_percent(),   
        "organizer_followers_count": organizer_followers_count, #####    
    }
    return render(request, 'events/public_rsvp.html', context)
############


def verify_rsvp_email(request, slug):
    """Verifies the attendee's email against the event's RSVPs and unlocks the feedback form via session."""
    event = get_object_or_404(Event, slug=slug)
    if request.method == 'POST':
        email = request.POST.get('attendee_email', '').strip()
        if email:
            matching_rsvp = event.rsvps.filter(email__iexact=email, status='ATTENDING').first()
            if matching_rsvp:
                request.session[f'rsvp_verified_{event.pk}'] = matching_rsvp.pk
                messages.success(request, "Identity verified! You can now submit your feedback.")
            else:
                messages.error(request, "No matching attending RSVP found with that email address.")
    return redirect('events:public_rsvp', slug=event.slug)


# CONNECT
def submit_rsvp(request, slug):
    event = get_object_or_404(Event, slug=slug)

    if event.is_past:
        messages.error(request, "This event has concluded. RSVPs are closed.")
        return redirect('events:public_rsvp', slug=event.slug)
    
    if request.method == 'POST':
        email = request.POST.get('email', '').strip().lower()
        full_name = request.POST.get('full_name', '').strip()
        first_name = request.POST.get('first_name') or (full_name.split(' ', 1)[0] if full_name else '')
        last_name = request.POST.get('last_name') or (full_name.split(' ', 1)[1] if len(full_name.split(' ', 1)) > 1 else '')
        
        phone_prefix = request.POST.get('phone_prefix', '').strip()
        custom_prefix = request.POST.get('custom_phone_prefix', '').strip()
        raw_phone = request.POST.get('phone', '').strip()

        active_prefix = custom_prefix if phone_prefix == 'OTHER' and custom_prefix else phone_prefix
        phone = f"{active_prefix} {raw_phone}".strip() if raw_phone else ''

        raw_status = request.POST.get('status', 'ATTENDING').upper()
        guest_message = request.POST.get('guest_message', '').strip()
        dietary = request.POST.get('dietary_restrictions', '').strip()
        ticket_quantity = 1
        try:
            ticket_quantity = int(request.POST.get('ticket_quantity', '1'))
            if ticket_quantity < 1:
                raise ValueError
        except (TypeError, ValueError):
            ticket_quantity = 0

        #####

        selected_tier = None
        status = 'ATTENDING'
        
        if raw_status == 'DECLINED':
            status = 'DECLINED'
        elif raw_status.startswith('TICKET_'):
            try:
                tier_id = int(raw_status.split('_')[1])
                selected_tier = TicketTier.objects.get(id=tier_id, event=event)
                status = 'ATTENDING'
            except (IndexError, ValueError, TicketTier.DoesNotExist):
                selected_tier = None
        else:
            status = raw_status

        try:
            plus_ones_count = int(request.POST.get('plus_ones_count') or request.POST.get('guest_count', 0))
            if event.allow_plus_ones:
                plus_ones_count = min(plus_ones_count, event.max_plus_ones_per_guest)
            else:
                plus_ones_count = 0
        except ValueError:
            plus_ones_count = 0

        errors = {}

        if not email:
            errors['email'] = "Email is required."
        elif RSVP.objects.filter(event=event, email=email).exists():
            errors['email'] = "This email address has already been used to RSVP for this event."

        if not first_name and not full_name:
            errors['full_name'] = "Full name is required."

        if not errors and selected_tier:
            if selected_tier.sold_count >= selected_tier.capacity:
                errors['email'] = f"Sorry, the '{selected_tier.name}' pass is completely sold out."

        if not errors and event.max_capacity and status == 'ATTENDING':
            incoming_headcount = 1 + plus_ones_count
            
            current_headcount = event.rsvps.filter(status='ATTENDING').aggregate(
                total=Sum('plus_ones_count')
            )['total'] or 0
            current_primary_count = event.rsvps.filter(status='ATTENDING').count()
            total_current_attendees = current_primary_count + current_headcount

            if (total_current_attendees + incoming_headcount) > event.max_capacity:
                remaining_spots = max(0, event.max_capacity - total_current_attendees)
                errors['plus_ones_count'] = f"Sorry, this event has reached its maximum venue capacity of {event.max_capacity} guests. Only {remaining_spots} spot(s) remaining."

        if errors:
            raw_theme = event.theme_settings() if callable(getattr(event, 'theme_settings', None)) else getattr(event, 'theme_settings', None)
            theme = raw_theme.copy() if isinstance(raw_theme, dict) else (raw_theme or {})
            if isinstance(theme, dict):
                theme['theme_color'] = theme.get('theme_color') or theme.get('primary_color') or '#3b82f6'

            raw_gallery = theme.get('gallery_slides') or theme.get('gallery_images', [])
            normalized_gallery = []
            for item in raw_gallery:
                if isinstance(item, str):
                    normalized_gallery.append({'url': item, 'caption': ''})
                elif isinstance(item, dict):
                    url = item.get('url') or item.get('image') or ''
                    caption = item.get('description') or item.get('caption') or item.get('text') or ''
                    if url:
                        normalized_gallery.append({'url': url, 'caption': caption})

            context = {
                'event': event,
                'theme': theme,
                'guest_messages': event.rsvps.exclude(guest_message__isnull=True).exclude(guest_message__exact=''),
                'normalized_gallery': normalized_gallery,
                'form_data': request.POST,  
                'form_errors': errors,      
            }
            return render(request, 'events/public_rsvp.html', context)

        # PAID TICKET FLOW        
        if selected_tier and selected_tier.price > 0:
            if not getattr(settings, 'STRIPE_SECRET_KEY', None):
                messages.error(
                    request,
                    "Online payment is currently unavailable. Please try again later.",
                )
                return redirect('events:public_rsvp', slug=event.slug)

            # Retrieve campaign attribution captured on the public event page.
            attribution = get_event_campaign_attribution(request, event)

            pending_order = PendingTicketOrder.objects.create(
                event=event,
                ticket_tier=selected_tier,
                ticket_quantity=ticket_quantity,
                first_name=first_name,
                last_name=last_name,
                email=email,
                phone=phone,
                guest_message=guest_message,
                dietary_restrictions=dietary,

                # Campaign attribution
                campaign_id=attribution.get("campaign_id"),
                campaign_platform=attribution.get("platform", ""),
                campaign_source=attribution.get("source", ""),
                campaign_medium=attribution.get("medium", ""),
            )

            checkout_session = create_checkout_session(
                request,
                pending_order,
            )

            if checkout_session:
                return redirect(checkout_session.url, code=303)

            # Checkout could not be started, so remove the pending order.
            pending_order.delete()

            messages.error(
                request,
                "Could not initiate payment. Please check whether the host "
                "has connected their Stripe payout account, then try again.",
            )
            return redirect('events:public_rsvp', slug=event.slug)

        # FREE RSVP FLOW — retain your existing behavior.
        is_paid = False
        ###########

        if selected_tier and selected_tier.price == 0:
            is_paid = True

        rsvp = RSVP.objects.create(
            event=event,
            ticket_tier=selected_tier,
            ticket_quantity=ticket_quantity,
            is_paid=is_paid,
            email=email,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            status=status,
            plus_ones_allowed=getattr(event, 'max_plus_ones_per_guest', 0),
            plus_ones_count=plus_ones_count,
            guest_message=guest_message,
            dietary_restrictions=dietary,
        )

        if status == 'ATTENDING' and event.allow_plus_ones:
            for i in range(1, plus_ones_count + 1):
                # Accept the current field name and the previous field name.
                guest_name = (
                    request.POST.get(f'guest_name_{i}')
                    or request.POST.get(f'plus_one_name_{i}')
                    or ''
                ).strip()

                if guest_name:
                    RSVPGuest.objects.create(
                        rsvp=rsvp,
                        full_name=guest_name,
                    )
                else:
                    print(
                        f"RSVP DEBUG: Guest name {i} was not received. "
                        f"POST keys: {list(request.POST.keys())}"
                    )

        if selected_tier and is_paid:
            selected_tier.sold_count += 1
            selected_tier.save()

        try:
            send_rsvp_confirmation(rsvp)
        except Exception as e:
            print(f"Email delivery error: {e}")

        messages.success(
            request,
            "Your RSVP and ticket selection have been recorded successfully!",
        )
        return redirect('events:rsvp_confirmed', pk=rsvp.pk)

    return redirect('events:public_rsvp', slug=slug)


@login_required
def event_rsvps_management(request, slug):
    event = get_object_or_404(
        Event,
        slug=slug,
        host=request.user,
    )

    # --- HANDLE POST-EVENT VAULT & GALLERY MANAGEMENT UPDATES ---
    if request.method == 'POST' and 'update_post_event' in request.POST:
        if 'presentation_file' in request.FILES:
            event.presentation_file = request.FILES['presentation_file']

        resource_link = request.POST.get('resource_link')
        if resource_link is not None:
            event.resource_link = resource_link

        event.enable_certificates = (
            request.POST.get('enable_certificates') == 'on'
        )
        event.save()

        # Handle bulk high-resolution post-event image uploads.
        images = request.FILES.getlist('post_event_images')

        for img in images:
            GalleryImage.objects.create(
                event=event,
                image=img,
                is_post_event=True,
                description=request.POST.get('description', ''),
            )

        messages.success(
            request,
            "Post-event vault and deliverables updated successfully!",
        )
        return redirect(
            'events:event_rsvps_management',
            slug=event.slug,
        )

    # Annotate each RSVP with its latest associated ticket-order status.
    latest_order = PendingTicketOrder.objects.filter(
        rsvp_id=OuterRef('pk'),
    ).order_by('-pk')

    
    rsvps = event.rsvps.all().annotate(
        ticket_order_status=Subquery(
            latest_order.values('status')[:1]
        )
    )

    # Preserve the complete RSVP queryset for summary calculations.
    all_rsvps = rsvps
    has_any_rsvps = event.rsvps.exists()

    # Total Responses excludes refunded/cancelled ticket orders.
    if event.is_ticketed:
        total_rsvps = all_rsvps.exclude(
            ticket_order_status__in=['refunded', 'cancelled']
        ).count()
    else:
        total_rsvps = all_rsvps.count()

    # Calculate confirmed ticket quantities independently of table filters.
    attending_rsvps = event.rsvps.filter(status='ATTENDING')

    if event.is_ticketed:
        attending_rsvps = attending_rsvps.filter(is_paid=True).exclude(
            pending_ticket_order__status__in=['refunded', 'cancelled']
        )

    attending_count = attending_rsvps.aggregate(
        total=Sum('ticket_quantity')
    )['total'] or 0

    # Calculate total headcount, including plus-ones.
    extra = event.rsvps.filter(
        status='ATTENDING'
    ).aggregate(
        total=Sum('plus_ones_count')
    )['total'] or 0

    total_headcount = attending_count + extra

    capacity_ratio = (
        total_headcount / event.max_capacity
        if event.max_capacity and event.max_capacity > 0
        else 0
    )

    if (
        event.max_capacity is not None
        and total_headcount >= event.max_capacity
    ):
        capacity_status = 'full'
    elif capacity_ratio >= 0.8:
        capacity_status = 'warning'
    else:
        capacity_status = 'safe'

    # Apply search and status filters ONLY to the table queryset.
    search_query = request.GET.get('q', '').strip()

    if search_query:
        rsvps = rsvps.annotate(
            computed_full_name=Concat(
                'first_name',
                Value(' '),
                'last_name',
            )
        ).filter(
            Q(first_name__icontains=search_query)
            | Q(last_name__icontains=search_query)
            | Q(computed_full_name__icontains=search_query)
            | Q(email__icontains=search_query)
            | Q(phone__icontains=search_query)
        )

    status_filter = request.GET.get('status', '').strip()

    if status_filter:
        if event.is_ticketed and status_filter in ('refunded', 'cancelled'):
            matching_rsvp_ids = PendingTicketOrder.objects.filter(
                rsvp_id__in=event.rsvps.values_list('pk', flat=True),
                status=status_filter,
            ).values_list('rsvp_id', flat=True)

            rsvps = rsvps.filter(pk__in=matching_rsvp_ids)
        else:
            rsvps = rsvps.filter(status=status_filter)

    # This count must match the queryset used to render the table.
    filtered_rsvp_count = rsvps.count()

    context = {
        'event': event,
        'rsvps': rsvps,
        'search_query': search_query,
        'status_filter': status_filter,
        'total_rsvps': total_rsvps,
        'filtered_rsvp_count': filtered_rsvp_count,
        'attending_count': attending_count,
        'total_headcount': total_headcount,
        'capacity_status': capacity_status,
        'post_event_photos': event.gallery_images.filter(
            is_post_event=True
        ),
        'has_any_rsvps': has_any_rsvps,
    }

    return render(
        request,
        'events/event_rsvps_management.html',
        context,
    )

def view_rsvp_detail(request, slug, pk):
    event = get_object_or_404(Event, slug=slug)
    rsvp = get_object_or_404(RSVP, pk=pk, event=event)
    
    context = {
        'event': event,
        'rsvp': rsvp,
        'additional_guests': rsvp.plus_ones.all(),
    }
    return render(request, 'events/view_rsvp_detail.html', context)


def edit_rsvp(request, slug, pk):
    event = get_object_or_404(Event, slug=slug)
    rsvp = get_object_or_404(RSVP, pk=pk, event=event)

    if event.is_past:
        messages.error(request, "This event has concluded. RSVPs are closed.")
        return redirect('events:event_rsvps_management', slug=event.slug)
    
    if request.method == 'POST':
        form = RSVPForm(request.POST, instance=rsvp)
        if form.is_valid():
            updated_rsvp = form.save(commit=False)
            
            # Handle guest count & dynamic plus-one names correctly using RSVPGuest
            if event.allow_plus_ones and event.max_plus_ones_per_guest > 0:
                try:
                    guest_count = int(request.POST.get('guest_count', 0))
                except ValueError:
                    guest_count = 0
                
                if guest_count > event.max_plus_ones_per_guest:
                    guest_count = event.max_plus_ones_per_guest
                if guest_count < 0:
                    guest_count = 0
                
                updated_rsvp.plus_ones_count = guest_count
                updated_rsvp.save()
                
                # Clear and recreate plus-ones using RSVPGuest model
                rsvp.plus_ones.all().delete()
                for i in range(1, guest_count + 1):
                    guest_name = request.POST.get(f'plus_one_name_{i}')
                    if guest_name:
                        RSVPGuest.objects.create(rsvp=updated_rsvp, full_name=guest_name)
            else:
                updated_rsvp.save()
                
            return redirect('events:view_rsvp_detail', slug=event.slug, pk=rsvp.pk)
    else:
        form = RSVPForm(instance=rsvp)
        
    context = {
        'event': event,
        'rsvp': rsvp,
        'form': form,
    }
    return render(request, 'events/edit_rsvp.html', context)

# CLONE EXISTING EVENT
@login_required
def clone_event(request, slug):
    original_event = get_object_or_404(Event, slug=slug)
    
    cloned_event = original_event
    cloned_event.pk = None
    cloned_event.id = None
    cloned_event.title = f"Copy of {original_event.title}"
    
    # Dynamically find and shift ALL Date and DateTime fields on the model forward
    now = timezone.now()
    today = now.date()
    
    for field in Event._meta.get_fields():
        if field.get_internal_type() in ['DateField', 'DateTimeField']:
            field_name = field.name
            current_val = getattr(cloned_event, field_name, None)
            if current_val:
                if hasattr(current_val, 'hour'):  # DateTimeField
                    setattr(cloned_event, field_name, now + timedelta(days=7))
                else:  # DateField
                    setattr(cloned_event, field_name, today + timedelta(days=7))
                
    base_slug = slugify(cloned_event.title)
    unique_slug = base_slug
    counter = 1
    while Event.objects.filter(slug=unique_slug).exists():
        unique_slug = f"{base_slug}-{counter}"
        counter += 1
    cloned_event.slug = unique_slug
    
    cloned_event.save()
    
    messages.success(request, f"Event successfully cloned. You can now update the details.")
    return redirect(f'/dashboard/events/{cloned_event.slug}/edit/')

def download_ics(request, slug):
    event = get_object_or_404(Event, slug=slug)
    
    start_date = event.event_date.strftime('%Y%m%d') if hasattr(event.event_date, 'strftime') else str(event.event_date)
    
    ics_content = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Manage My Event Platform//EN
BEGIN:VEVENT
SUMMARY:{event.title}
DESCRIPTION:{getattr(event, 'description', '')}
LOCATION:{getattr(event, 'location_name', '')}
DTSTART:{start_date}
DTEND:{start_date}
END:VEVENT
END:VCALENDAR"""
    
    response = HttpResponse(ics_content, content_type='text/calendar')
    response['Content-Disposition'] = f'attachment; filename="{event.slug}.ics"'
    return response


def rsvp_confirmed_view(request, pk):
    rsvp = get_object_or_404(RSVP, pk=pk)
    event = rsvp.event

    session_id = request.GET.get('session_id')

    # Verify the Stripe session when one is provided.
    if session_id:
        try:
            stripe.api_key = settings.STRIPE_SECRET_KEY
            session = stripe.checkout.Session.retrieve(session_id)

            # Verify that the session belongs to this RSVP.
            if session.client_reference_id != str(rsvp.pk):
                messages.error(request, "Invalid payment session.")
                return redirect('events:public_rsvp', slug=event.slug)

            if session.payment_status == 'paid':
                if not rsvp.is_paid:
                    rsvp.is_paid = True
                    rsvp.save(update_fields=['is_paid', 'updated_at'])

                    # Increase sold count only when marking this RSVP paid.
                    if rsvp.ticket_tier:
                        rsvp.ticket_tier.sold_count += rsvp.ticket_quantity
                        rsvp.ticket_tier.save(
                            update_fields=['sold_count']
                        )
            else:
                messages.error(request, "Payment has not been completed.")
                return redirect('events:public_rsvp', slug=event.slug)

        except Exception as e:
            print(f"Stripe Session Verification Error: {e}")
            messages.error(request, "Unable to verify your payment.")
            return redirect('events:public_rsvp', slug=event.slug)

    # Create any missing individual tickets for paid orders.
    # This also handles orders that were already marked paid.
    if rsvp.is_paid and event.enable_qr_checkins:
        for ticket_number in range(1, rsvp.ticket_quantity + 1):
            IndividualTicket.objects.get_or_create(
                rsvp=rsvp,
                ticket_number=ticket_number,
            )

    # Preserve your existing session authorization.
    request.session[f'rsvp_verified_{event.pk}'] = rsvp.pk

    return render(
        request,
        'events/rsvp_confirmed.html',
        {'rsvp': rsvp, 'event': event},
    )

# @login_required
# def verify_checkin(request, slug, token):
#     event = get_object_or_404(Event, slug=slug, host=request.user)
    
#     if not event.enable_qr_checkins:
#         messages.error(request, "QR check-ins are not enabled for this event.")
#         #return redirect('events:dashboard', slug=event.slug)
#         return redirect('events:door_dashboard', slug=event.slug)
        
#     rsvp = get_object_or_404(RSVP, event=event, checkin_token=token)
    
#     if not rsvp.checked_in:
#         rsvp.checked_in = True
#         rsvp.checked_in_at = timezone.now()
#         rsvp.save()
#         messages.success(request, f"Checked in: {rsvp.first_name} {rsvp.last_name}!")
#     else:
#         messages.warning(request, f"Guest already checked in at {rsvp.checked_in_at.strftime('%H:%M')}.")
        
#     return redirect('events:door_dashboard', slug=event.slug)

@login_required
def verify_checkin(request, slug, token):
    event = get_object_or_404(Event, slug=slug, host=request.user)
    rsvp = get_object_or_404(RSVP, event=event, checkin_token=token)

    if not event.enable_qr_checkins:
        messages.warning(request, "QR check-ins are disabled, but host override was applied.")

    # Check if host explicitly wants to undo check-in (via query param or toggle button)
    is_undo = request.GET.get("undo") == "true"

    if is_undo and rsvp.checked_in:
        # Host explicitly undoing check-in from dashboard link
        rsvp.checked_in = False
        rsvp.checked_in_at = None
        messages.info(request, f"Check-in undone for: {rsvp.first_name} {rsvp.last_name}")
    else:
        # Standard scan or manual check-in click
        if not rsvp.checked_in:
            rsvp.checked_in = True
            rsvp.checked_in_at = timezone.now()
            messages.success(request, f"Checked in: {rsvp.first_name} {rsvp.last_name}!")
        else:
            # Re-scanning an already checked-in guest
            checkin_time = (
                rsvp.checked_in_at.strftime("%H:%M")
                if rsvp.checked_in_at
                else "earlier"
            )
            messages.warning(request, f"{rsvp.first_name} {rsvp.last_name} was already checked in at {checkin_time}.")

    rsvp.save(update_fields=["checked_in", "checked_in_at"])

    return redirect("events:door_dashboard", slug=event.slug)


def event_door_dashboard(request, slug):
    event = get_object_or_404(Event, slug=slug, host=request.user)
    #rsvps = event.rsvps.all()
    rsvps = event.rsvps.select_related('ticket_tier').all()

    search_query = request.GET.get('q', '').strip()
    if search_query:
        rsvps = rsvps.annotate(
            computed_full_name=Concat('first_name', Value(' '), 'last_name')
        ).filter(
            Q(first_name__icontains=search_query) | 
            Q(last_name__icontains=search_query) | 
            Q(computed_full_name__icontains=search_query) |
            Q(email__icontains=search_query) |
            Q(phone__icontains=search_query)
        )

    status_filter = request.GET.get('status', '').strip()
    if status_filter == 'ATTENDING':
        rsvps = rsvps.filter(status='ATTENDING')
    elif status_filter == 'DECLINED':
        rsvps = rsvps.filter(status='DECLINED')
    elif status_filter == 'checked_in':
        rsvps = rsvps.filter(checked_in=True)
    elif status_filter == 'pending':
        rsvps = rsvps.filter(checked_in=False)

    context = {
        'event': event,
        'rsvps': rsvps,
        'total_rsvps': event.rsvps.count(),
        'checked_in_count': event.rsvps.filter(checked_in=True).count(),
        'pending_count': event.rsvps.filter(checked_in=False).count(),
        'search_query': search_query,
        'status_filter': status_filter,
    }
    return render(request, 'events/door_dashboard.html', context)

def features_view(request):
    return render(request, 'events/features_page.html')

def event_cookies (request):
    return render(request, 'events/legal/cookies.html')

def event_privacy(request):
    return render(request, 'events/legal/privacy.html')

def event_terms(request):
    return render(request, 'events/legal/terms.html')
# 
def conference(request):
    return render(request, 'events/solutions/conference.html')

def gig(request):
    return render(request, 'events/solutions/gig.html')

def live_stream(request):
    return render(request, 'events/solutions/live_stream.html')

def private_event(request):
    return render(request, 'events/solutions/private_event.html')

def summit(request):
    return render(request, 'events/solutions/summit.html')

def architecture_theme_engines(request):
    return render(request, 'events/architectures/theme_engines.html')

def architecture_split_screen(request):
    return render(request, 'events/architectures/split_screen_layouts.html')

def architecture_dynamic_rsvp(request):
    return render(request, 'events/architectures/dynamic_rsvp_pipeline.html')

def architecture_glassmorphic_ui(request):
    return render(request, 'events/architectures/glassmorphic_ui_components.html')

def architecture_changelog_releases(request):
    return render(request, 'events/architectures/changelog_and_releases.html')

def platform_architecture(request):
    return render(request, 'events/resources/platform_architecture.html')

def event_api(request):
    return render(request, 'events/resources/event_api.html')

def security_compliance(request):
    return render(request, 'events/resources/security_compliance.html')

def organizer_community(request):
    return render(request, 'events/resources/organizer_community.html')

def system_status(request):
    return render(request, 'events/resources/system_status.html')

@login_required
def manage_event_tiers(request, slug):
    event = get_object_or_404(Event, slug=slug, host=request.user)
    
    if request.method == 'POST':
        # Use the lightweight form instead of the full EventForm
        form = EventTicketingForm(request.POST, instance=event)
        formset = TicketTierFormSet(request.POST, instance=event)
        
        if form.is_valid() and formset.is_valid():
            form.save()
            formset.save()
            messages.success(request, "Ticket tiers updated successfully!")
            return redirect('events:event_rsvps_management', slug=event.slug)
        else:
            print("Form errors:", form.errors)
            print("Formset errors:", formset.errors)
    else:
        form = EventTicketingForm(instance=event)
        formset = TicketTierFormSet(instance=event)
        
    context = {
        'event': event,
        'form': form,
        'formset': formset,
    }
    return render(request, 'events/manage_event_tiers.html', context)

@login_required
def export_rsvps_csv(request, slug):
    event = get_object_or_404(Event, slug=slug, host=request.user)
    rsvps = event.rsvps.all()

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{event.slug}_rsvps.csv"'

    writer = csv.writer(response)
    writer.writerow(['First Name', 'Last Name', 'Email', 'Status', 'Updated At'])

    for rsvp in rsvps:
        writer.writerow([
            rsvp.first_name,
            rsvp.last_name,
            rsvp.email or 'N/A',
            rsvp.status,
            rsvp.updated_at
        ])

    return response

# def bento_view(request):
#     return render(request, 'events/layouts/bento.html')

# def editorial_minimalist_view(request):
#     return render(request, 'events/layouts/editorial_and_minimalist.html')

# def neon_and_dark_view(request):
#     return render(request, 'events/layouts/immersive_neon_and_dark_mode.html')

# def split_screen_conference_view(request):
#     return render(request, 'events/layouts/split_screen_conference.html')

# def story_teller_view(request):
#     return render(request, 'events/layouts/story_teller_festival.html')



# CONNECT
@login_required
def connect_stripe_onboarding(request):
    profile, created = OrganizerProfile.objects.get_or_create(user=request.user)

    try:
        if not profile.stripe_account_id:
            profile.stripe_account_id = create_stripe_express_account(request.user)
            profile.save()

        refresh_url = request.build_absolute_uri('/events/stripe/connect/')
        return_url = request.build_absolute_uri('/events/stripe/return/')
        
        onboarding_url = generate_account_link(profile.stripe_account_id, refresh_url, return_url)
        return redirect(onboarding_url)

    except stripe.error.StripeError as e:
        # TEMPORARY TRACE: This will show you the exact reason Stripe is failing on your screen
        print(f"FULL STRIPE ERROR: {str(e)}")
        raise e

@login_required
def stripe_onboarding_return(request):
    profile = get_object_or_404(OrganizerProfile, user=request.user)

    if profile.stripe_account_id:
        is_complete = check_stripe_onboarding_status(profile.stripe_account_id)
        
        if is_complete:
            profile.stripe_onboarding_complete = True
            profile.save()
            messages.success(request, "Your payout account was connected successfully!")
        else:
            messages.warning(request, "Onboarding was not completed. Please try again when you're ready.")

    return redirect('events:dashboard')


@login_required
def stripe_dashboard_redirect(request):
    profile = get_object_or_404(OrganizerProfile, user=request.user)
    
    if not profile.stripe_account_id or not profile.stripe_onboarding_complete:
        messages.warning(request, "Please connect your payout account first.")
        return redirect('events:connect_stripe_onboarding')

    try:
        dashboard_url = generate_stripe_login_link(profile.stripe_account_id)
        return redirect(dashboard_url)
    except stripe.error.StripeError as e:
        messages.error(request, f"Could not access Stripe dashboard: {e.user_message}")
        return redirect('events:dashboard')


@login_required
def stripe_connect_return(request):
    organizer_profile, created = OrganizerProfile.objects.get_or_create(user=request.user)
    
    if organizer_profile.stripe_account_id:
        try:
            # Query Stripe to verify completion
            account = stripe.Account.retrieve(organizer_profile.stripe_account_id)
            
            if account.details_submitted:
                organizer_profile.stripe_onboarding_complete = True
                organizer_profile.save()
                messages.success(request, "Payout account successfully connected!")
            else:
                messages.warning(request, "Onboarding was not completed yet. Please finish the setup.")
        except stripe.error.StripeError as e:
            messages.error(request, f"Stripe error: {e}")
    else:
        messages.error(request, "Organizer Stripe Account ID missing.")
        
    return redirect('events:dashboard')

@login_required
def stripe_dashboard_redirect(request):
    organizer_profile, created = OrganizerProfile.objects.get_or_create(user=request.user)

    if organizer_profile.stripe_account_id:
        try:
            login_link = stripe.Account.create_login_link(organizer_profile.stripe_account_id)
            return redirect(login_link.url)
        except stripe.error.StripeError as e:
            messages.error(request, f"Stripe error: {e}")
    else:
        messages.error(request, "No connected Stripe account found.")
        
    return redirect('events:dashboard')

@login_required
def event_analytics_dashboard(request, slug):
    """
    Host-only event analytics.

    Ticket inventory:
        Uses TicketTier.sold_count and reserved_count.

    Paid ticket orders:
        Uses PendingTicketOrder.status. Refunded orders are
        excluded from currently paid orders.

    Estimated sales:
        Uses current ticket-tier prices and quantities on paid
        orders. This is an estimate, not a historical Stripe ledger.

    RSVP analytics:
        Keeps RSVP record counts separate from ticket inventory.
        Free-event headcount includes confirmed RSVPGuest records.

    Marketing attribution:
        Reports tracked visits, attributed ticket-order records,
        currently paid attributed orders, and estimated revenue.
    """
    from datetime import timedelta

    from django.db.models import Count, Sum

    from .models import CampaignVisit
    from marketing_studio.models import CampaignDraft

    event = get_object_or_404(
        Event.objects.select_related("host"),
        slug=slug,
        host=request.user,
    )

    now = timezone.now()
    currency = "USD"
    zero_money = Decimal("0.00")

    # ---------------------------------------------------------
    # TICKET TIERS
    # Fetch once and reuse throughout the dashboard.
    # ---------------------------------------------------------

    ticket_tiers = list(
        TicketTier.objects.filter(
            event=event,
        ).order_by("price", "name")
    )

    has_ticket_tiers = bool(ticket_tiers)

    # ---------------------------------------------------------
    # RSVP RECORD COUNTS
    # These are RSVP records, not ticket quantities.
    # ---------------------------------------------------------

    rsvps = RSVP.objects.filter(event=event)

    total_rsvps = rsvps.count()

    confirmed_rsvps = rsvps.filter(
        status="ATTENDING",
    ).count()

    pending_rsvps = rsvps.filter(
        status="PENDING",
    ).count()

    declined_rsvps = rsvps.filter(
        status="DECLINED",
    ).count()

    checked_in_guests = rsvps.filter(
        checked_in=True,
    ).count()

    # RSVPGuest headcount applies to the free-RSVP presentation.
    # Ticketed events must use ticket quantities instead.
    if has_ticket_tiers:
        total_headcount = 0
    else:
        confirmed_rsvp_records = rsvps.filter(
            status="ATTENDING",
        )

        total_headcount = (
            confirmed_rsvps
            + RSVPGuest.objects.filter(
                rsvp__in=confirmed_rsvp_records,
            ).count()
        )

    # ---------------------------------------------------------
    # TICKET ORDERS
    # ---------------------------------------------------------

    ticket_orders = PendingTicketOrder.objects.filter(
        event=event,
    ).select_related("ticket_tier")

    paid_ticket_orders = ticket_orders.filter(
        status=PendingTicketOrder.STATUS_PAID,
    )

    unpaid_ticket_orders = ticket_orders.filter(
        status=PendingTicketOrder.STATUS_PENDING,
    )

    paid_orders = paid_ticket_orders.count()
    unpaid_order_count = unpaid_ticket_orders.count()

    # Quantities from currently paid orders, grouped by tier.
    # This is intentionally separate from TicketTier.sold_count.
    paid_quantity_by_tier = dict(
        paid_ticket_orders.values(
            "ticket_tier_id",
        ).annotate(
            quantity=Sum("ticket_quantity"),
        ).values_list(
            "ticket_tier_id",
            "quantity",
        )
    )

    pending_ticket_quantity = (
        unpaid_ticket_orders.aggregate(
            quantity=Sum("ticket_quantity"),
        )["quantity"]
        or 0
    )

    # ---------------------------------------------------------
    # MARKETING CAMPAIGN ATTRIBUTION
    # ---------------------------------------------------------

    campaign_metrics = {}

    def get_campaign_metric(campaign_id, platform):
        normalized_platform = platform or "unknown"
        key = (campaign_id, normalized_platform)

        if key not in campaign_metrics:
            campaign_metrics[key] = {
                "campaign_id": campaign_id,
                "platform": normalized_platform,
                "source": "",
                "medium": "",
                "campaign_name": f"Campaign #{campaign_id}",
                "tracked_visits": 0,
                "attributed_rsvps": 0,
                "paid_orders": 0,
                "estimated_revenue": zero_money,
            }

        return campaign_metrics[key]

    # Tracked visits for this event.
    campaign_visits = CampaignVisit.objects.filter(
        event=event,
    ).values(
        "campaign_id",
        "platform",
        "source",
        "medium",
    )

    for visit in campaign_visits:
        metric = get_campaign_metric(
            visit["campaign_id"],
            visit["platform"],
        )

        metric["tracked_visits"] += 1

        if visit["source"]:
            metric["source"] = visit["source"]

        if visit["medium"]:
            metric["medium"] = visit["medium"]

    # Attributed ticket-order records.
    # Keep the existing "attributed_rsvps" key for template
    # compatibility; each increment here represents an order row.
    attributed_ticket_orders = ticket_orders.filter(
        campaign_id__isnull=False,
    ).values(
        "campaign_id",
        "campaign_platform",
        "campaign_source",
        "campaign_medium",
    )

    for order_row in attributed_ticket_orders:
        metric = get_campaign_metric(
            order_row["campaign_id"],
            order_row["campaign_platform"],
        )

        metric["attributed_rsvps"] += 1

        if order_row["campaign_source"]:
            metric["source"] = order_row["campaign_source"]

        if order_row["campaign_medium"]:
            metric["medium"] = order_row["campaign_medium"]

    # Currently paid, attributed orders and estimated revenue.
    attributed_paid_orders = paid_ticket_orders.filter(
        campaign_id__isnull=False,
    )

    for order in attributed_paid_orders:
        metric = get_campaign_metric(
            order.campaign_id,
            order.campaign_platform,
        )

        metric["paid_orders"] += 1

        tier_price = (
            order.ticket_tier.price
            if order.ticket_tier
            else zero_money
        ) or zero_money

        quantity = order.ticket_quantity or 0

        metric["estimated_revenue"] += tier_price * quantity

        if order.campaign_source:
            metric["source"] = order.campaign_source

        if order.campaign_medium:
            metric["medium"] = order.campaign_medium

    # Resolve campaign names for display.
    campaign_ids = {
        campaign_id
        for campaign_id, platform in campaign_metrics
    }

    campaign_drafts = CampaignDraft.objects.filter(
        pk__in=campaign_ids,
        event=event,
        host=request.user,
    )

    campaign_names = {
        draft.pk: draft.name
        for draft in campaign_drafts
    }

    platform_labels = dict(CampaignDraft.PLATFORM_CHOICES)

    campaign_performance = []

    for metric in campaign_metrics.values():
        campaign_id = metric["campaign_id"]
        platform = metric["platform"]

        metric["campaign_name"] = campaign_names.get(
            campaign_id,
            f"Campaign #{campaign_id}",
        )

        metric["platform_label"] = platform_labels.get(
            platform,
            platform.replace("_", " ").title(),
        )

        metric["estimated_revenue"] = metric[
            "estimated_revenue"
        ].quantize(Decimal("0.01"))

        campaign_performance.append(metric)

    campaign_performance.sort(
        key=lambda row: (
            row["tracked_visits"],
            row["attributed_rsvps"],
            row["paid_orders"],
        ),
        reverse=True,
    )

    # Existing Chart.js chart: bars represent tracked visits.
    channel_attribution_labels = [
        f'{row["campaign_name"]} · {row["platform_label"]}'
        for row in campaign_performance
    ]

    # channel_attribution_data = [
    #     row["tracked_visits"]
    #     for row in campaign_performance
    # ]

    channel_attribution_data = [
        row["attributed_rsvps"] for row in campaign_performance
    ]

    # ---------------------------------------------------------
    # ESTIMATED SALES FROM CURRENTLY PAID ORDERS
    # ---------------------------------------------------------

    estimated_gross_sales = zero_money

    for order in paid_ticket_orders:
        tier_price = (
            order.ticket_tier.price
            if order.ticket_tier
            else zero_money
        ) or zero_money

        quantity = order.ticket_quantity or 0

        estimated_gross_sales += tier_price * quantity

    estimated_gross_sales = estimated_gross_sales.quantize(
        Decimal("0.01")
    )

    # ---------------------------------------------------------
    # PLATFORM FEE ESTIMATE
    # ---------------------------------------------------------

    active_fee = PlatformFee.objects.filter(
        active=True,
        applies_to="TICKET",
    ).order_by(
        "-updated_at",
        "-created_at",
    ).first()

    configured_fee_percent = (
        active_fee.percentage
        if active_fee
        else None
    )

    if configured_fee_percent is not None:
        estimated_platform_fee = (
            estimated_gross_sales
            * configured_fee_percent
            / Decimal("100")
        ).quantize(Decimal("0.01"))

        estimated_after_platform_fee = (
            estimated_gross_sales - estimated_platform_fee
        ).quantize(Decimal("0.01"))
    else:
        estimated_platform_fee = None
        estimated_after_platform_fee = None

    # ---------------------------------------------------------
    # SALES BY TICKET TIER
    # ---------------------------------------------------------

    tier_rows = []

    for tier in ticket_tiers:
        tier_tickets_sold = tier.sold_count or 0

        tier_paid_quantity = (
            paid_quantity_by_tier.get(tier.pk) or 0
        )

        tier_estimated_sales = (
            (tier.price or zero_money) * tier_paid_quantity
        ).quantize(Decimal("0.01"))

        tier_capacity = tier.capacity or 0
        tier_reserved = tier.reserved_count or 0

        remaining_capacity = max(
            tier_capacity - tier_tickets_sold - tier_reserved,
            0,
        )

        capacity_percent = (
            min(
                round(
                    tier_tickets_sold / tier_capacity * 100,
                    1,
                ),
                100,
            )
            if tier_capacity
            else 0
        )

        tier_rows.append({
            "name": tier.name,
            "price": tier.price,
            "capacity": tier_capacity,
            "tickets_sold": tier_tickets_sold,
            "remaining_capacity": remaining_capacity,
            "capacity_percent": capacity_percent,
            "estimated_sales": tier_estimated_sales,
        })

    # Calculate inventory totals once, after building tier rows.
    total_ticket_capacity = sum(
        row["capacity"]
        for row in tier_rows
    )

    tickets_sold = sum(
        row["tickets_sold"]
        for row in tier_rows
    )

    # ---------------------------------------------------------
    # DAILY REGISTRATION TREND
    # Registration records, not ticket sales.
    # ---------------------------------------------------------

    trend_start = now - timedelta(days=29)

    daily_registrations = (
        rsvps.filter(
            created_at__gte=trend_start,
        )
        .extra(
            select={"day": "DATE(created_at)"},
        )
        .values("day")
        .annotate(
            registrations=Count("id"),
        )
        .order_by("day")
    )

    registration_by_day = {
        str(row["day"]): row["registrations"]
        for row in daily_registrations
    }

    trend_labels = []
    registration_trend = []

    for day_offset in range(30):
        day = (trend_start + timedelta(days=day_offset)).date()
        day_key = str(day)

        trend_labels.append(day.strftime("%b %d"))
        registration_trend.append(
            registration_by_day.get(day_key, 0)
        )

    # ---------------------------------------------------------
    # HOURLY GATE CHECK-IN VELOCITY
    # Uses actual recorded check-in timestamps.
    # ---------------------------------------------------------

    recorded_checkins = rsvps.filter(
        checked_in=True,
        checked_in_at__isnull=False,
    ).values_list(
        "checked_in_at",
        flat=True,
    )

    hourly_checkin_counts = {}

    for checkin_timestamp in recorded_checkins:
        local_checkin_time = timezone.localtime(
            checkin_timestamp,
        )
        hour = local_checkin_time.hour

        hourly_checkin_counts[hour] = (
            hourly_checkin_counts.get(hour, 0) + 1
        )

    sorted_checkin_hours = sorted(hourly_checkin_counts)

    hourly_checkin_labels = [
        f"{hour % 12 or 12} {'AM' if hour < 12 else 'PM'}"
        for hour in sorted_checkin_hours
    ]

    hourly_checkin_data = [
        hourly_checkin_counts[hour]
        for hour in sorted_checkin_hours
    ]

    # ---------------------------------------------------------
    # REFUNDS AND FINANCIAL RECONCILIATION AVAILABILITY
    # ---------------------------------------------------------

    refunded_ticket_orders = ticket_orders.filter(
        status=PendingTicketOrder.STATUS_REFUNDED,
    )

    refunded_ticket_quantity = (
        refunded_ticket_orders.aggregate(
            quantity=Sum("ticket_quantity"),
        )["quantity"]
        or 0
    )

    financial_data = {
        "actual_gross_sales": None,
        "refunds_processed": None,
        "actual_platform_fees": None,
        "stripe_processing_fees": None,
        "actual_net_proceeds": None,
        "actual_payouts": None,
        "payout_status": "Unavailable from current order records",
        "transaction_history_available": False,
        "refund_tracking_available": True,
        "refunded_ticket_order_count": refunded_ticket_orders.count(),
        "failed_payment_tracking_available": False,
        "processing_fee_tracking_available": False,
        "payout_tracking_available": False,
    }

    context = {
        "event": event,
        "currency": currency,

        # RSVP records and attendance
        "total_rsvps": total_rsvps,
        "confirmed_rsvps": confirmed_rsvps,
        "pending_rsvps": pending_rsvps,
        "declined_rsvps": declined_rsvps,
        "checked_in_guests": checked_in_guests,
        "total_headcount": total_headcount,

        # Ticket orders and inventory
        "paid_orders": paid_orders,
        "unpaid_ticket_orders": unpaid_order_count,
        "tickets_sold": tickets_sold,
        "total_ticket_capacity": total_ticket_capacity,
        "pending_ticket_quantity": pending_ticket_quantity,
        "refunded_ticket_quantity": refunded_ticket_quantity,
        "has_ticket_tiers": has_ticket_tiers,

        # Estimated financial figures
        "estimated_gross_sales": estimated_gross_sales,
        "configured_fee_percent": configured_fee_percent,
        "estimated_platform_fee": estimated_platform_fee,
        "estimated_after_platform_fee": estimated_after_platform_fee,

        # Ticket tier breakdown and registration chart
        "tier_rows": tier_rows,
        "trend_labels": trend_labels,
        "registration_trend": registration_trend,

        # Marketing attribution
        "campaign_performance": campaign_performance,
        "channel_attribution_labels": channel_attribution_labels,
        "channel_attribution_data": channel_attribution_data,

        # Financial reconciliation
        "financial_data": financial_data,

        # Hourly gate check-in chart
        "hourly_checkin_labels": hourly_checkin_labels,
        "hourly_checkin_data": hourly_checkin_data,
    }

    return render(
        request,
        "events/analytics_dashboard.html",
        context,
    )


def get_event_lifecycle_context(event):
    """Helper to bundle shared lifecycle context for both host and public views."""
    return {
        'event': event,
        'ticket_tiers': event.ticket_tiers.all() if event.is_ticketed else None,
        'feedbacks': event.feedbacks.all() if event.is_past else None,
    }

def post_event_public_view(request, event_slug):
    """
    Public-facing landing page for guests.
    Transitions dynamically between RSVP registration and the post-event community hub.
    """
    event = get_object_or_404(Event, slug=event_slug)
    context = get_event_lifecycle_context(event)
    return render(request, 'events/public_rsvp.html', context)

@login_required
def post_event_detail_view(request, event_slug):
    """
    Organizer-facing dashboard view.
    Allows the host to manage the event and view the exact post-event loop.
    """
    event = get_object_or_404(Event, slug=event_slug, host=request.user)
    context = get_event_lifecycle_context(event)
    return render(request, 'events/event_detail.html', context)

# @login_required
def submit_feedback(request, slug):
    event = get_object_or_404(Event, slug=slug)
    
    # Retrieve the guest's verified RSVP from the session
    rsvp_id = request.session.get(f'rsvp_verified_{event.pk}')
    user_rsvp = event.rsvps.filter(pk=rsvp_id, status='ATTENDING').first() if rsvp_id else None
    
    if not user_rsvp:
        messages.error(request, "You must be a confirmed attendee to submit feedback.")
        referer = request.META.get('HTTP_REFERER')
        return redirect(referer) if referer else redirect('events:event_detail', slug=event.slug)

    # Prevent duplicate feedback from the same RSVP
    if event.feedbacks.filter(rsvp=user_rsvp).exists():
        messages.error(request, "You have already submitted feedback for this event.")
        referer = request.META.get('HTTP_REFERER')
        return redirect(referer) if referer else redirect('events:event_detail', slug=event.slug)

    if request.method == 'POST':
        form = FeedbackForm(request.POST)
        if form.is_valid():
            feedback = form.save(commit=False)
            feedback.event = event
            feedback.rsvp = user_rsvp  # Link directly to their verified RSVP
            if request.user.is_authenticated:
                feedback.user = request.user  # Attach user account if logged in, otherwise leaves it null for public guests
            feedback.save()
            messages.success(request, "Thank you for your feedback!")
            
    referer = request.META.get('HTTP_REFERER')
    if referer:
        return redirect(referer)
    return redirect('events:event_detail', slug=event.slug)



def download_certificate(request, event_slug):
    """
    Generates and streams an official Certificate of Attendance PDF.
    Allows the event host to download/test, and verifies checked-in attendees.
    """
    event = get_object_or_404(Event, slug=event_slug)
    
    # 1. Ensure certificates are enabled for this event
    if not event.enable_certificates:
        raise Http404("Certificates are not enabled for this event.")
        
    rsvp = None
    
    # 2. Allow Host to bypass check for testing/preview purposes
    if request.user.is_authenticated and event.host == request.user:
        # Grab the first checked-in RSVP for preview, or create a mock name if none exist
        rsvp = RSVP.objects.filter(event=event, checked_in=True).first()
        if not rsvp:
            # Fallback for host testing if no attendees are checked in yet
            rsvp = type('obj', (object,), {'full_name': request.user.get_full_name() or request.user.username})
    else:
        # 3. Standard Attendee Lookup for regular users
        if request.user.is_authenticated:
            rsvp = RSVP.objects.filter(event=event, email=request.user.email, checked_in=True).first()
        
        # Fallback to session-verified attendee email if not logged in
        if not rsvp:
            verified_email = request.session.get(f'verified_rsvp_{event.slug}')
            if verified_email:
                rsvp = RSVP.objects.filter(event=event, email=verified_email, checked_in=True).first()
                
    if not rsvp:
        return HttpResponse(
            f"Access Denied: No checked-in RSVP found for email '{request.user.email if request.user.is_authenticated else 'Guest'}'. Make sure your RSVP is marked as checked-in in the admin dashboard.", 
            status=403
        )
        
    attendee_name = getattr(rsvp, 'full_name', f"{getattr(rsvp, 'first_name', '')} {getattr(rsvp, 'last_name', '')}".strip() or "Valued Attendee")

    # 4. Create the PDF buffer using landscape Letter size
    buffer = io.BytesIO()
    p = canvas.Canvas(buffer, pagesize=landscape(letter))
    width, height = landscape(letter)
    
    # --- Certificate Styling & Borders ---
    p.setStrokeColor(colors.HexColor("#0f172a"))
    p.setLineWidth(4)
    p.rect(30, 30, width - 60, height - 60)
    
    p.setStrokeColor(colors.HexColor("#3b82f6"))
    p.setLineWidth(1)
    p.rect(40, 40, width - 80, height - 80)
    
    # --- Header Typography ---
    p.setFillColor(colors.HexColor("#0f172a"))
    p.setFont("Helvetica-Bold", 26)
    p.drawCentredString(width / 2.0, height - 110, "CERTIFICATE OF ATTENDANCE")
    
    p.setFont("Helvetica", 13)
    p.setFillColor(colors.HexColor("#64748b"))
    p.drawCentredString(width / 2.0, height - 145, "This is proudly presented to")
    
    # --- Attendee Name ---
    p.setFont("Helvetica-Bold", 30)
    p.setFillColor(colors.HexColor("#2563eb"))
    p.drawCentredString(width / 2.0, height - 200, attendee_name)
    
    # --- Subtitle & Event Context ---
    p.setFont("Helvetica", 13)
    p.setFillColor(colors.HexColor("#334155"))
    p.drawCentredString(
        width / 2.0, 
        height - 250, 
        "for successfully participating and completing the gathering for"
    )
    
    # --- Event Title ---
    p.setFont("Helvetica-Bold", 18)
    p.setFillColor(colors.HexColor("#0f172a"))
    p.drawCentredString(width / 2.0, height - 290, event.title)
    
    # --- Footer Details ---
    p.setFont("Helvetica", 11)
    p.setFillColor(colors.HexColor("#64748b"))
    date_str = event.event_date.strftime("%B %d, %Y") if event.event_date else "2026"
    p.drawString(70, 70, f"Date Issued: {date_str}")
    
    p.drawRightString(width - 70, 70, "Authorized Host Signature")
    p.setStrokeColor(colors.HexColor("#cbd5e1"))
    p.line(width - 240, 90, width - 70, 90)
    
    p.showPage()
    p.save()
    
    # 5. Stream Response to Browser
    buffer.seek(0)
    filename = f"Certificate_{event.slug}_{attendee_name.replace(' ', '_')}.pdf"
    response = HttpResponse(buffer, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response

def google_pass_view(request, token):
    """
    Supports both new IndividualTicket tokens and existing RSVP tokens.
    """
    individual_ticket = IndividualTicket.objects.select_related(
        'rsvp__event'
    ).filter(checkin_token=token).first()

    if individual_ticket:
        rsvp = individual_ticket.rsvp
        google_wallet_url = generate_google_wallet_link(
            rsvp,
            ticket=individual_ticket,
        )
    else:
        rsvp = get_object_or_404(RSVP, checkin_token=token)
        google_wallet_url = generate_google_wallet_link(rsvp)

    return redirect(google_wallet_url)


def web_ticket_view(request, token):
    """
    Displays an individual ticket when its token is supplied,
    while retaining support for existing RSVP-level tickets.
    """
    individual_ticket = IndividualTicket.objects.select_related(
        'rsvp__event'
    ).filter(checkin_token=token).first()

    if individual_ticket:
        rsvp = individual_ticket.rsvp
    else:
        rsvp = get_object_or_404(RSVP, checkin_token=token)

    event = rsvp.event

    # Use the token belonging to the specific ticket when available.
    checkin_token = (
        individual_ticket.checkin_token
        if individual_ticket
        else rsvp.checkin_token
    )

    verify_url = request.build_absolute_uri(
        reverse(
            'events:verify_checkin',
            kwargs={
                'slug': event.slug,
                'token': checkin_token,
            },
        )
    )

    ticket_data = get_ticket_context(
        rsvp,
        ticket=individual_ticket,
    )

    context = {
        'rsvp': rsvp,
        'event': event,
        'verify_url': verify_url,
        'ticket_data': ticket_data,
        'individual_ticket': individual_ticket,
    }

    return render(request, 'events/web_ticket.html', context)


def web_ticket_view(request, token):
    # First, look for the individual ticket whose token is in the URL.
    individual_ticket = IndividualTicket.objects.select_related(
        'rsvp__event'
    ).filter(checkin_token=token).first()

    if individual_ticket:
        rsvp = individual_ticket.rsvp
        checkin_token = individual_ticket.checkin_token
    else:
        # Backward compatibility for older RSVP-level ticket links.
        rsvp = get_object_or_404(RSVP, checkin_token=token)
        checkin_token = rsvp.checkin_token

    event = rsvp.event

    verify_url = request.build_absolute_uri(
        reverse(
            'events:verify_checkin',
            kwargs={
                'slug': event.slug,
                'token': checkin_token,
            },
        )
    )

    ticket_data = get_ticket_context(
        rsvp,
        ticket=individual_ticket,
    )

    context = {
        'rsvp': rsvp,
        'event': event,
        'verify_url': verify_url,
        'ticket_data': ticket_data,
        'individual_ticket': individual_ticket,
    }

    return render(request, 'events/web_ticket.html', context)

@login_required
def upgrade_options(request, pk):
    pending = get_object_or_404(
        PendingUpgrade,
        pk=pk,
        user=request.user,
        status='pending'
    )

    return render(request, 'events/upgrade_options.html', {
        'pending': pending,
        'single_event_price': settings.SINGLE_EVENT_PRICE,
        'pro_unlimited_price': settings.PRO_UNLIMITED_PRICE,
    })

@login_required
def start_dashboard_unlimited_checkout(request):
    """
    Starts an Unlimited Pro subscription directly from the Dashboard.

    This is intentionally separate from the existing PendingUpgrade
    event-creation/edit workflow because a Dashboard subscription is
    not tied to an individual event.
    """
    profile, _ = OrganizerProfile.objects.get_or_create(
        user=request.user
    )

    # Prevent creating another subscription for an already-active subscriber.
    if profile.is_subscriber:
        messages.info(
            request,
            "You already have an active Unlimited Pro subscription."
        )
        return redirect('events:dashboard')

    stripe.api_key = settings.STRIPE_SECRET_KEY

    customer = create_or_get_stripe_customer(request.user)

    session = stripe.checkout.Session.create(
        mode='subscription',
        line_items=[
            {
                'price': settings.STRIPE_PRO_UNLIMITED_PRICE_ID,
                'quantity': 1,
            }
        ],
        success_url=(
            request.build_absolute_uri(
                reverse('events:dashboard_subscription_success')
            )
        ),
        cancel_url=request.build_absolute_uri(
            reverse('events:dashboard')
        ),
        customer=customer.id,
        metadata={
            'dashboard_unlimited_pro': 'true',
            'user_id': str(request.user.pk),
        },
    )

    return redirect(session.url)

@login_required
def start_upgrade_checkout(request, pk):
    pending = get_object_or_404(
        PendingUpgrade,
        pk=pk,
        user=request.user,
        status='pending'
    )

    plan = request.GET.get('plan')

    if plan not in ('single', 'unlimited'):
        messages.error(request, "Invalid Pro plan.")
        return redirect('events:upgrade_options', pk=pending.pk)

    pending.plan = plan
    pending.save(update_fields=['plan', 'updated_at'])

    checkout_url = create_upgrade_checkout(request, pending)

    return redirect(checkout_url)  

@login_required
def payment_success(request):
    session_id = request.GET.get('session_id')

    if not session_id:
        messages.error(request, "Invalid payment session.")
        return redirect('events:dashboard')

    pending = get_object_or_404(
        PendingUpgrade,
        stripe_session_id=session_id,
        user=request.user
    )

    if pending.status != 'completed' or not pending.event:
        return render(request, 'events/payment_success.html', {
            'event': None,
            'token': session_id,
            'pending': pending,
        })

    return render(request, 'events/payment_success.html', {
        'event': pending.event,
        'token': session_id,
        'pending': pending,
    })

@login_required
def dashboard_subscription_success(request):
    profile, _ = OrganizerProfile.objects.get_or_create(
        user=request.user
    )

    return render(
        request,
        'events/dashboard_subscription_success.html',
        {
            'organizer_profile': profile,
        }
    )

def pricing(request):
    return render(request, 'events/pricing.html')

# TICKET PENDING
def pending_rsvp_confirmed_view(request, pending_order_id):
    pending_order = get_object_or_404(
        PendingTicketOrder.objects.select_related(
            "event",
            "rsvp",
        ),
        pk=pending_order_id,
    )

    session_id = request.GET.get("session_id")

    if not session_id:
        messages.error(request, "Payment confirmation is missing.")
        return redirect(
            "events:public_rsvp",
            slug=pending_order.event.slug,
        )

    try:
        stripe.api_key = settings.STRIPE_SECRET_KEY

        checkout_session = stripe.checkout.Session.retrieve(
            session_id
        )

        # Use Stripe Session attributes, not dictionary .get() calls.
        if checkout_session.client_reference_id != str(
            pending_order.pk
        ):
            messages.error(
                request,
                "We couldn't verify this payment.",
            )
            return redirect(
                "events:public_rsvp",
                slug=pending_order.event.slug,
            )

        # Retrieve and verify the payment before creating the RSVP.
        if checkout_session.payment_status != "paid":
            messages.error(
                request,
                "Payment has not been confirmed yet. Please try again shortly.",
            )
            return redirect(
                "events:public_rsvp",
                slug=pending_order.event.slug,
            )

        rsvp = finalize_pending_ticket_order(
            checkout_session
        )

        if not rsvp:
            messages.error(
                request,
                "Your payment has not been confirmed.",
            )
            return redirect(
                "events:public_rsvp",
                slug=pending_order.event.slug,
            )

        request.session[
            f"rsvp_verified_{rsvp.event_id}"
        ] = rsvp.pk

        return render(
            request,
            "events/rsvp_confirmed.html",
            {
                "rsvp": rsvp,
                "event": rsvp.event,
            },
        )

    except Exception as exc:
        print(
            "Ticket payment confirmation error: "
            f"{type(exc).__name__}: {exc}"
        )
        messages.error(
            request,
            "We couldn't confirm your payment yet. Please try again shortly.",
        )
        return redirect(
            "events:public_rsvp",
            slug=pending_order.event.slug,
        )

#============================== 
# ORGANIZERS FOLLOW
#==============================
@require_POST
def toggle_follow_organizer(request, organizer_id):
    """
    Priority 1 & 2: Follow / unfollow an organizer.
    - If user is not authenticated: returns 401 JSON or redirects to login.
    - Toggles follow relationship.
    - Returns updated follower count and following status.
    """
    if not request.user.is_authenticated:
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
            return JsonResponse({
                'authenticated': False,
                'login_url': f"/accounts/login/?next={request.META.get('HTTP_REFERER', '/')}",
                'message': 'Please sign in to follow this organizer.'
            }, status=401)
        return redirect(f"/accounts/login/?next={request.path}")

    organizer = get_object_or_404(User, pk=organizer_id)

    # An organizer cannot follow themselves
    if organizer == request.user:
        return JsonResponse({'error': 'You cannot follow yourself.'}, status=400)
   
    follow_record = OrganizerFollow.objects.filter(
        follower=request.user,
        organizer=organizer,
    ).first()

    if follow_record:
        follow_record.delete()
        is_following = False
    else:
        _, created = OrganizerFollow.objects.get_or_create(
            follower=request.user,
            organizer=organizer,
        )
        is_following = True

        if created:
            notify_organizer_of_new_follower(
                request.user,
                organizer,
            )

    follower_count = OrganizerFollow.objects.filter(
        organizer=organizer
    ).count()


    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
        return JsonResponse({
            'following': is_following,
            'follower_count': follower_count,
            'organizer_id': organizer.id,
            'organizer_name': organizer.get_full_name() or organizer.username
        })

    messages.success(request, f"{'Following' if is_following else 'Unfollowed'} {organizer.get_full_name() or organizer.username}.")
    return redirect(request.META.get('HTTP_REFERER', 'events:home'))

@login_required
def following_feed(request):
    """
    Priority 4: Following feed.
    Displays upcoming events from all organizers the logged-in user follows.
    """
    followed_organizer_ids = OrganizerFollow.objects.filter(
        follower=request.user
    ).values_list('organizer_id', flat=True)

    from .models import Event 
    events = Event.objects.filter(
        host_id__in=followed_organizer_ids,
        event_date__gte=timezone.now()
    ).select_related('host', 'theme_settings').order_by('event_date')

    filter_option = request.GET.get('filter', 'all')
    if filter_option == 'soon':
        next_month = timezone.now() + timezone.timedelta(days=30)
        events = events.filter(event_date__lte=next_month)

    followed_organizers = User.objects.filter(id__in=followed_organizer_ids)

    return render(request, 'events/following_feed.html', {
        'events': events,
        'followed_organizers': followed_organizers,
        'filter': filter_option
    })

@login_required
@require_POST
def update_follow_notification_preferences(request):
    """
    Update email and in-app notification preferences for organizer drops.
    """
    prefs, _ = FollowNotificationPreference.objects.get_or_create(user=request.user)
    prefs.email_new_events = bool(request.POST.get('email_new_events'))
    prefs.in_app_new_events = bool(request.POST.get('in_app_new_events'))
    prefs.save()
    messages.success(request, "Your notification preferences have been saved.")
    return redirect(request.META.get('HTTP_REFERER', 'events:following_feed'))


def unsubscribe_organizer_notifications(request, token):
    """
    Zero-friction one-click unsubscribe from emails via token.
    """
    prefs = get_object_or_404(FollowNotificationPreference, unsubscribe_token=token)
    prefs.email_new_events = False
    prefs.save()
    return render(request, 'events/unsubscribed_success.html', {
        'user': prefs.user
    })


def _safe_follow_return_url(request, value=None):
    """Accept only a local URL on the current host."""
    candidate = value or request.META.get("HTTP_REFERER", "/")

    if not url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return "/"

    parts = urlsplit(candidate)

    # Avoid carrying modal-state parameters through another verification.
    query = [
        (key, val)
        for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if key not in {"follow_status", "follow_modal"}
    ]

    return urlunsplit(("", "", parts.path or "/", urlencode(query), parts.fragment))


def _follow_return_with_state(return_url, **params):
    """Add modal state to the saved event URL."""
    parts = urlsplit(return_url or "/")
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(params)

    return urlunsplit((
        "",
        "",
        parts.path or "/",
        urlencode(query),
        parts.fragment,
    ))

def _visitor_json_error(message, status=400):
    return JsonResponse({"ok": False, "message": message}, status=status)

def _create_visitor_username(email):
    base = email.split("@")[0][:120] or "visitor"
    username = base
    suffix = 1

    while User.objects.filter(username=username).exists():
        username = f"{base[:110]}_{suffix}"
        suffix += 1

    return username

@require_POST
def request_visitor_follow(request, organizer_id):
    """Send a verification link and return the visitor to the event page."""
    organizer = get_object_or_404(User, pk=organizer_id)
    return_url = _safe_follow_return_url(
        request,
        request.POST.get("next"),
    )

    if request.user.is_authenticated:
        if request.user.pk == organizer.pk:
            return _visitor_json_error("You cannot follow yourself.")

        user = request.user
        _, created = OrganizerFollow.objects.get_or_create(
            follower=user,
            organizer=organizer,
        )

        if created:
            notify_organizer_of_new_follower(user, organizer)

        return JsonResponse({
            "ok": True,
            "following": True,
            "follower_count": OrganizerFollow.objects.filter(
                organizer=organizer
            ).count(),
            "message": "You are now following this organizer.",
        })

    email = request.POST.get("email", "").strip().lower()

    try:
        validate_email(email)
    except ValidationError:
        return redirect(
            _follow_return_with_state(
                return_url,
                follow_modal="email",
                follow_error="invalid_email",
            )
        )

    VisitorFollowVerification.objects.filter(
        email__iexact=email,
        organizer=organizer,
        is_used=False,
    ).delete()

    verification = VisitorFollowVerification.objects.create(
        email=email,
        organizer=organizer,
        expires_at=timezone.now() + timedelta(hours=24),
        return_url=return_url,
    )

    try:
        send_visitor_follow_verification(verification, request)

    except Exception:
        verification.delete()

        logger.exception(
            "Failed to send visitor follow verification email "
            "for organizer_id=%s",
            organizer.pk,
        )

        return redirect(
            _follow_return_with_state(
                return_url,
                follow_modal="email",
                follow_error="email_failed",
            )
        )

    messages.success(
        request,
        "If the address can receive email, a verification link will arrive shortly.",
    )

    return redirect(return_url)

def visitor_follow(request, organizer_id):
    """
    Legacy URL fallback. The normal flow opens a modal directly on the event.
    """
    organizer = get_object_or_404(User, pk=organizer_id)
    return_url = _safe_follow_return_url(
        request,
        request.GET.get("next"),
    )

    if request.user.is_authenticated:
        return redirect(return_url)

    return redirect(_follow_return_with_state(return_url, follow_modal="email"))


def verify_visitor_follow(request, token):
    """Verify the email and either follow immediately or request registration."""
    verification = get_object_or_404(
        VisitorFollowVerification,
        token=token,
    )
    return_url = _safe_follow_return_url(
        request,
        verification.return_url,
    )

    if not verification.is_valid():
        return redirect(
            _follow_return_with_state(
                return_url,
                follow_modal="expired",
            )
        )

    email = verification.email.strip().lower()
    organizer = verification.organizer

    with transaction.atomic():
        # Lock and recheck the token so it cannot be used twice concurrently.
        verification = VisitorFollowVerification.objects.select_for_update().get(
            pk=verification.pk
        )

        if not verification.is_valid():
            return redirect(
                _follow_return_with_state(
                    return_url,
                    follow_modal="expired",
                )
            )

        verification.is_used = True
        verification.save(update_fields=["is_used"])

        user = User.objects.filter(email__iexact=email).first()
        has_rsvp = RSVP.objects.filter(email__iexact=email).exists()

        # Existing accounts or visitors with an RSVP can follow
        # after proving ownership of their email address.
        # This does NOT log the visitor into an existing account.
        if user is not None or has_rsvp:
            if user is None:
                user = User.objects.create(
                    username=_create_visitor_username(email),
                    email=email,
                )
                user.set_unusable_password()
                user.save(update_fields=["password"])

            if user.pk == organizer.pk:
                return redirect(
                    _follow_return_with_state(
                        return_url,
                        follow_modal="self",
                    )
                )

            VisitorProfile.objects.update_or_create(
                user=user,
                defaults={"email_verified": True},
            )

            # Create the follow relationship once.
            # Notify the organizer only for a genuinely new follow.
            _, created = OrganizerFollow.objects.get_or_create(
                follower=user,
                organizer=organizer,
            )

            if created:
                notify_organizer_of_new_follower(user, organizer)

            # Keep the verified follow visible in this browser session.
            followed_ids = request.session.get(
                "visitor_followed_organizer_ids", []
            )

            if organizer.pk not in followed_ids:
                followed_ids.append(organizer.pk)

            request.session["visitor_followed_organizer_ids"] = followed_ids
            request.session["visitor_follow_return_url"] = return_url

            return redirect(
                _follow_return_with_state(
                    return_url,
                    follow_status="success",
                )
            )

        # A new visitor without an RSVP/ticket must register.
        request.session["visitor_verified_email"] = email
        request.session["visitor_pending_organizer_id"] = organizer.pk
        request.session["visitor_follow_return_url"] = return_url

    return redirect(
        _follow_return_with_state(
            return_url,
            follow_modal="register",
        )
    )

@require_POST
def register_visitor(request):
    """Register a verified visitor without requiring a password."""
    email = request.session.get("visitor_verified_email")
    organizer_id = request.session.get("visitor_pending_organizer_id")

    return_url = _safe_follow_return_url(
        request,
        request.session.get("visitor_follow_return_url"),
    )

    if not email or not organizer_id:
        messages.error(
            request,
            "Your verification session has expired. Please verify your email again.",
        )
        return redirect(return_url)

    organizer = get_object_or_404(User, pk=organizer_id)

    if request.POST.get("next"):
        submitted_return_url = _safe_follow_return_url(
            request,
            request.POST.get("next"),
        )
        if submitted_return_url != "/":
            return_url = submitted_return_url

    first_name = request.POST.get("first_name", "").strip()
    last_name = request.POST.get("last_name", "").strip()

    if not first_name or not last_name:
        messages.error(request, "Enter your first and last name.")
        return redirect(
            _follow_return_with_state(
                return_url,
                follow_modal="register",
            )
        )

    with transaction.atomic():
        user = User.objects.filter(email__iexact=email).first()

        if user is None:
            user = User.objects.create(
                username=_create_visitor_username(email),
                email=email,
                first_name=first_name,
                last_name=last_name,
            )
            user.set_unusable_password()
            user.save(update_fields=["password"])
        else:
            # Do not overwrite the details of an existing account.
            update_fields = []

            if not user.first_name:
                user.first_name = first_name
                update_fields.append("first_name")

            if not user.last_name:
                user.last_name = last_name
                update_fields.append("last_name")

            if update_fields:
                user.save(update_fields=update_fields)

        if user.pk == organizer.pk:
            messages.error(request, "You cannot follow yourself.")
            return redirect(
                _follow_return_with_state(
                    return_url,
                    follow_modal="self",
                )
            )

        VisitorProfile.objects.update_or_create(
            user=user,
            defaults={"email_verified": True},
        )

        # Notify the organizer only when a new follow is created.
        _, created = OrganizerFollow.objects.get_or_create(
            follower=user,
            organizer=organizer,
        )

        if created:
            notify_organizer_of_new_follower(user, organizer)

    # Clear verification details after successful registration.
    request.session.pop("visitor_verified_email", None)
    request.session.pop("visitor_pending_organizer_id", None)

    request.session["visitor_follow_return_url"] = return_url

    followed_ids = request.session.get(
        "visitor_followed_organizer_ids",
        [],
    )

    if organizer.pk not in followed_ids:
        followed_ids.append(organizer.pk)

    request.session["visitor_followed_organizer_ids"] = followed_ids

    messages.success(
        request,
        "Registration complete. You are now following this organizer.",
    )

    return redirect(
        _follow_return_with_state(
            return_url,
            follow_status="success",
        )
    )

@login_required
def notifications_inbox(request):
    """Show notifications belonging to the logged-in user."""
    notifications = (
        InAppNotification.objects
        .filter(recipient=request.user)
        .select_related("actor", "event")
        .order_by("-created_at")
    )

    unread_count = notifications.filter(is_read=False).count()

    return render(
        request,
        "events/notifications.html",
        {
            "notifications": notifications,
            "unread_count": unread_count,
        },
    )


@login_required
def open_notification(request, notification_id):
    """Mark the user's notification as read and open its destination."""
    notification = get_object_or_404(
        InAppNotification,
        pk=notification_id,
        recipient=request.user,
    )

    if not notification.is_read:
        notification.is_read = True
        notification.save(update_fields=["is_read"])

    if notification.url:
        return redirect(notification.url)

    return redirect("events:notifications_inbox")


@login_required
def mark_all_notifications_read(request):
    """Mark all notifications belonging to the current user as read."""
    if request.method != "POST":
        return redirect("events:notifications_inbox")

    InAppNotification.objects.filter(
        recipient=request.user,
        is_read=False,
    ).update(is_read=True)

    return redirect("events:notifications_inbox")

def notify_followers_of_new_event(event):
    """
    Notify followers when an organizer publishes a new event.

    Respects each follower's in-app and email preferences.
    Uses the existing public_rsvp and unsubscribe URL routes.
    """
   
    User = get_user_model()
    organizer = event.host

    followers = (
        OrganizerFollow.objects
        .filter(organizer=organizer)
        .select_related('follower')
    )

    event_url = reverse(
        'events:public_rsvp',
        kwargs={'slug': event.slug}
    )

    site_url = getattr(settings, 'SITE_URL', '').rstrip('/')

    if not site_url:
        # Set SITE_URL in settings.py to your public website URL.
        # Do not send followers localhost links.
        site_url = ''

    absolute_event_url = f'{site_url}{event_url}' if site_url else event_url

    for follow in followers:
        follower = follow.follower

        # Do not notify the organizer about their own event.
        if follower.pk == organizer.pk:
            continue

        prefs, _ = FollowNotificationPreference.objects.get_or_create(
            user=follower
        )

        # In-app notification
        if prefs.in_app_new_events:
            InAppNotification.objects.get_or_create(
                recipient=follower,
                actor=organizer,
                event=event,
                title=f'New event from {organizer.get_full_name() or organizer.get_username()}',
                message=f'{event.title} has been published by an organizer you follow.',
                url=event_url,
                is_read=False,
            )

        # Email notification
        if prefs.email_new_events and follower.email:
            unsubscribe_url = reverse(
                'events:unsubscribe_organizer_notifications',
                kwargs={'token': prefs.unsubscribe_token}
            )

            absolute_unsubscribe_url = (
                f'{site_url}{unsubscribe_url}'
                if site_url
                else unsubscribe_url
            )

            subject = f'New event: {event.title}'
            message = (
                f'{organizer.get_full_name() or organizer.get_username()} '
                f'has published a new event: {event.title}.\n\n'
                f'View the event: {absolute_event_url}\n\n'
                f'To stop receiving new-event emails, unsubscribe here:\n'
                f'{absolute_unsubscribe_url}'
            )

            try:
                send_mail(
                    subject=subject,
                    message=message,
                    from_email=getattr(
                        settings,
                        'DEFAULT_FROM_EMAIL',
                        None
                    ),
                    recipient_list=[follower.email],
                    fail_silently=False,
                )
            except Exception:
                import logging
                logging.getLogger(__name__).exception(
                    'Failed to email follower %s about event %s',
                    follower.pk,
                    event.pk,
                )

def capture_event_campaign_attribution(request, event):
    """
    Validate campaign tracking parameters, record a deduplicated visit,
    and retain attribution in the session for this event.
    """
    campaign_id = request.GET.get("utm_campaign", "").strip()
    platform = request.GET.get("utm_content", "").strip().lower()
    source = request.GET.get("utm_source", "").strip().lower()
    medium = request.GET.get("utm_medium", "").strip().lower()

    session_attribution_key = f"campaign_attribution_{event.pk}"

    # No tracking parameters: preserve existing attribution.
    if not any((campaign_id, platform, source, medium)):
        return request.session.get(session_attribution_key, {})

    # Ignore incomplete or malformed tracking parameters.
    if not campaign_id.isdigit() or not platform:
        return request.session.get(session_attribution_key, {})

    # Import the campaign model safely.
    try:
        from marketing_studio.models import CampaignDraft
    except ImportError:
        return request.session.get(session_attribution_key, {})

    # Only accept a campaign owned by this event's host and associated
    # with this exact event.
    try:
        draft = CampaignDraft.objects.get(
            pk=int(campaign_id),
            event=event,
            host=event.host,
        )
    except (
        CampaignDraft.DoesNotExist,
        ValueError,
        TypeError,
    ):
        return request.session.get(session_attribution_key, {})

    valid_platforms = {
        choice[0] for choice in CampaignDraft.PLATFORM_CHOICES
    }

    if platform not in valid_platforms:
        return request.session.get(session_attribution_key, {})

    draft_platforms = (
        draft.platforms
        if isinstance(draft.platforms, list)
        else []
    )

    if platform not in draft_platforms:
        return request.session.get(session_attribution_key, {})

    attribution = {
        "campaign_id": draft.pk,
        "platform": platform,
        "source": source[:50],
        "medium": medium[:50],
    }

    # Ensure the visitor has a session key before recording the visit.
    if not request.session.session_key:
        request.session.create()

    session_key = request.session.session_key

    CampaignVisit.objects.get_or_create(
        event=event,
        campaign_id=draft.pk,
        platform=platform,
        session_key=session_key,
        defaults={
            "source": attribution["source"],
            "medium": attribution["medium"],
        },
    )

    # Preserve attribution for subsequent RSVP or ticket-order creation.
    request.session[session_attribution_key] = attribution
    request.session.modified = True

    return attribution

def get_event_campaign_attribution(request, event):
    """Return previously captured attribution for this event, if available."""
    attribution = request.session.get(
        f"campaign_attribution_{event.pk}",
        {},
    )

    if not isinstance(attribution, dict):
        return {}

    return attribution