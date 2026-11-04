from __future__ import annotations

import json
import logging
from pathlib import Path

from openai import OpenAIError
from pydantic import ValidationError

from digest.ai.client import DigestAI
from digest.ai.sanitize import sanitize_text, strip_html
from digest.models import EnrichedItem, Item

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "item_review.md").read_text(encoding="utf-8")
OUTPUT_FIELDS = {"relevance", "ai_summary", "release_status", "action", "action_reason"}


async def review_item(item: Item, ai: DigestAI) -> EnrichedItem:
    if ai._client is None:
        return EnrichedItem(
            **item.model_dump(),
            relevance="medium",
            ai_summary=sanitize_text(
                (item.summary_raw or item.content_text or item.title)[:600],
                {item.id},
            ),
            action="monitor",
            action_reason=(
                "Deterministic dry-run fallback; configure Foundry for analyst review."
            ),
        )
    payload = {
        "id": item.id,
        "title": item.title,
        "source": item.source_title,
        "area": item.area,
        "text": strip_html(item.content_text or item.summary_raw)[:6000],
        "schema": {
            "relevance": ["high", "medium", "low", "ignore"],
            "ai_summary": "2-3 sentences",
            "release_status": ["preview", "ga", "retirement", "announcement", "unknown"],
            "action": ["look_into", "test", "inform_customers", "monitor", "ignore"],
            "action_reason": "short explanation",
        },
    }
    last_error: Exception | None = None
    for _ in range(2):
        try:
            response = await ai.json_completion(SYSTEM_PROMPT, json.dumps(payload))
            response["ai_summary"] = sanitize_text(str(response.get("ai_summary", "")), {item.id})
            response["action_reason"] = sanitize_text(
                str(response.get("action_reason", "")), {item.id}
            )
            return EnrichedItem(
                **item.model_dump(),
                **{key: value for key, value in response.items() if key in OUTPUT_FIELDS},
            )
        except (ValidationError, ValueError, KeyError, OpenAIError) as exc:
            logger.warning(
                "Item review failed for %s (%s: %s)", item.title[:80], type(exc).__name__, exc
            )
            last_error = exc
    return EnrichedItem(
        **item.model_dump(),
        relevance="medium",
        ai_summary=sanitize_text(
            (item.summary_raw or item.content_text or item.title)[:600], {item.id}
        ),
        action="monitor",
        action_reason=f"AI review failed validation; used source summary ({last_error}).",
        review_failed=True,
    )
