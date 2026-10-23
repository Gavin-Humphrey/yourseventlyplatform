def event_requires_pro(event):
    return any([
        event.is_ticketed,
        event.show_about_section,
        event.allow_guest_messages,
        event.show_announcement,
        event.enable_qr_checkins,
        event.enable_certificates,
        bool(event.presentation_file),
        bool(event.resource_link),
    ])


def user_has_unlimited_pro(user):
    profile = getattr(user, 'organizer_profile', None)
    return bool(profile and profile.subscription_status == 'active')


def event_has_pro(event):
    return (
        event.is_pro_upgraded or
        user_has_unlimited_pro(event.host)
    )


def needs_pro_upgrade(event, user):
    if not event_requires_pro(event):
        return False

    if event.is_pro_upgraded or user_has_unlimited_pro(user):
        return False

    return True