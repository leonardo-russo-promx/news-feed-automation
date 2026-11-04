from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import perf_counter

import httpx

from digest.ai.client import DigestAI
from digest.ai.item_review import review_item
from digest.ai.weekly_summary import weekly_summary
from digest.collectors.learn_search import LearnSearchCollector
from digest.collectors.page_diff import PageDiffCollector
from digest.collectors.rss import RssCollector
from digest.config import load_settings
from digest.models import Digest, EnrichedItem, Item, SourceResult
from digest.pipeline.dedupe import deduplicate
from digest.pipeline.enrich import enrich_content
from digest.pipeline.filter import filter_source_items, in_lookback
from digest.registry import load_sources
from digest.render.render import copy_stylesheet, render, write_index
from digest.source_health import sync_github_issues, update_health
from digest.state.store import JsonStore

logger = logging.getLogger(__name__)


def state_dir() -> Path:
    return Path(os.getenv("DIGEST_STATE_DIR", "state"))


def _week_bounds(value: str | None) -> tuple[str, date, date]:
    if value:
        year_text, week_text = value.split("-W", 1)
        monday = date.fromisocalendar(int(year_text), int(week_text), 1)
        week = value
    else:
        today = datetime.now(UTC).date()
        year, number, _ = today.isocalendar()
        week = f"{year}-W{number:02d}"
        monday = date.fromisocalendar(year, number, 1)
    return week, monday, monday + timedelta(days=6)


def _fixture_items() -> list[Item]:
    path = Path("tests/fixtures/items.json")
    if not path.exists():
        return []
    values = json.loads(path.read_text(encoding="utf-8"))
    return [Item.model_validate(value) for value in values]


def _write_source_status(sources, health: dict) -> None:
    counts = _health_counts(sources, health)
    rows = [
        "**Health counts:** " + " · ".join(f"{key}: {value}" for key, value in counts.items()),
        "",
        "| id | title | health | last success | items (30d) | last error |",
        "|---|---|---|---|---:|---|",
    ]
    for source in sources:
        status = health.get(source.id, {})
        rows.append(
            f"| {source.id} | {source.title} | {status.get('health', 'healthy')} | "
            f"{status.get('last_success') or '-'} | {status.get('items_30d', 0)} | "
            f"{str(status.get('last_error') or '-').replace('|', '/')} |"
        )
    Path(os.getenv("DIGEST_STATE_DIR", "state")).mkdir(parents=True, exist_ok=True)
    Path(os.getenv("DIGEST_STATE_DIR", "state"), "STATUS.md").write_text(
        "# Technology Digest source status\n\n" + "\n".join(rows) + "\n",
        encoding="utf-8",
    )


def _health_counts(sources, health: dict) -> dict[str, int]:
    states = ("healthy", "stale", "failing", "suspended")
    counts = {state: 0 for state in states}
    for source in sources:
        state = health.get(source.id, {}).get("health", "healthy")
        counts[state if state in counts else "healthy"] += 1
    return counts


async def _collect_source(
    source,
    client: httpx.AsyncClient,
    pages: dict,
    prior_health: dict | None = None,
) -> tuple[list[Item], SourceResult]:
    started = perf_counter()
    if not source.enabled:
        return [], SourceResult(source_id=source.id, status="skipped")
    if source.has_todo_configuration:
        return [], SourceResult(
            source_id=source.id, status="skipped", error="source has TODO configuration"
        )
    try:
        feed_metadata = {}
        if source.method == "rss":
            collector = RssCollector()
            old = prior_health or {}
            validators = {}
            if old.get("etag"):
                validators["If-None-Match"] = old["etag"]
            if old.get("last_modified"):
                validators["If-Modified-Since"] = old["last_modified"]
            items = await collector.collect(source, client, validators)
            feed_metadata = {
                "resolved_feed_url": collector.resolved_feed_url,
                "etag": collector.etag,
                "last_modified": collector.last_modified,
                "warning": collector.warning,
            }
        elif source.method == "learn_search":
            collector = LearnSearchCollector()
            old = prior_health or {}
            validators = {}
            if old.get("etag"):
                validators["If-None-Match"] = old["etag"]
            if old.get("last_modified"):
                validators["If-Modified-Since"] = old["last_modified"]
            items = await collector.collect(source, client, validators)
            feed_metadata = {
                "warning": collector.warning,
                "etag": collector.etag,
                "last_modified": collector.last_modified,
            }
        elif source.method == "page":
            collector = PageDiffCollector()
            old = prior_health or {}
            validators = {}
            if old.get("etag"):
                validators["If-None-Match"] = old["etag"]
            if old.get("last_modified"):
                validators["If-Modified-Since"] = old["last_modified"]
            items = await collector.collect(source, client, pages, validators)
            feed_metadata = {
                "warning": collector.warning,
                "etag": collector.etag,
                "last_modified": collector.last_modified,
            }
        else:
            raise ValueError(f"unsupported collector method: {source.method}")
        return items, SourceResult(
            source_id=source.id,
            status="ok" if items else "empty",
            item_count=len(items),
            duration_ms=int((perf_counter() - started) * 1000),
            **feed_metadata,
        )
    except Exception as exc:
        return [], SourceResult(
            source_id=source.id,
            status="error",
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=int((perf_counter() - started) * 1000),
        )


async def run_digest(
    *,
    dry_run: bool,
    week_arg: str | None,
    force: bool,
    source_filter: str | None,
    reset_seen: bool = False,
) -> int:
    settings = load_settings()
    now = datetime.now(UTC)
    week, period_start, period_end = _week_bounds(week_arg)
    sources = load_sources()
    if source_filter:
        sources = [source for source in sources if source.id == source_filter]
        if not sources:
            raise ValueError(f"unknown source id: {source_filter}")

    runs = JsonStore(state_dir() / "runs.json")
    seen_store = JsonStore(state_dir() / "seen.json")
    pages_store = JsonStore(state_dir() / "pages.json")
    health_store = JsonStore(state_dir() / "source_health.json")
    run_state = runs.read()
    if not dry_run and not force and week in run_state:
        print(f"Digest for {week} already exists; use --force to rerun.")
        return 0

    if dry_run:
        items = _fixture_items()
        results = [
            SourceResult(source_id=source.id, status="skipped", error="fixture-only dry-run")
            for source in sources
        ]
    else:
        pages = pages_store.read()
        old_health = health_store.read()
        async with httpx.AsyncClient(timeout=httpx.Timeout(20), follow_redirects=True) as client:
            collected = await asyncio.gather(
                *(
                    _collect_source(
                        source,
                        client,
                        pages,
                        old_health.get(source.id, {}),
                    )
                    for source in sources
                )
            )
            collected_by_source = {
                source.id: source_items
                for source, (source_items, _) in zip(sources, collected, strict=True)
            }
            items = [item for source_items, _ in collected for item in source_items]
        results = [result for _, result in collected]
    for result in results:
        line = (
            f"source={result.source_id} status={result.status} "
            f"items={result.item_count} duration_ms={result.duration_ms}"
        )
        if result.error:
            line += f" error={result.error}"
        if result.warning:
            line += f" warning={result.warning}"
        print(line)
    if not dry_run:
        new_health = {}
        for source, result in zip(sources, results, strict=True):
            source_items = collected_by_source[source.id]
            item_dates = [
                {"id": item.id, "at": (item.updated_at or item.published_at).isoformat()}
                for item in source_items
                if item.updated_at or item.published_at
            ]
            newest = max(
                (
                    (item.updated_at or item.published_at).isoformat()
                    for item in source_items
                    if item.updated_at or item.published_at
                ),
                default=None,
            )
            previous = old_health.get(source.id, {})
            new_health[source.id] = update_health(
                source,
                previous,
                result,
                newest,
                suspend_after_failures=settings.suspend_after_failures,
                stale_after_days=settings.stale_after_days,
                item_dates=item_dates,
                now=now,
            )
            if previous.get("health") == "suspended" and result.status == "error":
                items = [item for item in items if item.source_id != source.id]
        health_store.write(new_health)
        _write_source_status(sources, new_health)
        try:
            await sync_github_issues(sources, new_health)
        except httpx.HTTPError as exc:
            print(f"Warning: source health issue reporting failed: {exc}")

    if dry_run:
        new_health = {}
    filtered: list[Item] = []
    sources_by_id = {source.id: source for source in sources}
    items_by_source: dict[str, list[Item]] = {}
    for item in items:
        source = sources_by_id.get(item.source_id)
        if source and in_lookback(
            item,
            settings.learn_lookback_days
            if source.method == "learn_search"
            else settings.lookback_days,
            now,
        ):
            items_by_source.setdefault(source.id, []).append(item)
    for source_id, source_items in items_by_source.items():
        filtered.extend(
            filter_source_items(
                source_items,
                sources_by_id[source_id],
                settings_categories=settings.roadmap_products,
            )
        )
    filtered.sort(
        key=lambda item: item.updated_at or item.published_at or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    filtered = filtered[: settings.max_items_llm]
    seen = {} if reset_seen else seen_store.read()
    fresh = deduplicate(filtered, seen, now)
    logger.warning(
        "Pipeline counts: collected=%d after_filter=%d fresh_after_dedupe=%d dry_run=%s",
        len(items),
        len(filtered),
        len(fresh),
        dry_run,
    )
    if not dry_run and not (os.getenv("FOUNDRY_ENDPOINT") and os.getenv("FOUNDRY_API_KEY")):
        raise ValueError("FOUNDRY_ENDPOINT and FOUNDRY_API_KEY are required for a live digest run")
    ai = DigestAI()
    try:
        if not dry_run:
            await ai.preflight()
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(20), follow_redirects=True
            ) as client:
                await enrich_content(fresh, client)
        enriched: list[EnrichedItem] = []
        for item in fresh:
            enriched.append(await review_item(item, ai))
        retry_ids = {item.id for item in enriched if item.review_failed}
        if retry_ids:
            logger.warning(
                "%d of %d items failed AI review; they will be retried on the next run",
                len(retry_ids),
                len(enriched),
            )
            for item_id in retry_ids:
                seen.pop(item_id, None)
        summary, actions = await weekly_summary(enriched, ai)
        logger.warning(
            "Weekly summary result: %d summary lines, %d calls to action",
            len(summary),
            len(actions),
        )
    finally:
        await ai.close()

    sections: dict[str, list[EnrichedItem]] = {}
    for item in enriched:
        sections.setdefault(item.area, []).append(item)
    failures = [result for result in results if result.status == "error"]
    health_counts = _health_counts(sources, new_health) if not dry_run else {}
    failures_for_render = [
        {
            "title": next(
                (source.title for source in sources if source.id == failure.source_id),
                failure.source_id,
            ),
            "url": str(
                next(
                    (source.website_url for source in sources if source.id == failure.source_id), ""
                )
            ),
            "error": failure.error or "collection failed",
        }
        for failure in failures
    ]
    digest = Digest(
        iso_week=week,
        period_start=period_start,
        period_end=period_end,
        management_summary=summary[:7],
        calls_to_action=actions,
        sections=sections,
        stats={
            "items_collected": len(items),
            "items_in_digest": len(enriched),
            "sources": len(results),
        },
        failed_sources=failures,
    )
    out = Path("out")
    out.mkdir(exist_ok=True)
    if dry_run:
        archive_path = out / "archive.html"
    else:
        archive_dir = Path(os.getenv("ARCHIVE_DIR", "archive"))
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / f"{week}.html"
    archive_html = render(
        title="Technology Digest",
        iso_week=week,
        management_summary=digest.management_summary,
        calls_to_action=digest.calls_to_action,
        sections=digest.sections,
        failed_sources=failures_for_render,
        health_counts=health_counts,
    )
    archive_path.write_text(archive_html, encoding="utf-8")
    if not dry_run:
        write_index(archive_path.parent)
    (out / "archive.html").write_text(archive_html, encoding="utf-8")
    copy_stylesheet(out)
    (out / "run.json").write_text(
        json.dumps(
            {
                "digest": digest.model_dump(mode="json"),
                "source_results": [r.model_dump(mode="json") for r in results],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if not dry_run:
        if failures:
            try:
                await ensure_digest_problems_issue(failures)
            except httpx.HTTPError as exc:
                print(f"Warning: source failure issue update failed: {exc}")
        seen_store.write(seen)
        pages_store.write(pages)
        run_state[week] = {"completed_at": now.isoformat(), "item_count": len(enriched)}
        runs.write(run_state)
        export_opml()
    summary_file = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_file:
        with Path(summary_file).open("a", encoding="utf-8") as stream:
            stream.write(f"## Technology Digest {week}\n\nItems: {len(enriched)}\n\n")
            stream.write(
                "**Source health:** "
                + " · ".join(f"{key}: {value}" for key, value in health_counts.items())
                + "\n\n"
            )
            status_path = state_dir() / "STATUS.md"
            if status_path.exists():
                stream.write(status_path.read_text(encoding="utf-8") + "\n")
            stream.write(f"Sources with errors: {len(failures)}\n")
    print(f"Wrote out/archive.html ({len(enriched)} items).")
    return 0


async def validate_sources(source_ids: list[str] | None) -> int:
    sources = load_sources()
    if source_ids:
        sources = [source for source in sources if source.id in source_ids]
    if not sources:
        print("No matching sources.")
        return 1
    async with httpx.AsyncClient(timeout=httpx.Timeout(20), follow_redirects=True) as client:
        checks = await asyncio.gather(
            *(_collect_source(source, client, {}, set()) for source in sources)
        )
    now = datetime.now(UTC)
    for source, (items, result) in zip(sources, checks, strict=True):
        newest = max(
            (
                item.updated_at or item.published_at
                for item in items
                if item.updated_at or item.published_at
            ),
            default=None,
        )
        recent_count = sum(
            1
            for item in items
            if (item.updated_at or item.published_at) and in_lookback(item, 30, now)
        )
        print(
            f"{source.id}: {result.status}; items_30d={recent_count}; "
            f"newest={newest.isoformat() if newest else 'unknown'}; "
            f"feed={result.resolved_feed_url or source.feed_url or 'n/a'}"
            + (f"; error={result.error}" if result.error else "")
            + (f"; warning={result.warning}" if result.warning else "")
        )
    return 1 if any(result.status == "error" for _, result in checks) else 0


def export_opml() -> int:
    sources = load_sources()
    from xml.etree.ElementTree import Element, SubElement, tostring

    opml = Element("opml", version="2.0")
    head = SubElement(opml, "head")
    SubElement(head, "title").text = "Technology Digest Sources"
    body = SubElement(opml, "body")
    areas: dict[str, list] = {}
    source_health = JsonStore(state_dir() / "source_health.json").read()
    for source in sources:
        if source.enabled and source.method == "rss":
            areas.setdefault(source.area, []).append(source)
    for area, area_sources in sorted(areas.items()):
        group = SubElement(body, "outline", text=area)
        for source in area_sources:
            url = str(
                source_health.get(source.id, {}).get("resolved_feed_url")
                or source.feed_url
                or source.website_url
                or ""
            )
            SubElement(
                group,
                "outline",
                text=source.title,
                title=source.title,
                type="rss",
                xmlUrl=url,
                htmlUrl=str(source.website_url or ""),
            )
    state_dir().mkdir(exist_ok=True)
    (state_dir() / "sources.opml").write_bytes(
        b'<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(opml, encoding="utf-8")
    )
    return 0


async def ensure_digest_problems_issue(failures: list[SourceResult]) -> None:
    token = os.getenv("GITHUB_TOKEN")
    repository = os.getenv("GITHUB_REPOSITORY")
    if not token or not repository:
        return
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    api = os.getenv("GITHUB_API_URL", "https://api.github.com")
    async with httpx.AsyncClient(timeout=20, headers=headers) as client:
        endpoint = f"{api}/repos/{repository}/issues"
        response = await client.get(endpoint, params={"state": "open", "per_page": 100})
        response.raise_for_status()
        issue = next(
            (item for item in response.json() if item.get("title") == "Digest source problems"),
            None,
        )
        body = "## Current source errors\n\n" + "\n".join(
            f"- `{failure.source_id}`: {failure.error or 'unknown error'}" for failure in failures
        )
        if issue:
            response = await client.patch(issue["url"], json={"body": body})
            response.raise_for_status()
        else:
            response = await client.post(
                endpoint, json={"title": "Digest source problems", "body": body}
            )
            response.raise_for_status()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m digest")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "dry-run"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--week")
        command_parser.add_argument("--force", action="store_true")
        command_parser.add_argument("--source")
        command_parser.add_argument(
            "--reset-seen",
            action="store_true",
            help="Ignore previously seen items so they are processed again",
        )
    validate = subparsers.add_parser("validate-sources")
    validate.add_argument("--only", nargs="+")
    subparsers.add_parser("export-opml")
    args = parser.parse_args()
    logging.basicConfig(
        level=os.getenv("DIGEST_LOG_LEVEL", "WARNING").upper(),
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        if args.command in {"run", "dry-run"}:
            status = asyncio.run(
                run_digest(
                    dry_run=args.command == "dry-run",
                    week_arg=args.week,
                    force=args.force,
                    source_filter=args.source,
                    reset_seen=args.reset_seen,
                )
            )
        elif args.command == "validate-sources":
            status = asyncio.run(validate_sources(args.only))
        else:
            status = export_opml()
    except Exception as exc:
        parser.exit(1, f"digest: {type(exc).__name__}: {exc}\n")
    raise SystemExit(status)
