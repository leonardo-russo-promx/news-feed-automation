from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from digest.ai.weekly_summary import weekly_summary
from digest.cli import export_opml, run_digest
from digest.collectors.page_diff import PageDiffCollector
from digest.collectors.rss import parse_content
from digest.models import EnrichedItem, Item, Source, SourceResult
from digest.pipeline.dedupe import canonical_url, deduplicate, item_id
from digest.registry import load_sources, validate_schema
from digest.registry_ops import append_source, parse_issue_body, source_entry
from digest.render.render import render
from digest.source_health import update_health
from digest.state.store import JsonStore

ROOT = Path(__file__).parents[1]


def test_source_registry_validates_schema_and_pydantic() -> None:
    errors = validate_schema(ROOT / "config/sources.yaml", ROOT / "config/sources.schema.json")
    assert errors == []
    sources = load_sources(ROOT / "config/sources.yaml")
    assert len({source.id for source in sources}) == len(sources)
    assert next(
        source for source in sources if source.id == "project-operations-docs"
    ).has_todo_configuration


def test_invalid_source_fixture_fails_schema_validation() -> None:
    errors = validate_schema(
        ROOT / "tests/fixtures/invalid_sources.yaml",
        ROOT / "config/sources.schema.json",
    )
    assert any("Bad ID" in error or "matches" in error for error in errors)


def test_source_id_slug_and_required_method_fields() -> None:
    with pytest.raises(ValueError):
        Source(id="Bad Id", title="bad", method="rss", area="AI")
    invalid = Source(id="missing-feed", title="Missing feed", method="rss", area="AI")
    assert invalid.configuration_error() == "rss requires feed_url or website_url"
    with pytest.raises(ValueError, match="HTTP"):
        Item(
            id="unsafe",
            source_id="s",
            source_title="Source",
            area="AI",
            kind="blog_post",
            title="Bad link",
            url="javascript:alert(1)",
        )


def test_source_request_parsing_and_comment_preserving_yaml_append(tmp_path: Path) -> None:
    body = """### Title
Contoso Updates

### Website URL
https://example.test/news

### Feed URL (optional)
https://example.test/feed.xml

### Area
Azure

### Collection method
rss

### Why should this source be included?
Useful product updates.
"""
    fields = parse_issue_body(body)
    entry = source_entry(fields)
    assert entry["id"] == "contoso-updates"
    assert fields["method"] == "rss"

    registry = tmp_path / "sources.yaml"
    registry.write_text(
        "# keep this curator note\n"
        "sources:\n"
        "  # keep existing source note\n"
        "  - id: existing\n"
        "    title: Existing\n",
        encoding="utf-8",
    )
    append_source(registry, entry)
    saved = registry.read_text(encoding="utf-8")
    assert "# keep this curator note" in saved
    assert "# keep existing source note" in saved
    assert "id: contoso-updates" in saved


def test_source_request_rejects_missing_method_configuration() -> None:
    fields = {
        "title": "Learn updates",
        "website_url": "https://learn.microsoft.com/",
        "method": "learn_search",
        "area": "Development",
    }
    entry = source_entry(fields)
    assert Source.model_validate(entry).configuration_error() == "learn_search requires learn_scope"


def test_source_request_rejects_multiline_title_and_unknown_area() -> None:
    fields = {
        "title": "Feed\ninjected_output=true",
        "website_url": "https://example.test/",
        "method": "rss",
        "area": "AI",
    }
    with pytest.raises(ValueError, match="single line"):
        source_entry(fields)

    fields["title"] = "Valid title"
    fields["area"] = "Unexpected"
    with pytest.raises(ValueError, match="form options"):
        source_entry(fields)


def test_dry_run_uses_fixtures_without_writing_state_or_archive(
    tmp_path: Path, monkeypatch
) -> None:
    shutil.copytree(ROOT / "config", tmp_path / "config")
    shutil.copytree(ROOT / "tests/fixtures", tmp_path / "tests/fixtures")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FOUNDRY_ENDPOINT", raising=False)
    monkeypatch.delenv("DIGEST_STATE_DIR", raising=False)
    monkeypatch.delenv("ARCHIVE_DIR", raising=False)

    result = asyncio.run(
        run_digest(
            dry_run=True,
            week_arg="2026-W45",
            force=False,
            source_filter=None,
        )
    )

    assert result == 0
    assert (tmp_path / "out/archive.html").is_file()
    assert (tmp_path / "out/run.json").is_file()
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "archive").exists()


def test_opml_export_matches_snapshot(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "sources.yaml").write_text(
        "sources:\n"
        "  - id: ai-feed\n"
        "    title: AI Feed\n"
        "    method: rss\n"
        "    feed_url: https://example.test/ai.xml\n"
        "    website_url: https://example.test/ai\n"
        "    area: AI\n"
        "  - id: azure-feed\n"
        "    title: Azure Feed\n"
        "    method: rss\n"
        "    feed_url: https://example.test/azure.xml\n"
        "    website_url: https://example.test/azure\n"
        "    area: Azure\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DIGEST_STATE_DIR", str(tmp_path / "state"))
    assert export_opml() == 0
    expected = (ROOT / "tests/fixtures/sources.opml").read_bytes()
    expected = expected.replace(b"\r\n", b"\n").rstrip(b"\n")
    assert (tmp_path / "state/sources.opml").read_bytes() == expected


def test_canonical_url_removes_tracking_and_fragment() -> None:
    assert canonical_url("HTTPS://Example.COM/post?utm_source=x&id=3#part") == (
        "https://example.com/post?id=3"
    )


def test_dedupe_prunes_old_seen_items_and_deduplicates_urls() -> None:
    item = Item(
        id="old",
        source_id="s",
        source_title="Source",
        area="AI",
        kind="blog_post",
        title="Update",
        url="https://example.com/post?utm_campaign=x",
    )
    seen = {"expired": "2020-01-01T00:00:00+00:00"}
    now = datetime(2026, 11, 4, tzinfo=UTC)
    assert len(deduplicate([item, item.model_copy()], seen, now)) == 1
    assert "expired" not in seen
    assert item.id == item_id(item.url) and len(item.id) == 64


def test_health_suspends_after_threshold_and_recovers() -> None:
    source = Source(
        id="s", title="Source", method="rss", feed_url="https://example.com/rss", area="AI"
    )
    failed = SourceResult(source_id="s", status="error", error="timeout")
    state = {}
    for _ in range(3):
        state = update_health(
            source,
            state,
            failed,
            None,
            now=datetime(2026, 11, 4, tzinfo=UTC),
        )
    assert state["health"] == "suspended"
    recovered = update_health(
        source,
        state,
        SourceResult(source_id="s", status="ok", item_count=2),
        "2026-11-04T10:00:00+00:00",
        now=datetime(2026, 11, 4, 11, tzinfo=UTC),
    )
    assert recovered["health"] == "healthy"
    assert recovered["consecutive_failures"] == 0


def test_json_store_atomic_round_trip(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "nested" / "state.json")
    store.write({"ok": True})
    assert store.read() == {"ok": True}
    assert list(store.path.parent.glob("*.tmp")) == []


def test_rss_parsing_skips_entries_without_dates() -> None:
    source = Source(
        id="feed",
        title="Feed",
        method="rss",
        feed_url="https://example.com/feed.xml",
        area="AI",
    )
    content = (ROOT / "tests/fixtures/rss.xml").read_bytes()
    items = parse_content(source, content)
    assert [item.title for item in items] == ["Recorded article"]


def test_page_diff_first_run_is_baseline_then_emits_new_text() -> None:
    source = Source(
        id="page",
        title="Page",
        method="page",
        website_url="https://example.com/news",
        area="AI",
    )
    pages = {}
    fixture = (ROOT / "tests/fixtures/licensing-after.html").read_text(encoding="utf-8")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text=fixture, request=request)
    )

    async def collect():
        async with httpx.AsyncClient(transport=transport) as client:
            collector = PageDiffCollector()
            assert await collector.collect(source, client, pages) == []
            pages.clear()
            pages["page"] = {
                "hash": "old",
                "text": (
                    "Current licensing terms with a stable introduction and old pricing context."
                ),
            }
            return await collector.collect(source, client, pages)

    items = asyncio.run(collect())
    assert len(items) == 1
    assert items[0].kind == "page_change"


def test_render_escapes_untrusted_source_text() -> None:
    source = Source(
        id="xss",
        title="Untrusted",
        method="rss",
        feed_url="https://example.com/feed",
        area="AI",
    )
    from digest.collectors.base import build_item
    from digest.models import CallToAction, EnrichedItem

    item = EnrichedItem(
        **build_item(
            source,
            title="<script>alert(1)</script>",
            url="https://example.com/?x=1&y=2",
            published_at=None,
        ).model_dump(),
        ai_summary="<img src=x onerror=alert(1)>",
    )
    html = render(
        title="Digest",
        iso_week="2026-W45",
        management_summary=["safe"],
        calls_to_action=[
            CallToAction(title="Review", why="Because", item_ids=[]) for _ in range(3)
        ],
        sections={"AI": [item]},
        failed_sources=[],
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<img src=x" not in html


def test_weekly_summary_filters_unknown_ids_and_generated_urls() -> None:
    item = Item(
        id="a" * 64,
        source_id="source",
        source_title="Source",
        area="AI",
        kind="blog_post",
        title="Update",
        url="https://example.test/update",
        summary_raw="A relevant update.",
    )
    enriched = EnrichedItem(**item.model_dump(), ai_summary="A relevant update.")

    class FakeAI:
        _client = object()

        async def json_completion(self, system, user):
            return {
                "management_summary": [f"Review https://invented.test {('b' * 64)}"],
                "calls_to_action": [
                    {
                        "title": "Investigate",
                        "why": f"See https://invented.test {('a' * 64)}",
                        "item_ids": [("b" * 64), ("a" * 64)],
                    }
                ],
            }

    summary, actions = asyncio.run(weekly_summary([enriched], FakeAI()))
    assert "https://invented.test" not in summary[0]
    assert "unknown item" in summary[0]
    assert actions[0].item_ids == ["a" * 64]
    assert "https://invented.test" not in actions[0].why

def test_write_index_lists_weeks_newest_first(tmp_path: Path) -> None:
    from digest.render.render import write_index

    for name in ("2026-W44.html", "2026-W45.html", "notes.html"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    html = write_index(tmp_path).read_text(encoding="utf-8")
    assert html.index("2026-W45") < html.index("2026-W44")
    assert "notes" not in html

def test_sanitize_text_strips_html_tags_and_decodes_entities() -> None:
    from digest.ai.sanitize import sanitize_text

    raw = '<p>Update <a href="https://x.test">now</a></p><script>alert(1)</script> 1 &lt; 2 &amp; 3'
    result = sanitize_text(raw)
    assert "<p>" not in result and "<a" not in result and "script" not in result
    assert result == "Update now 1 < 2 & 3"


def test_ai_output_is_stripped_before_reaching_items() -> None:
    from digest.ai.item_review import review_item

    class FakeAI:
        _client = object()

        async def json_completion(self, system, user):
            return {
                "relevance": "high",
                "ai_summary": "<p>Plain <a href='x'>text</a></p>",
                "release_status": "ga",
                "action": "test",
                "action_reason": "<b>Because</b>",
            }

    item = Item(
        id="a" * 64,
        source_id="s",
        source_title="S",
        area="AI",
        kind="blog_post",
        title="T",
        url="https://example.test/t",
    )
    result = asyncio.run(review_item(item, FakeAI()))
    assert result.ai_summary == "Plain text"
    assert result.action_reason == "Because"
