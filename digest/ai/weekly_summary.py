from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from openai import OpenAIError

from digest.ai.client import DigestAI
from digest.ai.sanitize import sanitize_text
from digest.models import CallToAction, EnrichedItem

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "weekly_summary.md").read_text(
    encoding="utf-8"
)
MIN_ACTIONS = 3


def _fallback_summary(relevant: list[EnrichedItem], valid_ids: set[str] | None) -> list[str]:
    return [sanitize_text(f"{item.title}: {item.ai_summary}", valid_ids) for item in relevant[:7]]


def _fallback_actions(relevant: list[EnrichedItem]) -> list[CallToAction]:
    return [
        CallToAction(
            title=sanitize_text(f"Review: {item.title[:100]}"),
            why=sanitize_text(item.action_reason or item.ai_summary[:240]),
            item_ids=[item.id],
        )
        for item in relevant[:MIN_ACTIONS]
    ]


def _parse_result(
    result: dict[str, Any], valid_ids: set[str]
) -> tuple[list[str], list[CallToAction]]:
    logger.warning("Weekly summary response keys: %s", sorted(result))
    summary_values = result.get("management_summary", [])
    if not isinstance(summary_values, list):
        logger.warning(
            "Weekly summary 'management_summary' is %s, expected a list",
            type(summary_values).__name__,
        )
        summary_values = []
    summary = [text for value in summary_values if (text := sanitize_text(str(value), valid_ids))]
    action_values = result.get("calls_to_action", [])
    if not isinstance(action_values, list):
        logger.warning(
            "Weekly summary 'calls_to_action' is %s, expected a list",
            type(action_values).__name__,
        )
        action_values = []
    actions: list[CallToAction] = []
    for index, action in enumerate(action_values[:5]):
        if not isinstance(action, dict):
            logger.warning("Weekly summary action %d dropped: not an object", index)
            continue
        raw_ids = action.get("item_ids", [])
        item_ids = [str(value) for value in raw_ids] if isinstance(raw_ids, list) else []
        ids = [value for value in item_ids if value in valid_ids]
        if len(ids) != len(item_ids):
            logger.warning(
                "Weekly summary action %d: dropped %d unknown item ids",
                index,
                len(item_ids) - len(ids),
            )
        title = sanitize_text(str(action.get("title", "")), valid_ids)
        why = sanitize_text(str(action.get("why", "")), valid_ids)
        if not title:
            logger.warning("Weekly summary action %d dropped: empty title", index)
            continue
        actions.append(CallToAction(title=title, why=why, item_ids=ids))
    logger.warning(
        "Weekly summary parsed: %d summary lines, %d calls to action",
        len(summary),
        len(actions),
    )
    return summary, actions


async def weekly_summary(
    items: list[EnrichedItem], ai: DigestAI
) -> tuple[list[str], list[CallToAction]]:
    relevant = [item for item in items if item.relevance != "ignore"]
    logger.warning(
        "Weekly summary input: %d items, %d relevant, AI %s",
        len(items),
        len(relevant),
        "enabled" if ai._client is not None else "disabled (deterministic fallback)",
    )
    valid_ids = {item.id for item in relevant}
    summary: list[str] = []
    actions: list[CallToAction] = []
    if not relevant:
        logger.warning("Weekly summary skipped: no relevant items this run; using placeholders")
    elif ai._client is None:
        summary = _fallback_summary(relevant, None)
        actions = _fallback_actions(relevant)
    else:
        payload = {
            "items": [
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
        }
        try:
            result = await ai.json_completion(SYSTEM_PROMPT, json.dumps(payload))
        except (ValueError, OpenAIError) as exc:
            logger.warning(
                "Weekly summary AI call failed (%s: %s); using fallback", type(exc).__name__, exc
            )
            result = {}
        logger.warning("Weekly summary raw AI response: %s", json.dumps(result)[:4000])
        summary, actions = _parse_result(result, valid_ids)
        if not summary:
            logger.warning("Weekly summary has no management summary; using item summaries")
            summary = _fallback_summary(relevant, valid_ids)
    if len(actions) < MIN_ACTIONS and relevant:
        logger.warning(
            "Weekly summary has %d calls to action (expected at least %d); "
            "topping up from top items",
            len(actions),
            MIN_ACTIONS,
        )
        used = {item_id for action in actions for item_id in action.item_ids}
        spare = [item for item in relevant if item.id not in used]
        actions.extend(_fallback_actions(spare)[: MIN_ACTIONS - len(actions)])
    if len(actions) < MIN_ACTIONS:
        logger.warning(
            "Weekly summary padding with %d generic placeholder actions",
            MIN_ACTIONS - len(actions),
        )
        while len(actions) < MIN_ACTIONS:
            actions.append(
                CallToAction(
                    title="Review relevant updates",
                    why="No item-specific action was generated.",
                    item_ids=[],
                )
            )
    return summary[:7], actions[:5]
