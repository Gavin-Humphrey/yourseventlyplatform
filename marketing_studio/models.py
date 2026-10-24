
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class CampaignDraft(models.Model):
    """A host-owned marketing campaign draft for one of their events."""

    OBJECTIVE_CHOICES = [
        ("ticket_sales", "Increase ticket sales"),
        ("rsvp", "Increase event RSVPs"),
        ("awareness", "Build event awareness"),
        ("last_minute", "Promote an upcoming event"),
    ]

    PLATFORM_CHOICES = [
        ("facebook", "Facebook"),
        ("instagram", "Instagram"),
        ("linkedin", "LinkedIn"),
        ("x", "X"),
    ]

    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("reviewed", "Reviewed"),
    ]

    host = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="marketing_campaign_drafts",
    )

    event = models.ForeignKey(
        "events.Event",
        on_delete=models.CASCADE,
        related_name="marketing_campaign_drafts",
    )

    name = models.CharField(
        max_length=200,
        blank=True,
        help_text="Optional name for this marketing campaign.",
    )

    objective = models.CharField(
        max_length=30,
        choices=OBJECTIVE_CHOICES,
        default="awareness",
    )

    platforms = models.JSONField(
        default=list,
        help_text="Selected social media platforms.",
    )

    # Stores editable campaign content for later review and revision.
    # The AI engine can populate this field in a future phase.
    content = models.JSONField(
        default=dict,
        blank=True,
        help_text="Campaign copy and other draft content.",
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="draft",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Marketing campaign draft"
        verbose_name_plural = "Marketing campaign drafts"
        indexes = [
            models.Index(
                fields=["host", "-updated_at"],
                name="mktdraft_host_updated_idx",
            ),
            models.Index(
                fields=["event", "-updated_at"],
                name="mktdraft_event_updated_idx",
            ),
        ]

    def clean(self):
        super().clean()

        if self.event_id and self.host_id:
            if self.event.host_id != self.host_id:
                raise ValidationError({
                    "event": "You can only create a campaign for your own event."
                })

        if not isinstance(self.platforms, list):
            raise ValidationError({
                "platforms": "Platforms must be provided as a list."
            })

        allowed_platforms = {
            value for value, label in self.PLATFORM_CHOICES
        }
        invalid_platforms = set(self.platforms) - allowed_platforms

        if invalid_platforms:
            raise ValidationError({
                "platforms": "One or more selected platforms are invalid."
            })

    def __str__(self):
        return self.name or (
            f"Campaign for {self.event.title} ({self.get_objective_display()})"
        )