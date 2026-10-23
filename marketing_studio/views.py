"""
YoursEvently Marketing Studio.

Campaign plans are stored in CampaignDraft.content["draft_text"].
Existing drafts remain compatible; no database migration is required.
"""

import json
import logging
import re
from datetime import date, datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from decouple import config
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.html import strip_tags

from events.models import Event
from .models import CampaignDraft


logger = logging.getLogger(__name__)

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = config("GROQ_MODEL", default="openai/gpt-oss-20b").strip()

MAX_CAMPAIGN_NAME_LENGTH = 200
MAX_CAMPAIGN_TEXT_LENGTH = 50000

VALID_OBJECTIVES = {
    key for key, _label in CampaignDraft.OBJECTIVE_CHOICES
}

VALID_PLATFORMS = {
    key for key, _label in CampaignDraft.PLATFORM_CHOICES
}


def _clean_campaign_text(value):
    """Normalize model output without destroying intentional line breaks."""
    if value is None:
        return ""

    if isinstance(value, (list, tuple)):
        value = "\n".join(str(item) for item in value)

    return str(value).strip()


def _format_hashtags(hashtags):
    """Normalize and de-duplicate hashtags."""
    if not isinstance(hashtags, list):
        return ""

    result = []
    seen = set()

    for tag in hashtags:
        tag = re.sub(r"\s+", "", str(tag).strip().lstrip("#"))

        if not tag:
            continue

        normalized = f"#{tag}"

        if normalized.casefold() not in seen:
            result.append(normalized)
            seen.add(normalized.casefold())

    return " ".join(result)


def _format_platform_post(value):
    """Normalize whitespace in platform-specific copy."""
    value = _clean_campaign_text(value)
    value = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", value)

    return value.strip()


def _public_base_url(request):
    """Use PUBLIC_SITE_URL in production, or the current request host locally."""
    configured = (
        getattr(settings, "PUBLIC_SITE_URL", "")
        or config("PUBLIC_SITE_URL", default="")
    )

    configured = str(configured or "").strip().rstrip("/")

    if configured:
        if not configured.startswith(("https://", "http://")):
            configured = f"https://{configured}"

        return configured

    return request.build_absolute_uri("/").rstrip("/")


def _event_public_url(event, request):
    """Build the public RSVP URL for an event."""
    try:
        path = reverse(
            "events:public_rsvp",
            kwargs={"slug": event.slug},
        )
    except NoReverseMatch:
        logger.exception(
            "Could not reverse events:public_rsvp for event %s.",
            event.pk,
        )
        return ""

    return f"{_public_base_url(request)}{path}"


def _event_location(event):
    """Build a location string using available event fields."""
    parts = [
        getattr(event, "location_name", ""),
        getattr(event, "address", ""),
        getattr(event, "city", ""),
        str(getattr(event, "country", "") or ""),
    ]

    return ", ".join(
        str(part).strip()
        for part in parts
        if str(part or "").strip()
    )


def _event_date_text(event):
    """Format the event date without inventing missing date information."""
    value = getattr(event, "event_date", None)

    if not value:
        return ""

    try:
        if isinstance(value, datetime) and timezone.is_aware(value):
            value = timezone.localtime(value)

        if isinstance(value, (datetime, date)):
            return value.strftime(
                "%A, %B %d, %Y at %I:%M %p"
            ).replace(" 0", " ")

    except (TypeError, ValueError):
        logger.warning(
            "Could not format event date for event %s.",
            event.pk,
        )

    return str(value)


def _event_facts(event, event_url=""):
    """Return only event facts that are actually available."""
    facts = {
        "event_title": getattr(event, "title", ""),
        "event_type": "",
        "event_date_and_time": _event_date_text(event),
        "location": _event_location(event),
        "description": strip_tags(
            str(getattr(event, "description", "") or "")
        )[:5000],
        "event_url": event_url,
        "is_ticketed": bool(
            getattr(event, "is_ticketed", False)
        ),
        "ticketing_note": (
            "Ticketed event; invite the audience to view ticket options."
            if getattr(event, "is_ticketed", False)
            else (
                "Do not mention ticket purchases; use an RSVP/attendance "
                "CTA where suitable."
            )
        ),
    }

    try:
        facts["event_type"] = event.get_event_type_display()
    except (AttributeError, TypeError):
        facts["event_type"] = str(
            getattr(event, "event_type", "") or ""
        )

    for optional in ("food_served", "max_capacity"):
        value = getattr(event, optional, None)

        if value not in (None, ""):
            facts[optional] = value

    return {
        key: value
        for key, value in facts.items()
        if value not in ("", None)
    }


def _extract_json_object(content):
    """Parse a JSON object, tolerating accidental code fences."""
    content = _clean_campaign_text(content)

    content = re.sub(
        r"^\s*```(?:json)?\s*",
        "",
        content,
        flags=re.IGNORECASE,
    )

    content = re.sub(r"\s*```\s*$", "", content)

    try:
        parsed = json.loads(content)

    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")

        if start < 0 or end <= start:
            raise

        parsed = json.loads(content[start:end + 1])

    if not isinstance(parsed, dict):
        raise ValueError(
            "The AI response was not a campaign object."
        )

    return parsed


def _build_campaign_prompt(
    event_facts,
    objective_label,
    selected_platforms,
):
    """Build the strategic campaign-generation prompt."""
    platform_guidance = {
        "facebook": (
            "Community-led, conversational, useful event details, "
            "shareable invitation."
        ),
        "instagram": (
            "Visual-first, vivid first line, concise caption, useful "
            "line breaks and relevant hashtags."
        ),
        "linkedin": (
            "Professional and audience-relevant, explain the event's "
            "practical or community value."
        ),
        "x": (
            "Concise and direct; prioritize hook, key fact, CTA, and URL; "
            "avoid padding."
        ),
    }

    selected_guidance = {
        platform: platform_guidance.get(
            platform,
            "Adapt naturally to this platform.",
        )
        for platform in selected_platforms
    }

    system = """
You are YoursEvently's senior event-marketing strategist and creative agency.
Create a useful, accurate campaign plan plus ready-to-edit promotional copy.
Return valid JSON only. Do not wrap it in Markdown fences.

TRUTH AND SAFETY:
- The EVENT FACTS are the sole source of truth. Never invent speakers,
  performers, activities, ticket prices, discounts, scarcity, endorsements,
  attendance, facilities, accessibility, sponsor names, or claims that an
  event is nearly sold out.
- A suggested audience must be labelled as a marketing hypothesis, not a
  verified attendee demographic.
- If a fact is missing, omit it. Do not make up dates, venue, city, ticket
  details, or URL.
- Use the exact event_url provided. Do not rewrite, shorten, or fabricate links.
- For Instagram, do not assert a link is in the bio. Invite people to visit
  the event page instead.
- Do not invent campaign discounts or early-bird offers. If no verified offer
  is provided, make any offer-related step conditional on the host confirming
  an offer exists.
- Do not claim content was published, scheduled, delivered, or measured.
  This tool creates a plan and draft copy only.
- Avoid generic filler and repetitive copy. Each platform post must be
  meaningfully adapted.
- Campaign timing must be expressed relative to the event date, for example
  "3–4 weeks before", "7 days before", "24 hours before", or "event day".
  If no event date exists, provide sequence order and state that the host must
  set dates manually.
- Only create platform_posts for selected platforms. Use empty strings for
  unselected platforms.
- Keep content human, persuasive, scannable, accessible, and factually grounded.
"""

    user = f"""
Build a complete event-marketing campaign for YoursEvently.

OBJECTIVE: {objective_label}
SELECTED PLATFORMS: {json.dumps(selected_platforms)}
PLATFORM STYLE GUIDANCE: {json.dumps(selected_guidance, ensure_ascii=False)}
EVENT FACTS (authoritative): {
    json.dumps(event_facts, ensure_ascii=False, default=str, indent=2)
}

Create a strategic plan with:
1. campaign_strategy: target_audience, core_message, value_proposition, tone,
   objective, primary_kpi, measurement_method, assumptions_to_confirm.
2. campaign_timeline: 5–7 practical stages spanning awareness through event-day
   communication as appropriate. Each item has stage, timing, purpose,
   recommended_action, success_signal, and copy_angle. Do not make every stage
   a sales push.
3. primary_post: hook, body, call_to_action, hashtags (array).
4. alternative_hook: a genuinely different creative angle.
5. platform_posts: selected-platform copy strings; empty for unselected platforms.
6. audience_variants: 3 distinct message angles for different plausible
   audiences, each with audience_hypothesis, angle, sample_hook.
   Label audiences as hypotheses.
7. creative_direction: visual_concept, image_guidance, overlay_text.
8. optimization_notes: 3 concrete A/B tests or improvement ideas; do not claim
   results.

For each platform, write independently using its style guidance; do not just
shorten the primary post. Include event facts and exact event URL only where
appropriate. Do not fabricate a URL. Keep the strategy concise but make the
timeline actionable.

Return exactly this JSON shape:
{{
  "campaign_strategy": {{
    "target_audience": "",
    "core_message": "",
    "value_proposition": "",
    "tone": "",
    "objective": "",
    "primary_kpi": "",
    "measurement_method": "",
    "assumptions_to_confirm": []
  }},
  "campaign_timeline": [
    {{
      "stage": "",
      "timing": "",
      "purpose": "",
      "recommended_action": "",
      "success_signal": "",
      "copy_angle": ""
    }}
  ],
  "primary_post": {{
    "hook": "",
    "body": "",
    "call_to_action": "",
    "hashtags": []
  }},
  "alternative_hook": "",
  "platform_posts": {{
    "facebook": "",
    "instagram": "",
    "linkedin": "",
    "x": ""
  }},
  "audience_variants": [
    {{
      "audience_hypothesis": "",
      "angle": "",
      "sample_hook": ""
    }}
  ],
  "creative_direction": {{
    "visual_concept": "",
    "image_guidance": "",
    "overlay_text": ""
  }},
  "optimization_notes": ["", "", ""]
}}
"""

    return system.strip(), user.strip()


def generate_groq_campaign(
    event,
    objective,
    platforms,
    event_url="",
):
    """Generate a strategic campaign and format it as an editable document."""
    api_key = config("GROQ_API_KEY", default="").strip()

    if not api_key:
        raise ValueError(
            "GROQ_API_KEY is missing. Add it to your environment or .env file."
        )

    if objective not in VALID_OBJECTIVES:
        raise ValueError("Select a valid marketing objective.")

    selected_platforms = list(
        dict.fromkeys(
            platform
            for platform in (platforms or [])
            if platform in VALID_PLATFORMS
        )
    )

    if not selected_platforms:
        raise ValueError(
            "Select at least one valid marketing platform."
        )

    objective_label = dict(
        CampaignDraft.OBJECTIVE_CHOICES
    )[objective]

    event_facts = _event_facts(event, event_url)

    system_prompt, user_prompt = _build_campaign_prompt(
        event_facts,
        objective_label,
        selected_platforms,
    )

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": user_prompt,
            },
        ],
        "temperature": 0.75,
        "max_completion_tokens": 6000,
        "response_format": {
            "type": "json_object",
        },
    }

    api_request = Request(
        GROQ_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "YoursEvently-MarketingStudio/2.0",
        },
        method="POST",
    )

    try:
        with urlopen(api_request, timeout=75) as response:
            response_data = json.loads(
                response.read().decode("utf-8")
            )

    except HTTPError as exc:
        body = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        logger.error(
            "Groq API HTTP %s: %s",
            exc.code,
            body[:1200],
        )

        if exc.code == 403 and any(
            term in body.lower()
            for term in (
                "cloudflare",
                "access denied",
                "1010",
            )
        ):
            raise ValueError(
                "The AI provider denied the request (HTTP 403). "
                "Check provider access and server logs."
            ) from exc

        if exc.code == 429:
            raise ValueError(
                "The AI service is busy or rate-limited. "
                "Wait briefly and try again."
            ) from exc

        raise ValueError(
            f"The AI service returned HTTP {exc.code}. "
            "Check API configuration and try again."
        ) from exc

    except URLError as exc:
        logger.warning(
            "Could not connect to Groq: %s",
            exc,
        )

        raise ValueError(
            "Unable to connect to the AI service. "
            "Check the connection and try again."
        ) from exc

    except (TimeoutError, OSError) as exc:
        logger.warning(
            "Groq request failed: %s",
            exc,
        )

        raise ValueError(
            "The AI request timed out or failed. Please try again."
        ) from exc

    except json.JSONDecodeError as exc:
        logger.exception("Groq returned invalid response JSON.")

        raise ValueError(
            "The AI service returned an invalid response. Please try again."
        ) from exc

    try:
        choices = response_data.get("choices") or []

        content = choices[0].get(
            "message",
            {},
        ).get("content", "")

        if not content:
            raise ValueError(
                "The AI service returned an empty response."
            )

        campaign = _extract_json_object(content)

    except (
        AttributeError,
        IndexError,
        TypeError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        logger.exception("Could not parse Groq campaign response.")

        raise ValueError(
            "The AI returned an incomplete campaign. "
            "Please generate it again."
        ) from exc

    return format_campaign_document(
        campaign,
        selected_platforms,
    )





def format_campaign_document(campaign, selected_platforms):
    """Format campaign data as a clean, modern, social-first campaign brief."""
    import re
    import textwrap

    strategy = campaign.get("campaign_strategy")
    strategy = strategy if isinstance(strategy, dict) else {}

    primary = campaign.get("primary_post")
    primary = primary if isinstance(primary, dict) else {}

    creative = campaign.get("creative_direction")
    creative = creative if isinstance(creative, dict) else {}

    platform_posts = campaign.get("platform_posts")
    platform_posts = platform_posts if isinstance(platform_posts, dict) else {}

    timeline = campaign.get("campaign_timeline")
    timeline = timeline if isinstance(timeline, list) else []

    audience_variants = campaign.get("audience_variants")
    audience_variants = (
        audience_variants if isinstance(audience_variants, list) else []
    )

    optimization_notes = campaign.get("optimization_notes")
    optimization_notes = (
        optimization_notes if isinstance(optimization_notes, list) else []
    )

    def clean(value):
        """Normalize AI-generated text without destroying paragraph structure."""
        if value is None:
            return ""

        if isinstance(value, (dict, list)):
            return _clean_campaign_text(value)

        value = str(value)

        # Convert literal backslash-n sequences returned by the AI
        # into actual newlines before normalizing whitespace.
        value = value.replace("\\r\\n", "\n")
        value = value.replace("\\n", "\n")
        value = value.replace("\\r", "\n")

        value = value.replace("\r\n", "\n").replace("\r", "\n")
        value = re.sub(r"[ \t]+\n", "\n", value)
        value = re.sub(r"\n[ \t]+", "\n", value)
        value = re.sub(r"\n{3,}", "\n\n", value)

        return value.strip()

    def paragraph(value):
        """Format prose with readable paragraph spacing."""
        value = clean(value)
        if not value:
            return []

        paragraphs = re.split(r"\n\s*\n", value)
        result = []

        for item in paragraphs:
            item = item.strip()
            if item:
                result.extend([item, ""])

        return result[:-1] if result and result[-1] == "" else result

    def field(label, value, icon="•"):
        """Create a readable label/value pair."""
        value = clean(value)
        if not value:
            return []

        return [f"{icon} {label}: {value}"]

    def bullet_list(items, icon="•"):
        if not isinstance(items, list):
            return []

        result = []
        for item in items:
            value = clean(item)
            if value:
                result.append(f"{icon} {value}")

        return result

    def heading(icon, title, subtitle=None):
        """Use simple headings rather than oversized text dividers."""
        result = [
            "",
            f"{icon}  {title}",
        ]

        if subtitle:
            result.append(subtitle)

        result.append("")
        return result

    def add_fields(fields):
        result = []

        for label, value, icon in fields:
            result.extend(field(label, value, icon))

        return result

    def format_caption(value):
        """Normalize social copy while retaining intentional line breaks."""
        value = clean(value)

        if not value:
            return ""

        # Remove accidental spaces before punctuation.
        value = re.sub(r"[ \t]+([,.;!?])", r"\1", value)

        # Keep hashtags readable and separate from the main caption.
        value = re.sub(
            r"[ \t]+(?=#\w)",
            "\n",
            value,
        )

        # Separate sentences accidentally joined by literal newlines
        # only when they are already distinct lines; don't rewrite wording.
        value = re.sub(r"\n{3,}", "\n\n", value)

        return value.strip()

    def format_hashtags(value):
        formatted = _format_hashtags(value or [])
        return clean(formatted)

    # Campaign cover
    lines = [
        "YOURSEVENTLY  ✦  MARKETING STUDIO",
        "",
        "EVENT CAMPAIGN",
        "Your idea. Your people. Your moment.",
        "",
        "A campaign workspace for planning, creating, and promoting your event.",
        "",
        "✦ CAMPAIGN AT A GLANCE",
    ]

    lines.extend(
        add_fields([
            ("Objective", strategy.get("objective"), "🎯"),
            ("Audience", strategy.get("target_audience"), "👥"),
            ("Core message", strategy.get("core_message"), "💬"),
            ("Campaign tone", strategy.get("tone"), "✨"),
            ("Success metric", strategy.get("primary_kpi"), "📈"),
        ])
    )

    # Strategy
    lines.extend(
        heading(
            "🧭",
            "THE GAME PLAN",
            "The strategy behind the campaign",
        )
    )

    lines.extend(
        add_fields([
            ("Value proposition", strategy.get("value_proposition"), "💎"),
            ("Measurement approach", strategy.get("measurement_method"), "📊"),
        ])
    )

    assumptions = bullet_list(
        strategy.get("assumptions_to_confirm") or [],
        "☑",
    )

    if assumptions:
        lines.extend(["", "BEFORE YOU LAUNCH", *assumptions])

    # Timeline
    lines.extend(
        heading(
            "🗓️",
            "THE CAMPAIGN COUNTDOWN",
            "A suggested rollout from first teaser to event day",
        )
    )

    valid_timeline = [
        item for item in timeline
        if isinstance(item, dict)
    ]

    if valid_timeline:
        for index, item in enumerate(valid_timeline, 1):
            stage = clean(item.get("stage")) or f"Campaign stage {index}"

            lines.extend([
                f"{index:02d}  ✦  {stage}",
                "",
            ])

            lines.extend(
                add_fields([
                    ("When", item.get("timing"), "⏰"),
                    ("Goal", item.get("purpose"), "🎯"),
                    ("Action", item.get("recommended_action"), "🚀"),
                    ("Creative angle", item.get("copy_angle"), "🎨"),
                    ("Success signal", item.get("success_signal"), "📈"),
                ])
            )

            lines.append("")
    else:
        lines.append("Add your campaign dates and milestones before launch.")

    # Main campaign copy
    hook = clean(primary.get("hook"))
    body = format_caption(primary.get("body"))
    cta = clean(primary.get("call_to_action"))
    hashtags = format_hashtags(primary.get("hashtags"))

    lines.extend(
        heading(
            "🔥",
            "THE MAIN EVENT POST",
            "Your core message, ready to review and personalize",
        )
    )

    if hook:
        lines.extend(["✨ " + hook, ""])

    if body:
        lines.extend(paragraph(body))
        lines.append("")

    if cta:
        lines.extend([
            "👇 YOUR NEXT MOVE",
            cta,
            "",
        ])

    if hashtags:
        lines.extend([
            "🏷️ " + hashtags,
            "",
        ])

    # Alternative hook
    alternative = clean(campaign.get("alternative_hook"))

    lines.extend(
        heading(
            "💡",
            "ANOTHER ANGLE",
            "A second hook to try",
        )
    )

    lines.extend(paragraph(alternative or "No alternative hook was provided."))
    lines.append("")

    # Platform copy
    lines.extend(
        heading(
            "📲",
            "SOCIAL, YOUR WAY",
            "Each channel gets its own voice. Review every caption before posting.",
        )
    )

    platform_details = {
        "facebook": ("FACEBOOK", "👥", "Community, conversation, and shares"),
        "instagram": ("INSTAGRAM", "📸", "Visual storytelling and discovery"),
        "linkedin": ("LINKEDIN", "💼", "Professional and industry audiences"),
        "x": ("X", "𝕏", "Short, timely updates and conversation"),
        "tiktok": ("TIKTOK", "🎬", "Fast-paced video and creator-style storytelling"),
        "youtube": ("YOUTUBE", "▶️", "Video, trailers, and event highlights"),
        "threads": ("THREADS", "🧵", "Casual conversation and community"),
        "pinterest": ("PINTEREST", "📌", "Visual inspiration and planning"),
    }

    # Support both normalized platform names and case differences.
    for platform in selected_platforms:
        platform_key = str(platform).strip().lower()
        platform_copy = format_caption(
            platform_posts.get(platform)
            or platform_posts.get(platform_key)
            or ""
        )

        label, icon, description = platform_details.get(
            platform_key,
            (str(platform).upper(), "📣", "Platform-specific campaign copy"),
        )

        lines.extend([
            f"{icon}  {label}",
            description,
            "",
        ])

        if platform_copy:
            lines.extend(paragraph(platform_copy))
        else:
            lines.append(
                "Caption not returned. Add and review the copy for this channel."
            )

        lines.extend([
            "",
            "✂️ Caption ends here",
            "",
        ])

    # Audience variations
    lines.extend(
        heading(
            "🎯",
            "SPEAK TO DIFFERENT CROWDS",
            "Test these audience angles to learn which message connects",
        )
    )

    valid_variants = [
        item for item in audience_variants
        if isinstance(item, dict)
    ]

    if valid_variants:
        for index, item in enumerate(valid_variants, 1):
            lines.extend([
                f"💬 AUDIENCE IDEA {index:02d}",
                "",
            ])

            lines.extend(
                add_fields([
                    ("Who", item.get("audience_hypothesis"), "👥"),
                    ("Angle", item.get("angle"), "✨"),
                    ("Hook to test", item.get("sample_hook"), "🪝"),
                ])
            )

            lines.append("")
    else:
        lines.append("No audience variants were provided.")

    # Creative direction
    lines.extend(
        heading(
            "🎨",
            "MAKE IT SCROLL-STOPPING",
            "Visual ideas for your graphics, photos, and video",
        )
    )

    lines.extend(
        add_fields([
            ("Visual concept", creative.get("visual_concept"), "🖼️"),
            ("Photo / video direction", creative.get("image_guidance"), "🎥"),
            ("Graphic headline", creative.get("overlay_text"), "✍️"),
        ])
    )

    # Optimization
    lines.extend(
        heading(
            "📊",
            "TEST. LEARN. LEVEL UP.",
            "Small experiments can reveal what moves your audience",
        )
    )

    experiments = [
        clean(item)
        for item in optimization_notes
        if clean(item)
    ]

    if experiments:
        for index, experiment in enumerate(experiments, 1):
            lines.append(f"{index}. {experiment}")
    else:
        lines.append("No optimization experiments were provided.")

    # Launch checklist
    lines.extend(
        heading(
            "✅",
            "BEFORE YOU HIT PUBLISH",
            "A quick final check",
        )
    )

    lines.extend([
        "☐ Confirm the event name, date, time, venue, and timezone.",
        "☐ Verify ticket prices, availability, and promotional claims.",
        "☐ Check every link using a public event URL.",
        "☐ Preview captions on mobile and check hashtags and line breaks.",
        "☐ Add tracking parameters if you measure campaign traffic.",
        "☐ Confirm the final creative and copy with the event host.",
        "",
        "────────────────────────",
        "Created with YoursEvently Marketing Studio ✦",
        "Plan the vibe. Build the buzz. Bring people together.",
    ])

    # Prevent accidental excessive blank lines in the final text.
    output = "\n".join(lines)
    output = re.sub(r"\n[ \t]+\n", "\n\n", output)
    output = re.sub(r"\n{3,}", "\n\n", output)

    return output.strip()




def _owned_draft(user, draft_id):
    """Retrieve a campaign draft only if it belongs to the current host."""
    if not draft_id:
        return None

    try:
        return (
            CampaignDraft.objects
            .filter(
                pk=int(draft_id),
                host=user,
            )
            .select_related("event")
            .first()
        )

    except (TypeError, ValueError):
        return None


def _draft_text(content):
    """Read current and legacy draft formats."""
    if isinstance(content, dict):
        return str(
            content.get("draft_text")
            or content.get("text")
            or ""
        )

    if isinstance(content, str):
        return content

    return ""


def _drafts_for_host(user):
    """Return only campaigns owned by the signed-in host."""
    return (
        CampaignDraft.objects
        .filter(host=user)
        .select_related("event")
        .order_by("-updated_at")
    )


@login_required
def ai_marketing_studio(request):
    """Host-only Marketing Studio with ownership checks on mutations."""
    events = list(
        Event.objects
        .filter(host=request.user)
        .order_by("-created_at")
    )

    drafts = _drafts_for_host(request.user)
    objectives = CampaignDraft.OBJECTIVE_CHOICES
    available_platforms = CampaignDraft.PLATFORM_CHOICES

    selected_objective = "awareness"
    selected_platforms = ["facebook", "instagram"]

    selected_event = None
    selected_draft = None

    campaign_name = ""
    campaign_content = ""
    draft_id = ""

    if request.method == "POST":
        submitted_actions = [
            value.strip()
            for value in request.POST.getlist("action")
            if value and value.strip()
        ]

        allowed_actions = {"generate", "save", "delete"}

        action = next(
            (
                value
                for value in reversed(submitted_actions)
                if value in allowed_actions
            ),
            "",
        )

        draft_id = request.POST.get("draft_id", "").strip()

        # FIX: if a stale page or Enter-key submission omits the button value,
        # recognize the main campaign form and safely default to Save.
        if (
            not action
            and not draft_id
            and all(
                key in request.POST
                for key in (
                    "name",
                    "event_id",
                    "objective",
                    "campaign_content",
                )
            )
        ):
            action = "save"
            logger.warning(
                "Marketing Studio action missing; defaulting to Save. "
                "User ID: %s; POST keys: %r",
                request.user.pk,
                list(request.POST.keys()),
            )

        if action == "delete":
            draft = _owned_draft(request.user, draft_id)

            if draft is None:
                messages.error(
                    request,
                    "The campaign draft could not be found in your account.",
                )
            else:
                draft.delete()
                messages.success(
                    request,
                    "Campaign draft deleted.",
                )

            return redirect("marketing_studio:studio")

        if action not in {"generate", "save"}:
            logger.warning(
                "Unrecognized Marketing Studio action for user %s. "
                "POST action values: %r; POST keys: %r",
                request.user.pk,
                submitted_actions,
                list(request.POST.keys()),
            )
            messages.error(
                request,
                "The requested action was not recognized. "
                "Reload Marketing Studio and try again.",
            )
            return redirect("marketing_studio:studio")

        # Keep the selected draft when editing. Check ownership before reuse.
        if draft_id:
            selected_draft = _owned_draft(
                request.user,
                draft_id,
            )

            if selected_draft is None:
                messages.error(
                    request,
                    "That campaign draft was not found in your account.",
                )
                return redirect("marketing_studio:studio")

        event_id = request.POST.get("event_id", "").strip()

        selected_objective = request.POST.get(
            "objective",
            "awareness",
        ).strip()

        selected_platforms = list(
            dict.fromkeys(request.POST.getlist("platforms"))
        )

        campaign_name = request.POST.get(
            "name",
            "",
        ).strip()[:MAX_CAMPAIGN_NAME_LENGTH]

        campaign_content = request.POST.get(
            "campaign_content",
            "",
        ).strip()

        if len(campaign_content) > MAX_CAMPAIGN_TEXT_LENGTH:
            messages.error(
                request,
                "Campaign content is too long. Please shorten it before saving.",
            )
            campaign_content = campaign_content[:MAX_CAMPAIGN_TEXT_LENGTH]

        # Resolve only an event belonging to the signed-in host.
        if event_id:
            try:
                selected_event = Event.objects.get(
                    pk=event_id,
                    host=request.user,
                )
            except (
                Event.DoesNotExist,
                ValueError,
                TypeError,
            ):
                messages.error(
                    request,
                    "That event is not available in your account.",
                )
                selected_event = None

        elif selected_draft is not None:
            selected_event = selected_draft.event

        if not event_id:
            messages.error(
                request,
                "Please select one of your events.",
            )

        if selected_objective not in VALID_OBJECTIVES:
            messages.error(
                request,
                "Please select a valid campaign objective.",
            )

        if (
            not selected_platforms
            or any(
                platform not in VALID_PLATFORMS
                for platform in selected_platforms
            )
        ):
            messages.error(
                request,
                "Select at least one valid marketing platform.",
            )

        form_fields_valid = (
            bool(event_id)
            and selected_event is not None
            and selected_objective in VALID_OBJECTIVES
            and bool(selected_platforms)
            and all(
                platform in VALID_PLATFORMS
                for platform in selected_platforms
            )
        )

        if (
            form_fields_valid
            and selected_event
            and action == "generate"
        ):
            event_url = _event_public_url(
                selected_event,
                request,
            )

            if not event_url:
                messages.warning(
                    request,
                    "A public event URL could not be resolved. "
                    "The AI will not invent one; verify the link before publishing.",
                )

            try:
                campaign_content = generate_groq_campaign(
                    event=selected_event,
                    objective=selected_objective,
                    platforms=selected_platforms,
                    event_url=event_url,
                )

                messages.success(
                    request,
                    "Campaign plan generated. Review the timeline, "
                    "event facts, and copy before publishing.",
                )

            except ValueError as exc:
                messages.error(request, str(exc))

            except Exception:
                logger.exception(
                    "Unexpected Marketing Studio generation error "
                    "for user %s.",
                    request.user.pk,
                )
                messages.error(
                    request,
                    "The campaign could not be generated. "
                    "Please try again or check server logs.",
                )

        elif (
            form_fields_valid
            and selected_event
            and action == "save"
        ):
            if not campaign_content:
                messages.error(
                    request,
                    "Add or generate campaign content before saving.",
                )

            else:
                campaign_name = (
                    campaign_name
                    or f"{selected_event.title} Campaign"
                )

                draft = selected_draft

                if draft and draft.event_id != selected_event.pk:
                    messages.error(
                        request,
                        "You cannot change the event associated with an existing draft.",
                    )

                else:
                    if draft is None:
                        draft = CampaignDraft(
                            host=request.user,
                            event=selected_event,
                        )

                    draft.name = campaign_name
                    draft.objective = selected_objective
                    draft.platforms = selected_platforms

                    # Preserve existing content keys while updating draft_text.
                    old_content = (
                        draft.content
                        if isinstance(draft.content, dict)
                        else {}
                    )

                    draft.content = {
                        **old_content,
                        "draft_text": campaign_content,
                        "format_version": 2,
                    }

                    draft.status = "draft"

                    try:
                        draft.full_clean()
                        draft.save()

                        messages.success(
                            request,
                            "Campaign saved. You can reopen and reuse it at any time.",
                        )

                        return redirect(
                            f"{reverse('marketing_studio:studio')}"
                            f"?draft={draft.pk}"
                        )

                    except ValidationError as exc:
                        if hasattr(exc, "message_dict"):
                            errors = [
                                str(message)
                                for field_errors in exc.message_dict.values()
                                for message in field_errors
                            ]
                        else:
                            errors = exc.messages

                        messages.error(
                            request,
                            "The campaign could not be saved: "
                            + " ".join(errors),
                        )

    else:
        draft_id = request.GET.get("draft", "").strip()

        if draft_id:
            selected_draft = _owned_draft(
                request.user,
                draft_id,
            )

            if selected_draft:
                selected_event = selected_draft.event
                selected_objective = selected_draft.objective
                selected_platforms = selected_draft.platforms or []
                campaign_name = selected_draft.name or ""
                campaign_content = _draft_text(selected_draft.content)

            else:
                messages.error(
                    request,
                    "That campaign draft was not found in your account.",
                )

    tracked_links = _tracked_campaign_links(
        selected_draft,
        request,
    )

    context = {
        "events": events,
        "drafts": drafts,
        "objectives": objectives,
        "available_platforms": available_platforms,
        "selected_event": selected_event,
        "selected_draft": selected_draft,
        "selected_objective": selected_objective,
        "selected_platforms": selected_platforms,
        "campaign_name": campaign_name,
        "name": campaign_name,
        "campaign_content": campaign_content,
        "tracked_links": tracked_links,
    }

    return render(
        request,
        "marketing_studio/studio.html",
        context,
    )



######################
from urllib.parse import urlencode


def _tracked_campaign_links(draft, request):
    """Build a separate, attributable public event URL for each platform."""
    if not draft or not draft.event_id:
        return []

    event_url = _event_public_url(draft.event, request)
    if not event_url:
        return []

    platform_labels = dict(CampaignDraft.PLATFORM_CHOICES)
    selected_platforms = draft.platforms or []
    links = []

    for platform in selected_platforms:
        if platform not in platform_labels:
            continue

        tracking_params = {
            "utm_source": platform,
            "utm_medium": "social",
            "utm_campaign": str(draft.pk),
            "utm_content": platform,
        }

        links.append({
            "platform": platform,
            "label": platform_labels[platform],
            "url": f"{event_url}?{urlencode(tracking_params)}",
        })

    return links
