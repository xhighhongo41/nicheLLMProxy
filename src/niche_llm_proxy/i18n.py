"""Internationalization helpers for nicheLLM Proxy user-facing messages."""

from __future__ import annotations

import gettext
import os
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any


DEFAULT_LANGUAGE = "en"
"""Language used when no supported language is explicitly selected."""

LANGUAGE_ENV_VAR = "NICHELLM_LANGUAGE"
"""Environment variable that selects the process-wide message language."""

SUPPORTED_LANGUAGES = frozenset({"en", "ja"})
"""Languages with translations supplied by this package."""

_DOMAIN = "niche_llm_proxy"
_LOCALE_DIR = Path(__file__).with_name("locales")


def normalize_language(value: str | None) -> str:
    """Return a supported language code, falling back to English safely."""

    if not value:
        return DEFAULT_LANGUAGE

    language = value.strip().lower().replace("_", "-").split("-", maxsplit=1)[0]
    return language if language in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


def get_language(environ: Mapping[str, str] | None = None) -> str:
    """Read and normalize the configured process-wide message language."""

    environment = os.environ if environ is None else environ
    return normalize_language(environment.get(LANGUAGE_ENV_VAR))


@lru_cache
def _translation_for(language: str) -> gettext.NullTranslations:
    """Load a catalog, returning untranslated English text if it is unavailable."""

    return gettext.translation(
        _DOMAIN,
        localedir=_LOCALE_DIR,
        languages=[language],
        fallback=True,
    )


def translate(message_id: str, /, **values: Any) -> str:
    """Translate a message ID using the configured language and format its values."""

    message = _translation_for(get_language()).gettext(message_id)
    return message.format(**values)
