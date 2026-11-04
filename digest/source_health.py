from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from digest.models import Source, SourceResult


def update_health(
    source: Source,
    previous: dict[str, Any],
    result: SourceResult,
    newest_item_at: str | None,
    *,
    suspend_after_failures: int = 3,
    stale_after_days: int = 42,
    item_dates: list[dict[str, str]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(UTC)
    failures = int(previous.get("consecutive_failures", 0))
    last_error = previous.get("last_error")
    last_error_at = previous.get("last_error_at")
    last_success = previous.get("last_success")
    suspended_since = previous.get("suspended_since")

    date_by_id = {
        str(entry["id"]): str(entry["at"])
        for entry in previous.get("item_dates", [])
        if isinstance(entry, dict) and "id" in entry and "at" in entry
    }
    if item_dates:
        date_by_id.update({entry["id"]: entry["at"] for entry in item_dates})
    cutoff = current - timedelta(days=30)
    date_by_id = {
        item_id: timestamp
        for item_id, timestamp in date_by_id.items()
        if _datetime(timestamp) >= cutoff
    }

    if result.status == "error":
        failures += 1
        last_error = result.error
        last_error_at = current.isoformat()
    elif result.status in {"ok", "empty"}:
        failures = 0
        last_success = current.isoformat()
        last_error = None
        last_error_at = None
        suspended_since = None

    if failures >= suspend_after_failures:
        health = "suspended"
        suspended_since = suspended_since or current.isoformat()
    elif failures:
        health = "failing"
    elif newest_item_at and current - _datetime(newest_item_at) > timedelta(days=stale_after_days):
        health = "stale"
    else:
        health = "healthy"
    return {
        "last_check": current.isoformat(),
        "last_success": last_success,
        "last_error": last_error,
        "last_error_at": last_error_at,
        "consecutive_failures": failures,
        "items_last_run": result.item_count,
        "items_30d": len(date_by_id),
        "item_dates": [
            {"id": item_id, "at": timestamp} for item_id, timestamp in date_by_id.items()
        ],
        "newest_item_at": newest_item_at or previous.get("newest_item_at"),
        "resolved_feed_url": result.resolved_feed_url or previous.get("resolved_feed_url"),
        "etag": result.etag or previous.get("etag"),
        "last_modified": result.last_modified or previous.get("last_modified"),
        "health": health,
        "suspended_since": suspended_since,
    }


def _datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


async def sync_github_issues(sources: list[Source], health: dict[str, dict[str, Any]]) -> None:
    token = os.getenv("GITHUB_TOKEN")
    repository = os.getenv("GITHUB_REPOSITORY")
    if not token or not repository:
        return
    api = os.getenv("GITHUB_API_URL", "https://api.github.com")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=20, headers=headers) as client:
        base = f"{api}/repos/{repository}/issues"
        for label in ("source-health", "source-health-dashboard"):
            label_url = f"{api}/repos/{repository}/labels/{label}"
            check = await client.get(label_url)
            if check.status_code == 404:
                created_label = await client.post(
                    f"{api}/repos/{repository}/labels",
                    json={"name": label, "color": "d4c5f9"},
                )
                if created_label.status_code not in {201, 422}:
                    created_label.raise_for_status()
            else:
                check.raise_for_status()
        response = await client.get(base, params={"state": "open", "per_page": 100})
        response.raise_for_status()
        issues = response.json()
        rows = [
            "**Health counts:** "
            + " · ".join(
                f"{state}: {sum(1 for entry in health.values() if entry.get('health') == state)}"
                for state in ("healthy", "stale", "failing", "suspended")
            ),
            "",
            "| id | title | health | last_success | items_30d | last_error | suggested fix |",
            "|---|---|---|---|---:|---|---|",
        ]
        for source in sources:
            status = health.get(source.id, {})
            rows.append(
                f"| {source.id} | {source.title} | {status.get('health', 'healthy')} | "
                f"{status.get('last_success') or '-'} | {status.get('items_30d', 0)} | "
                f"{str(status.get('last_error') or '-').replace('|', '/')} | "
                f"{_feed_suggestion(source, status)} |"
            )
            suspended_title = f"Source suspended: {source.id}"
            matching = next(
                (issue for issue in issues if issue.get("title") == suspended_title), None
            )
            if status.get("health") == "suspended":
                body = (
                    f"Source **{source.title}** is suspended after consecutive collection failures."
                    f"\n\nLast error: {status.get('last_error') or 'unknown'}"
                    f"\n\n{_feed_suggestion(source, status)}"
                )
                if matching:
                    updated = await client.patch(matching["url"], json={"body": body})
                    updated.raise_for_status()
                else:
                    labels = ["source-health"]
                    payload: dict[str, Any] = {
                        "title": suspended_title,
                        "body": body,
                        "labels": labels,
                    }
                    if source.owner:
                        payload["assignees"] = [source.owner.removeprefix("@")]
                    created = await client.post(base, json=payload)
                    created.raise_for_status()
            elif matching:
                closed = await client.patch(matching["url"], json={"state": "closed"})
                closed.raise_for_status()

        dashboard = next(
            (
                issue
                for issue in issues
                if "source-health-dashboard" in [label["name"] for label in issue.get("labels", [])]
            ),
            None,
        )
        body = "## Current source health\n\n" + "\n".join(rows)
        if dashboard:
            updated = await client.patch(dashboard["url"], json={"body": body})
            updated.raise_for_status()
            await _pin_issue(client, f"{base}/{dashboard['number']}/pin")
        else:
            created = await client.post(
                base,
                json={
                    "title": "Source health",
                    "body": body,
                    "labels": ["source-health-dashboard"],
                },
            )
            created.raise_for_status()
            issue_number = created.json().get("number")
            if issue_number:
                await _pin_issue(client, f"{base}/{issue_number}/pin")


async def _pin_issue(client: httpx.AsyncClient, url: str) -> None:
    # The REST pin endpoint is not available for every repository/token; pinning is cosmetic.
    pinned = await client.post(url)
    if pinned.status_code not in {201, 204, 404, 422}:
        pinned.raise_for_status()


def _feed_suggestion(source: Source, status: dict[str, Any]) -> str:
    resolved = status.get("resolved_feed_url")
    if resolved and str(source.feed_url or "") != str(resolved):
        return f"Suggested fix: update feed_url to {resolved}".replace("|", "/")
    return "-"
