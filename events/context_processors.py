from .models import InAppNotification


def unread_notification_count(request):
    if not request.user.is_authenticated:
        return {"unread_notification_count": 0}

    count = InAppNotification.objects.filter(
        recipient=request.user,
        is_read=False,
    ).count()

    return {"unread_notification_count": count}
