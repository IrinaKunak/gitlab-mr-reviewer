"""Stage 5 — EN->RU translation of what gets delivered (prompts are
English-only; translation is a stage, not a prompt instruction)."""

from __future__ import annotations

import logging
from typing import Any

from ... import prompts
from ...ai_client import AIError
from ...domain.models import Tier
from .base import ReviewContext

logger = logging.getLogger(__name__)

# long documents overwhelm Haiku: it starts leaving half the prose in English
# mid-sentence (dev feedback 2026-07-23) — main tier handles them
FAST_TIER_MAX_CHARS = 3500


class Translator:
    def __init__(self, ai: Any, language: str) -> None:
        self.ai = ai
        self.language = language

    async def translate(self, text: str, tier: Tier) -> str:
        if self.language != "ru" or not text:
            return text
        if tier == Tier.FAST and len(text) > FAST_TIER_MAX_CHARS:
            tier = Tier.MAIN
        try:
            result = await self.ai.complete(
                tier, prompts.TRANSLATE_SYSTEM, prompts.translate_user_prompt(text),
                max_tokens=max(2048, min(16000, len(text))), use_cache=True)
        except AIError as exc:
            logger.error("translation failed, delivering English original: %s", exc)
            return text
        out = (result.text or "").strip()
        # invariant: a RU translation contains Cyrillic; anything else is model
        # commentary (e.g. Haiku asking for "the document") — deliver the original
        if out and any("Ѐ" <= ch <= "ӿ" for ch in out):
            return out
        logger.warning("translation output had no Cyrillic — delivering English original")
        return text


class Translate:
    """The review (+ impact analysis). The tester report is translated by its
    own delivery stage, after the review is out."""

    def __init__(self, translator: Translator) -> None:
        self.translator = translator

    async def run(self, ctx: ReviewContext) -> ReviewContext:
        ctx.review_out = await self.translator.translate(ctx.review_en, tier=Tier.FAST)
        return ctx
