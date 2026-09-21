from django.shortcuts import render, get_object_or_404, redirect
from django.conf import settings
from django.urls import reverse
import stripe
import json
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from .models import OrganizerProfile, RSVP

stripe.api_key = settings.STRIPE_SECRET_KEY

@csrf_exempt
def stripe_webhook(request):
    payload = request.body
    sig_header = request.META.get('HTTP_STRIPE_SIGNATURE')
    event = None

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        return HttpResponse(status=400)
    except stripe.error.SignatureVerificationError:
        return HttpResponse(status=400)

    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        rsvp_id = session.get('metadata', {}).get('rsvp_id')
        
        if rsvp_id:
            try:
                rsvp = RSVP.objects.get(id=rsvp_id)
                if not rsvp.is_paid:
                    rsvp.is_paid = True
                    rsvp.save()
                    
                    if rsvp.ticket_tier:
                        rsvp.ticket_tier.sold_count += 1
                        rsvp.ticket_tier.save()
            except RSVP.DoesNotExist:
                pass

    return HttpResponse(status=200)





# Connect

def create_checkout_session(request, event, rsvp, selected_tier):
    """
    Creates a Stripe Checkout Session using Stripe Connect Destination Charges
    to route ticket revenue directly to the event organizer's connected account.
    """
    try:
        # Retrieve the organizer's profile using event.host
        try:
            organizer_profile = OrganizerProfile.objects.get(user=event.host)
        except OrganizerProfile.DoesNotExist:
            organizer_profile = None
        
        stripe_account_id = organizer_profile.stripe_account_id if organizer_profile else None
        
        if not stripe_account_id:
            print(f"ERROR: Organizer '{event.host.username}' for event '{event.title}' has no connected Stripe Account ID.")
            return None

        # Build line items for Stripe Checkout
        unit_amount = int(selected_tier.price * 100) # Convert price to cents
        
        session_params = {
            'payment_method_types': ['card'],
            'line_items': [{
                'price_data': {
                    'currency': 'usd', # Update currency if dynamic
                    'product_data': {
                        'name': f"{event.title} - {selected_tier.name}",
                    },
                    'unit_amount': unit_amount,
                },
                'quantity': 1,
            }],
            'mode': 'payment',
            'success_url': request.build_absolute_uri(reverse('events:rsvp_confirmed', args=[rsvp.id])) + '?session_id={CHECKOUT_SESSION_ID}',
            'cancel_url': request.build_absolute_uri(reverse('events:public_rsvp', args=[event.slug])),
            'client_reference_id': str(rsvp.pk),
            'metadata': {
                'rsvp_id': rsvp.id,
                'event_id': event.id,
                'tier_id': selected_tier.id, # Kept this as a helpful addition
            },
            'payment_intent_data': {
                'transfer_data': {
                    'destination': stripe_account_id,
                },
            },
        }
        checkout_session = stripe.checkout.Session.create(**session_params)
        return checkout_session.url

    except stripe.error.StripeError as e:
        print(f"Stripe Checkout Creation Error: {e}")
        return None

# Set your Stripe secret key globally for this module
stripe.api_key = settings.STRIPE_SECRET_KEY

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