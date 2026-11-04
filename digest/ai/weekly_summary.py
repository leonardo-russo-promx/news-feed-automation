from __future__ import annotations

import json
import logging
from pathlib import Path

from openai import OpenAIError

from digest.ai.client import DigestAI
from digest.ai.sanitize import sanitize_text
from digest.models import CallToAction, EnrichedItem

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "weekly_summary.md").read_text(
    encoding="utf-8"
)


async def weekly_summary(
    items: list[EnrichedItem], ai: DigestAI
) -> tuple[list[str], list[CallToAction]]:
    relevant = [item for item in items if item.relevance != "ignore"]
    if ai._client is None:
        summary = [sanitize_text(f"{item.title}: {item.ai_summary}") for item in relevant[:7]]
        selected = relevant[:3]
        actions = [
            CallToAction(
                title=sanitize_text(f"Review: {item.title[:100]}"),
                why=sanitize_text(item.action_reason or item.ai_summary[:240]),
                item_ids=[item.id],
            )
            for item in selected
        ]
    else:
        payload = [
            {
                "id": item.id,
                "title": item.title,
                "area": item.area,
                "summary": item.ai_summary,
                "relevance": item.relevance,
                "action": item.action,
            }
            for item in relevant
        ]
        try:
            result = await ai.json_completion(SYSTEM_PROMPT, json.dumps(payload))
            logger.debug("Weekly summary raw AI response: %s", json.dumps(result))
        except (ValueError, OpenAIError) as exc:
            logger.warning("Weekly summary AI call failed; using fallback: %s", exc)
            result = {}
        valid_ids = {item.id for item in relevant}
        summary_values = result.get("management_summary", [])
        if not isinstance(summary_values, list):
            summary_values = []
        summary = [sanitize_text(str(value), valid_ids) for value in summary_values][:7]
        actions = []
        action_values = result.get("calls_to_action", [])
        if not isinstance(action_values, list):
            action_values = []
        for action in action_values[:5]:
            if not isinstance(action, dict):
                continue
            item_ids = action.get("item_ids", [])
            if not isinstance(item_ids, list):
                item_ids = []
            ids = [str(value) for value in item_ids if str(value) in valid_ids]
            actions.append(
                CallToAction(
                    title=sanitize_text(str(action.get("title", "")), valid_ids),
                    why=sanitize_text(str(action.get("why", "")), valid_ids),
                    item_ids=ids,
                )
            )
        if len(actions) < 3:
            logger.warning(
                "Weekly summary returned %d calls to action (expected at least 3); "
                "adding fallback actions",
                len(actions),
            )
            actions.extend(
                CallToAction(
                    title="Review the weekly digest", why="Check relevant updates.", item_ids=[]
                )
                for _ in range(3 - len(actions))
            )
        if not summary:
            summary = [
                sanitize_text(f"{item.title}: {item.ai_summary}", valid_ids)
                for item in relevant[:7]
            ]
    while len(actions) < 3:
        actions.append(
            CallToAction(
                title="Review relevant updates",
                why="No item-specific action was generated.",
                item_ids=[],
            )
        )
    return summary, actions[:5]
