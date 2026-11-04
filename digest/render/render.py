from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from digest.models import CallToAction, EnrichedItem

TEMPLATE_DIR = Path(__file__).parent / "templates"
WEEK_FILE = re.compile(r"^\d{4}-W\d{2}\.html$")


def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(("html", "xml", "j2")),
    )


def render(
    *,
    title: str,
    iso_week: str,
    management_summary: list[str],
    calls_to_action: list[CallToAction],
    sections: dict[str, list[EnrichedItem]],
    failed_sources: list[str],
    health_counts: dict[str, int] | None = None,
) -> str:
    template = _environment().get_template("archive.html.j2")
    items = [item for values in sections.values() for item in values]
    items.sort(
        key=lambda item: item.updated_at or item.published_at or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    items.sort(key=lambda item: {"high": 0, "medium": 1, "low": 2, "ignore": 3}[item.relevance])
    order = {item.id: index for index, item in enumerate(items)}
    sections = {
        area: sorted(values, key=lambda item: order[item.id]) for area, values in sections.items()
    }
    return template.render(
        title=title,
        iso_week=iso_week,
        management_summary=management_summary,
        calls_to_action=calls_to_action,
        sections=sections,
        failed_sources=failed_sources,
        health_counts=health_counts or {},
    )


def write_index(archive_dir: Path, title: str = "Technology Digest") -> Path:
    weeks = sorted(
        (path.stem for path in archive_dir.glob("*.html") if WEEK_FILE.match(path.name)),
        reverse=True,
    )
    html = _environment().get_template("index.html.j2").render(title=title, weeks=weeks)
    target = archive_dir / "index.html"
    target.write_text(html, encoding="utf-8")
    return target
