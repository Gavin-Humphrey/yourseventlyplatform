# import logging
# import os

# import requests
# from django.db import IntegrityError, transaction

# from .models import TranslationCache

# logger = logging.getLogger(__name__)

# LIBRETRANSLATE_URL = os.environ.get(
#     "LIBRETRANSLATE_URL",
#     "http://127.0.0.1:5000",
# ).rstrip("/")

# TRANSLATION_TIMEOUT = 20
# BRAND_NAME = "YoursEvently"


# def translate_text(text, target_language, source_language="en"):
#     """Translate one string, using the database cache when available."""
#     return translate_batch(
#         [text],
#         target_language=target_language,
#         source_language=source_language,
#     )[0]


# def translate_batch(texts, target_language, source_language="en"):
#     """
#     Translate multiple strings while reusing cached translations.

#     The returned list has the same length and order as the input.
#     Original text is returned for strings that cannot be translated.
#     """
#     if not texts:
#         return []

#     results = list(texts)

#     source_language = (source_language or "").strip().lower()
#     target_language = (target_language or "").strip().lower()

#     if not source_language or not target_language:
#         return results

#     if source_language == target_language:
#         return results

#     # Preserve non-string values and leave blank strings unchanged.
#     pending = {}
#     for index, text in enumerate(texts):
#         if not isinstance(text, str) or not text.strip():
#             continue

#         if text.strip() == BRAND_NAME:
#             continue

#         pending.setdefault(text, []).append(index)

#     if not pending:
#         return results

#     unique_texts = list(pending.keys())

#     try:
#         cached_entries = TranslationCache.objects.filter(
#             source_text__in=unique_texts,
#             source_language=source_language,
#             target_language=target_language,
#         )

#         cached = {
#             entry.source_text: entry.translated_text
#             for entry in cached_entries
#         }

#         missing = [
#             text for text in unique_texts
#             if text not in cached
#         ]

#         translated = {}

#         if missing:
#             placeholders = {
#                 text: f"YEVBRANDTOKEN{i}END"
#                 for i, text in enumerate(missing)
#                 if BRAND_NAME in text
#             }

#             protected_texts = [
#                 text.replace(BRAND_NAME, placeholders[text])
#                 if text in placeholders else text
#                 for text in missing
#             ]

#             response = requests.post(
#                 f"{LIBRETRANSLATE_URL}/translate",
#                 json={
#                     "q": protected_texts,
#                     "source": source_language,
#                     "target": target_language,
#                     "format": "text",
#                 },
#                 timeout=TRANSLATION_TIMEOUT,
#             )
#             response.raise_for_status()
#             payload = response.json()

            
#             # LibreTranslate returns a dictionary containing translatedText.
#             if isinstance(payload, dict):
#                 translated_value = payload.get("translatedText")

#                 if isinstance(translated_value, list):
#                     translated_items = translated_value
#                 elif isinstance(translated_value, str) and len(missing) == 1:
#                     translated_items = [translated_value]
#                 else:
#                     translated_items = []
#             else:
#                 translated_items = []


#             # Reject an incomplete response rather than mismatching strings.
#             if len(translated_items) != len(missing):
#                 logger.warning(
#                     "LibreTranslate returned an unexpected batch response."
#                 )
#                 return results

#             for original, value in zip(missing, translated_items):
#                 if not isinstance(value, str) or not value:
#                     continue

#                 placeholder = placeholders.get(original)
#                 if placeholder:
#                     value = value.replace(placeholder, BRAND_NAME)

#                 translated[original] = value

#             # Cache successful translations.
#             if translated:
#                 try:
#                     with transaction.atomic():
#                         TranslationCache.objects.bulk_create(
#                             [
#                                 TranslationCache(
#                                     source_text=original,
#                                     source_language=source_language,
#                                     target_language=target_language,
#                                     translated_text=value,
#                                 )
#                                 for original, value in translated.items()
#                             ],
#                             ignore_conflicts=True,
#                         )
#                 except (IntegrityError, Exception):
#                     # A cache write failure must not break page rendering.
#                     logger.exception(
#                         "Could not save one or more cached translations."
#                     )

#         # Prefer cached values, then newly translated values.
#         available = {**cached, **translated}

#         for original, indices in pending.items():
#             value = available.get(original)
#             if value is not None:
#                 for index in indices:
#                     results[index] = value

#         return results

#     except (requests.RequestException, ValueError, TypeError):
#         logger.exception(
#             "LibreTranslate batch failed for language %s.",
#             target_language,
#         )
#         return results

import logging
import os

import requests
from django.db import IntegrityError, transaction

from .models import TranslationCache

logger = logging.getLogger(__name__)

LIBRETRANSLATE_URL = os.environ.get(
    "LIBRETRANSLATE_URL",
    "http://127.0.0.1:5000",
).rstrip("/")

TRANSLATION_TIMEOUT = 20
BRAND_NAME = "YoursEvently"


def translate_text(text, target_language, source_language="en"):
    """Translate one string, using the database cache when available."""
    return translate_batch(
        [text],
        target_language=target_language,
        source_language=source_language,
    )[0]


def translate_batch(texts, target_language, source_language="en"):
    """
    Translate multiple strings while reusing cached translations.

    The returned list has the same length and order as the input.
    Original text is returned for strings that cannot be translated.
    """
    if not texts:
        return []

    results = list(texts)

    source_language = (source_language or "").strip().lower()
    target_language = (target_language or "").strip().lower()

    if not source_language or not target_language:
        return results

    if source_language == target_language:
        return results

    # Preserve non-string values and leave blank strings unchanged.
    pending = {}
    for index, text in enumerate(texts):
        if not isinstance(text, str) or not text.strip():
            continue

        if text.strip() == BRAND_NAME:
            continue

        pending.setdefault(text, []).append(index)

    if not pending:
        return results

    unique_texts = list(pending.keys())

    try:
        cached_entries = TranslationCache.objects.filter(
            source_text__in=unique_texts,
            source_language=source_language,
            target_language=target_language,
        )

        cached = {
            entry.source_text: entry.translated_text
            for entry in cached_entries
        }

        missing = [
            text for text in unique_texts
            if text not in cached
        ]

        translated = {}

        if missing:
            for text in missing:
                protected_text = text
                placeholder = None
                
                if BRAND_NAME in text:
                    placeholder = "YEVBRANDTOKENEND"
                    protected_text = text.replace(BRAND_NAME, placeholder)

                try:
                    response = requests.post(
                        f"{LIBRETRANSLATE_URL}/translate",
                        json={
                            "q": protected_text,
                            "source": source_language,
                            "target": target_language,
                            "format": "text",
                        },
                        timeout=TRANSLATION_TIMEOUT,
                    )
                    response.raise_for_status()
                    payload = response.json()

                    if isinstance(payload, dict):
                        translated_value = payload.get("translatedText")
                        if isinstance(translated_value, str) and translated_value:
                            if placeholder:
                                translated_value = translated_value.replace(placeholder, BRAND_NAME)
                            translated[text] = translated_value
                except requests.RequestException:
                    logger.warning(
                        "Failed to translate string via LibreTranslate: %s", text[:30]
                    )
                    continue

            # Cache successful translations.
            if translated:
                try:
                    with transaction.atomic():
                        TranslationCache.objects.bulk_create(
                            [
                                TranslationCache(
                                    source_text=original,
                                    source_language=source_language,
                                    target_language=target_language,
                                    translated_text=value,
                                )
                                for original, value in translated.items()
                            ],
                            ignore_conflicts=True,
                        )
                except (IntegrityError, Exception):
                    # A cache write failure must not break page rendering.
                    logger.exception(
                        "Could not save one or more cached translations."
                    )

        # Prefer cached values, then newly translated values.
        available = {**cached, **translated}

        for original, indices in pending.items():
            value = available.get(original)
            if value is not None:
                for index in indices:
                    results[index] = value

        return results

    except (requests.RequestException, ValueError, TypeError):
        logger.exception(
            "LibreTranslate processing failed for language %s.",
            target_language,
        )
        return results
