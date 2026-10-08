"""Message catalog: reviewer/i18n/<lang>.yaml, one function `t(key, lang, **kw)`.

Keys are dotted paths into the YAML (`mr.review_failed`). A key missing in
the requested language falls back to English with a WARNING; an unknown
language reads as English. Every user-facing text (MR notes, notifications)
comes from here — a new channel or language never needs `if lang == ...`.
"""

from __future__ import annotations

import logging
from functools import cache
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

CATALOG_DIR = Path(__file__).parent
DEFAULT_LANG = "en"


def _flatten(tree: dict[str, Any], prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in tree.items():
        key = f"{prefix}{name}"
        if isinstance(value, dict):
            out.update(_flatten(value, key + "."))
        else:
            out[key] = str(value)
    return out


@cache
def catalog(lang: str) -> dict[str, str]:
    """The flattened catalog of one language ({} when there is none)."""
    path = CATALOG_DIR / f"{lang}.yaml"
    if not path.is_file():
        return {}
    return _flatten(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def languages() -> list[str]:
    return sorted(p.stem for p in CATALOG_DIR.glob("*.yaml"))


def has(key: str, lang: str = DEFAULT_LANG) -> bool:
    return key in catalog(lang) or key in catalog(DEFAULT_LANG)


def t(key: str, lang: str = DEFAULT_LANG, **kwargs: object) -> str:
    """Message `key` in `lang`, formatted with kwargs (str.format fields)."""
    template = catalog(lang).get(key)
    if template is None:
        if lang in languages():
            logger.warning("i18n: %r missing in %s — using %s", key, lang, DEFAULT_LANG)
        template = catalog(DEFAULT_LANG).get(key)
        if template is None:
            raise KeyError(f"i18n key {key!r} is not in the {DEFAULT_LANG} catalog")
    return template.format(**kwargs) if kwargs else template
