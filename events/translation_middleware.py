from bs4 import BeautifulSoup, NavigableString, Comment  # Added Comment import
from django.conf import settings
from django.utils import translation

from .translation_service import translate_batch


SKIP_TAGS = {
    "script",
    "style",
    "noscript",
    "textarea",
    "option",
    "code",
    "pre",
    "svg",
    "template",
}

BATCH_SIZE = 40


def translate_html(html, target_language, source_language="en"):
    """
    Translate visible HTML text while preserving the document structure
    as much as possible. Does not translate HTML attributes or HTML comments.
    """
    if not html or target_language == source_language:
        return html

    soup = BeautifulSoup(html, "html.parser")
    html_tag = soup.find("html")
    if html_tag:
        html_tag["lang"] = target_language

    text_nodes = []

    for node in soup.find_all(string=True):
        # 1. Skip HTML Comment nodes explicitly so they don't leak into rendered HTML
        if isinstance(node, Comment):
            continue

        if not isinstance(node, NavigableString):
            continue

        if not node.strip():
            continue

        parent = node.parent

        if parent is None:
            continue

        # Skip text inside tags that must remain unchanged.
        if parent.find_parent(SKIP_TAGS) or parent.name in SKIP_TAGS:
            continue

        original = str(node)
        stripped = original.strip()

        # Preserve leading and trailing whitespace around the text.
        leading = original[: len(original) - len(original.lstrip())]
        trailing = original[len(original.rstrip()) :]

        text_nodes.append((node, stripped, leading, trailing))

    # Translate manageable batches instead of making one request per node.
    for start in range(0, len(text_nodes), BATCH_SIZE):
        batch = text_nodes[start : start + BATCH_SIZE]

        translated = translate_batch(
            [item[1] for item in batch],
            target_language=target_language,
            source_language=source_language,
        )

        for (node, original, leading, trailing), result in zip(
            batch, translated
        ):
            if isinstance(result, str) and result:
                node.replace_with(leading + result + trailing)

    result = str(soup)

    result = result.lstrip()
    if result.lower().startswith("html\n"):
        result = result[5:]

    # Restore exactly one HTML5 doctype when the source had one.
    if html.lstrip().lower().startswith("<!doctype html"):
        result = "<!DOCTYPE html>\n" + result

    return result

class TranslationMiddleware:
    """
    Translate HTML responses for non-English language selections.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        # Do not modify redirects, errors, or other non-HTML responses.
        if response.status_code != 200:
            return response

        content_type = response.get("Content-Type", "")
        if "text/html" not in content_type.lower():
            return response

        # Streaming responses cannot be processed through response.content.
        if getattr(response, "streaming", False):
            return response

        language = (
            translation.get_language()
            or getattr(request, "LANGUAGE_CODE", None)
            or settings.LANGUAGE_CODE
        )

        target_language = (language or "en").lower().split("-")[0]

        if target_language in ("", "en"):
            return response

        try:
            charset = response.charset or "utf-8"
            html = response.content.decode(charset)

            translated_html = translate_html(
                html,
                target_language=target_language,
                source_language="en",
            )

            encoded_content = translated_html.encode(charset)
            response.content = encoded_content
            
            # FIX: Explicitly update Content-Length so the browser knows 
            # exactly when the download is complete and stops the spinner!
            response["Content-Length"] = str(len(encoded_content))

        except Exception:
            # Never let translation failures break a page.
            return response

        return response