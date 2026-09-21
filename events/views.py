from django.shortcuts import render, get_object_or_404, redirect
from django.conf import settings
from django.http import HttpResponse
from django.utils import timezone
from datetime import timedelta
from django.contrib import messages
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm
from django.contrib.auth.decorators import login_required
from .models import Event, OrganizerProfile, GalleryImage, RSVP, RSVPGuest, TicketTier
from .forms import EventForm, RSVPForm, TicketTierFormSet, EventTicketingForm
from django.forms import inlineformset_factory
from django.utils.text import slugify
from django.db.models import Q, Value, Sum
from django.db.models.functions import Concat
import csv
from .utils import send_rsvp_confirmation
import stripe

from .stripe_utils import (
    create_checkout_session,
    create_stripe_express_account,
    generate_account_link,
    check_stripe_onboarding_status,
    generate_stripe_login_link
)



stripe.api_key = settings.STRIPE_SECRET_KEY


# -------------------------------------------------------------------
# PUBLIC & GUEST VIEWS
# -------------------------------------------------------------------
def home(request):
    return render(request, 'events/home.html')


def event_detail(request, slug):
    event = get_object_or_404(Event, slug=slug)

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

    # Normalize gallery slides and descriptions
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
                
    guest_messages = event.rsvps.exclude(guest_message__isnull=True).exclude(guest_message__exact='')
    
    return render(request, 'events/event_detail.html', {
        'event': event, 
        'form': form, 
        'theme': theme,
        'guest_messages': guest_messages,
        'normalized_gallery': normalized_gallery,
    })

# -------------------------------------------------------------------
# HOST AUTHENTICATION VIEWS
# -------------------------------------------------------------------

def register_host(request):
    if request.user.is_authenticated:
        #return redirect('dashboard')
        return redirect('events:dashboard')
        
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(request, "Account created successfully! Welcome to your dashboard.")
            #return redirect('dashboard')
            return redirect('events:dashboard')
    else:
        form = UserCreationForm()
    return render(request, 'registration/register.html', {'form': form})

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
        formset = GalleryFormSet(request.POST, request.FILES, prefix='gallery_images')
        
        if form.is_valid() and formset.is_valid():
            # Check if this is a ticketed event
            is_ticketed = form.cleaned_data.get('is_ticketed', False)
            
            if is_ticketed:
                # Ensure an organizer profile exists
                profile, created = OrganizerProfile.objects.get_or_create(user=request.user)
                
                if not profile.stripe_onboarding_complete:
                    messages.warning(request, "You must connect your payout account before publishing a ticketed event.")
                    return redirect('events:connect_stripe_onboarding') # Adjust namespace/url name as needed

            event = form.save(commit=False)
            event.host = request.user
            event.save()
            
            formset.instance = event
            formset.save()
            
            messages.success(request, f"Event '{event.title}' created successfully!")
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
    event = get_object_or_404(Event, slug=slug)

    if event.host != request.user:
        messages.error(request, "You do not have permission to edit this event.")
        return redirect('events:dashboard')

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
        form = EventForm(request.POST, request.FILES, instance=event)
        formset = GalleryFormSet(request.POST, request.FILES, instance=event, queryset=event.gallery_images.all(), prefix='gallery_images')
        
        if form.is_valid() and formset.is_valid():
            # Check if they are switching/saving it as a ticketed event
            is_ticketed = form.cleaned_data.get('is_ticketed', False)
            
            if is_ticketed:
                profile, created = OrganizerProfile.objects.get_or_create(user=request.user)
                
                if not profile.stripe_onboarding_complete:
                    messages.warning(request, "You must connect your payout account before publishing or updating a ticketed event.")
                    return redirect('events:connect_stripe_onboarding')

            saved_event = form.save()
            formset.save()  
            
            messages.success(request, f"Event '{saved_event.title}' updated successfully!")
            return redirect('events:event_detail', slug=saved_event.slug)
    else:
        form = EventForm(instance=event)
        formset = GalleryFormSet(instance=event, queryset=event.gallery_images.all(), prefix='gallery_images')

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
    
    if request.method == 'POST':
        form = RSVPForm(request.POST, event=event)
        if form.is_valid():
            rsvp = form.save(commit=False)
            rsvp.event = event
            rsvp.status = 'ATTENDING'
            
            # Calculate total headcount request (Primary + Plus-Ones)
            requested_plus_ones = form.cleaned_data.get('plus_ones_count', 0)
            total_requested = 1 + requested_plus_ones
            
            # Enforce Event Capacity Limit
            current_headcount = sum(r.plus_ones.count() + 1 for r in event.rsvps.filter(status='ATTENDING'))
            if event.max_capacity and (current_headcount + total_requested > event.max_capacity):
                messages.error(request, "Sorry, this event does not have enough remaining capacity for your requested plus-ones.")
                # Fall through to render context with errors
            else:
                rsvp.save()
                
                # Save Individual Plus-One Names if provided
                plus_one_names = request.POST.getlist('plus_one_names[]')
                for name in plus_one_names:
                    if name.strip():
                        #PlusOne.objects.create(rsvp=rsvp, full_name=name.strip())
                        RSVPGuest.objects.create(rsvp=rsvp, full_name=name.strip())
                
                try:
                    send_rsvp_confirmation(rsvp)
                except Exception as e:
                    print(f"Email failed to send: {e}")
                    
                return redirect('events:rsvp_confirmed', pk=rsvp.pk)
    else:
        form = RSVPForm(event=event)
    
    raw_theme = event.theme_settings() if callable(getattr(event, 'theme_settings', None)) else getattr(event, 'theme_settings', None)
    
    theme = {}
    if isinstance(raw_theme, dict):
        theme = raw_theme.copy()
        theme['theme_color'] = theme.get('theme_color') or theme.get('primary_color') or '#3b82f6'
    elif raw_theme is not None:
        theme = raw_theme

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

    guest_messages = event.rsvps.exclude(guest_message__isnull=True).exclude(guest_message__exact='')

    context = {
        'event': event,
        'form': form,
        'theme': theme,
        'guest_messages': guest_messages,
        'normalized_gallery': normalized_gallery,
    }
    return render(request, 'events/public_rsvp.html', context)


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

        # Payment Status Initialization (Free tiers start paid, paid tiers start unpaid)
        is_paid = False
        if selected_tier and selected_tier.price == 0:
            is_paid = True

        rsvp = RSVP.objects.create(
            event=event,
            ticket_tier=selected_tier,
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
                guest_name = request.POST.get(f'guest_name_{i}')
                if guest_name:
                    RSVPGuest.objects.create(rsvp=rsvp, full_name=guest_name)

        if selected_tier and is_paid:
            selected_tier.sold_count += 1
            selected_tier.save()

        stripe_is_configured = bool(getattr(settings, 'STRIPE_SECRET_KEY', None))
        
        if selected_tier and selected_tier.price > 0 and stripe_is_configured:
            checkout_url = create_checkout_session(request, event, rsvp, selected_tier)
            if checkout_url:
                return redirect(checkout_url, code=303)
            else:
                messages.error(request, "Could not initiate payment session. Please check if the host has connected their Stripe payout account.")
                return redirect('events:public_rsvp', slug=event.slug)

        try:
            send_rsvp_confirmation(rsvp)
        except Exception as e:
            print(f"Email delivery error: {e}")
        
        messages.success(request, "Your RSVP and ticket selection have been recorded successfully!")
        return redirect('events:rsvp_confirmed', pk=rsvp.pk)

    return redirect('events:public_rsvp', slug=slug)


@login_required
def event_rsvps_management(request, slug):
    event = get_object_or_404(Event, slug=slug, host=request.user)
    rsvps = event.rsvps.all()
    
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
    if status_filter:
        rsvps = rsvps.filter(status=status_filter)

    total_rsvps = event.rsvps.count()
    attending_count = event.rsvps.filter(status='ATTENDING').count()
    
    # Calculate total headcount including primary RSVPs plus their plus-ones
    total_headcount = attending_count
    extra = event.rsvps.filter(status='ATTENDING').aggregate(
        total=Sum('plus_ones_count', default=0)
    )['total']
    total_headcount += (extra or 0)

    capacity_ratio = (total_headcount / event.max_capacity) if event.max_capacity and event.max_capacity > 0 else 0

    # if total_headcount >= event.max_capacity:
    if event.max_capacity is not None and total_headcount >= event.max_capacity:
        capacity_status = 'full'      # Red
    elif capacity_ratio >= 0.8:
        capacity_status = 'warning'   # Orange (80%+ full)
    else:
        capacity_status = 'safe'      # Green

    context = {
        'event': event,
        'rsvps': rsvps,
        'search_query': search_query,
        'status_filter': status_filter,
        'total_rsvps': total_rsvps,
        'attending_count': attending_count,
        'total_headcount': total_headcount,
        'capacity_status': capacity_status,
    }
    return render(request, 'events/event_rsvps_management.html', context)


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
    
    # Sync-on-Redirect fallback fallback: check Stripe session directly if not yet paid
    session_id = request.GET.get('session_id')
    if session_id and not rsvp.is_paid:
        try:
            stripe.api_key = settings.STRIPE_SECRET_KEY
            session = stripe.checkout.Session.retrieve(session_id)
            if session.payment_status == 'paid':
                rsvp.is_paid = True
                rsvp.save()
                
                if rsvp.ticket_tier:
                    rsvp.ticket_tier.sold_count += 1
                    rsvp.ticket_tier.save()
        except Exception as e:
            print(f"Stripe Session Verification Error: {e}")

    return render(request, 'events/rsvp_confirmed.html', {'rsvp': rsvp})


@login_required
def verify_checkin(request, slug, token):
    event = get_object_or_404(Event, slug=slug, host=request.user)
    
    if not event.enable_qr_checkins:
        messages.error(request, "QR check-ins are not enabled for this event.")
        return redirect('events:dashboard', slug=event.slug)
        
    rsvp = get_object_or_404(RSVP, event=event, checkin_token=token)
    
    if not rsvp.checked_in:
        rsvp.checked_in = True
        rsvp.checked_in_at = timezone.now()
        rsvp.save()
        messages.success(request, f"Checked in: {rsvp.first_name} {rsvp.last_name}!")
    else:
        messages.warning(request, f"Guest already checked in at {rsvp.checked_in_at.strftime('%H:%M')}.")
        
    return redirect('events:door_dashboard', slug=event.slug)


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


from django.db.models import Count, Sum, Q
from decimal import Decimal
from django.shortcuts import get_object_or_404, render


@login_required
def event_analytics_dashboard(request, slug):
    # Ensure this matches your model's related_name (e.g., event.rsvps or event.rsvp_set)
    event = get_object_or_404(Event, slug=slug, host=request.user)
    rsvps_query = event.rsvps  # Adjust to event.rsvp_set if you didn't set a custom related_name
    
    # Calculate key metrics
    total_rsvps = rsvps_query.count()
    confirmed_guests = rsvps_query.filter(status='ATTENDING').count()
    checked_in_guests = rsvps_query.filter(checked_in=True).count()
    
    # Calculate ticket sales and revenue using the related ticket_tier price
    ticket_sales_data = rsvps_query.filter(
        status='ATTENDING', 
        ticket_tier__isnull=False
    ).aggregate(
        total_revenue=Sum('ticket_tier__price'),
        total_tickets_sold=Count('id')
    )
    
    gross_revenue = ticket_sales_data['total_revenue'] or Decimal('0.00')
    tickets_sold = ticket_sales_data['total_tickets_sold'] or 0
    
    # Platform fee calculation (5%)
    platform_fee = gross_revenue * Decimal('0.05')
    net_organizer_payout = gross_revenue - platform_fee

    context = {
        'event': event,
        'total_rsvps': total_rsvps,
        'confirmed_guests': confirmed_guests,
        'checked_in_guests': checked_in_guests,
        'gross_revenue': gross_revenue,
        'tickets_sold': tickets_sold,
        'platform_fee': platform_fee,
        'net_organizer_payout': net_organizer_payout,
    }
    
    return render(request, 'events/analytics_dashboard.html', context)