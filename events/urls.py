from django.conf import settings
from django.conf.urls.static import static
from django.urls import path
from . import views

from .stripe_utils import stripe_webhook
from . import wallet

app_name = 'events'

urlpatterns = [
    path('', views.home, name='home'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('events/create/', views.create_event, name='create_event'),
    path('events/<int:pk>/edit/', views.event_form_view, name='edit_event'),
    path('e/<slug:slug>/', views.event_detail, name='event_detail'),
    # path('events/<slug:slug>/edit/', views.edit_event, name='edit_event'),
    path('dashboard/events/<slug:slug>/edit/', views.edit_event, name='edit_event'),
    path('e/<slug:slug>/attendee/', views.submit_event_attendance, name='submit_event_attendance'),
    path('attendee/<slug:slug>/', views.public_event_attendance, name='public_event_attendance'),
    path('dashboard/events/<slug:slug>/guests/', views.event_attendees_management, name='event_attendees_management'),
    path('dashboard/events/<slug:slug>/guests/<int:pk>/', views.view_attendee_detail, name='view_attendee_detail'),
    path('dashboard/events/<slug:slug>/guests/<int:pk>/edit/', views.edit_attendee, name='edit_attendee'),


    path('dashboard/events/<slug:slug>/clone/', views.clone_event, name='clone_event'),

    path('e/<slug:slug>/calendar/ics/', views.download_ics, name='download_ics'),

    # EventAttendee Confirmation page (where the QR code displays)
    path('attendee/<int:pk>/confirmed/', views.attendee_confirmed_view, name='attendee_confirmed'),
    
    # Host Door Check-in Verification (triggered when scanning the QR code)
    path('event/<slug:slug>/checkin/<uuid:token>/', views.verify_checkin, name='verify_checkin'),
    
    # Host Live Attendance & Door Dashboard
    path('event/<slug:slug>/door/', views.event_door_dashboard, name='door_dashboard'),

    path('features/', views.features_view, name='features_page'),


    path('cookies/', views.event_cookies, name='cookies'),
    path('privacy/', views.event_privacy, name='privacy'),
    path('terms/', views.event_terms, name='terms'),


    path('conference/', views.conference, name='conference'),
    path('gig/', views.gig, name='gig'),
    path('live_stream/', views.live_stream, name='live_stream'),
    path('private_event/', views.private_event, name='private_event'),
    path('summit/', views.summit, name='summit'),

    path('theme-engines/', views.architecture_theme_engines, name='theme_engines'),
    path('split-screen/', views.architecture_split_screen, name='split_screen'),
    path('dynamic-rsvp/', views.architecture_dynamic_rsvp, name='dynamic_rsvp'),
    path('glassmorphic-ui/', views.architecture_glassmorphic_ui, name='glassmorphic_ui'),
    path('changelog-releases/', views.architecture_changelog_releases, name='changelog_releases'),

    path('resources/architect/', views.platform_architecture, name='platform_architecture'),
    path('resources/api/', views.event_api, name='event_api'),
    path('resources/security/', views.security_compliance, name='security_compliance'),
    path('resources/community/', views.organizer_community, name='organizer_community'),
    path('resources/status/', views.system_status, name='system_status'),

    path('pricing/', views.pricing, name='pricing'),

    path('events/<slug:slug>/tiers/', views.manage_event_tiers, name='manage_event_tiers'),

    path('dashboard/events/<slug:slug>/guests/export/', views.export_attendees_csv, name='export_attendees_csv'),

    path('dashboard/upgrade/unlimited/', views.start_dashboard_unlimited_checkout, name='start_dashboard_unlimited_checkout',),

    path('webhook/', stripe_webhook, name='stripe_webhook'),
    #path('events/webhook/', stripe_webhook, name='stripe_webhook'),

    path("attendee/pending/<int:ticket_order_id>/confirmed/", views.ticket_order_confirmed_view, name="ticket_order_confirmed",),

    path('upgrade/<int:pk>/', views.upgrade_options, name='upgrade_options'),
    path('upgrade/<int:pk>/checkout/', views.start_upgrade_checkout, name='start_upgrade_checkout'),
    path('payment/success/', views.payment_success, name='payment_success'),
    ############
    path('dashboard/subscription/success/', views.dashboard_subscription_success, name='dashboard_subscription_success'),

    path('events/stripe/connect/', views.connect_stripe_onboarding, name='connect_stripe_onboarding'),

    path('events/stripe/return/', views.stripe_connect_return, name='stripe_connect_return'),
    path('events/stripe/dashboard/', views.stripe_dashboard_redirect, name='stripe_dashboard_redirect'),

    path('dashboard/events/<slug:slug>/analytics/', views.event_analytics_dashboard, name='event_analytics'),

    path('e/<slug:slug>/feedback/', views.submit_feedback, name='submit_feedback'),


    path('attendee/<slug:slug>/verify-email/', views.verify_attendee_email, name='verify_attendee_email'),

    path('event/<slug:event_slug>/certificate/download/', views.download_certificate, name='download_certificate'),

    # path('pass/<uuid:token>/apple/', wallet.download_apple_wallet_pass, name='apple_pass'),
    
    path('wallet/pass/<uuid:token>/', views.google_pass_view, name='google_pass_view'),
    path('ticket/<token>/', views.web_ticket_view, name='web_ticket'),

    # FOLLOW
    path('organizer/<int:organizer_id>/toggle-follow/', views.toggle_follow_organizer, name='toggle_follow_organizer'),
    path('feed/following/', views.following_feed, name='following_feed'),
    path('notifications/preferences/', views.update_follow_notification_preferences, name='update_follow_notification_preferences'),
    path('notifications/unsubscribe/<uuid:token>/', views.unsubscribe_organizer_notifications, name='unsubscribe_organizer_notifications'),

    path( "visitor/follow/<int:organizer_id>/", views.visitor_follow, name="visitor_follow",),
    path("visitor/follow/<int:organizer_id>/send/", views.request_visitor_follow, name="request_visitor_follow",),
    path("visitor/verify/<uuid:token>/", views.verify_visitor_follow, name="verify_visitor_follow",),
    path("visitor/register/", views.register_visitor, name="visitor_register",),

    path("notifications/", views.notifications_inbox, name="notifications_inbox",),
    path("notifications/<int:notification_id>/open/", views.open_notification, name="open_notification",),

    path("notifications/mark-all-read/", views.mark_all_notifications_read, name="mark_all_notifications_read",),

    #==============================

    # path('view/bento/', views.bento_view, name='bento_view'),
    # path('view/editorial_minimalist_view/', views.editorial_minimalist_view, name='editorial_minimalist_view'),
    # path('view/neon_and_dark_view/', views.neon_and_dark_view, name='neon_and_dark_view'),
    # path('view/split_screen_conference_view/', views.split_screen_conference_view, name='split_screen_conference_view'),
    # path('view/story_teller_view/', views.story_teller_view, name='story_teller_view'),
]
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)