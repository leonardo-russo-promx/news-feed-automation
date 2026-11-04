from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx
import yaml

from digest.collectors.learn_search import LearnSearchCollector
from digest.collectors.page_diff import PageDiffCollector
from digest.collectors.rss import RssCollector
from digest.models import Source
from digest.registry import load_sources, validate_schema


def _base_registry() -> dict[str, dict[str, Any]]:
    base = os.environ["BASE_REF"]
    subprocess.run(["git", "fetch", "origin", base], check=True, capture_output=True)
    content = subprocess.run(
        ["git", "show", f"origin/{base}:config/sources.yaml"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    data = yaml.safe_load(content)
    return {entry["id"]: entry for entry in data["sources"]}


async def _check(source: Source) -> tuple[str, int, str, str | None]:
    if not source.enabled:
        return "skipped", 0, "disabled", None
    if source.has_todo_configuration:
        return "skipped", 0, "source has TODO configuration", None
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            if source.method == "rss":
                items = await RssCollector().collect(source, client)
            elif source.method == "learn_search":
                items = await LearnSearchCollector().collect(source, client)
            elif source.method == "page":
                snapshots = {}
                await PageDiffCollector().collect(source, client, snapshots)
                items = []
            else:
                return "skipped", 0, f"unsupported method: {source.method}", None
        newest = max(
            (
                item.updated_at or item.published_at
                for item in items
                if item.updated_at or item.published_at
            ),
            default=None,
        )
        return "ok" if items else "empty", len(items), "", newest.isoformat() if newest else None
    except Exception as exc:
        return "error", 0, f"{type(exc).__name__}: {exc}", None


async def _run() -> int:
    errors = validate_schema(Path("config/sources.yaml"), Path("config/sources.schema.json"))
    if errors:
        report = "Schema validation failed:\n" + "\n".join(f"- {error}" for error in errors)
        _output(report)
        return 1
    try:
        load_sources(Path("config/sources.yaml"))
    except ValueError as exc:
        _output(f"Pydantic validation failed:\n- {exc}")
        return 1
    current_data = yaml.safe_load(Path("config/sources.yaml").read_text(encoding="utf-8"))
    current = {entry["id"]: entry for entry in current_data["sources"]}
    old = _base_registry()
    changed = [
        Source.model_validate(entry)
        for source_id, entry in current.items()
        if source_id not in old
        or Source.model_validate(entry).model_dump(mode="json")
        != Source.model_validate(old[source_id]).model_dump(mode="json")
    ]
    rows = ["| Source | Status | Items | Newest item | Result |", "|---|---|---:|---|---|"]
    blocking_failure = False
    for source in changed:
        status, count, detail, newest = await _check(source)
        added = source.id not in old
        if added and source.enabled and status == "error":
            blocking_failure = True
        suffix = " (warning only: existing source)" if status == "error" and not added else ""
        safe_detail = (detail or "live check completed").replace("|", "/").replace("\n", " ")
        rows.append(
            f"| {source.id} | {status} | {count} | {newest or '-'} | {safe_detail}{suffix} |"
        )
    if not changed:
        rows.append("| — | no registry entries changed | 0 | — | — |")
    report = "\n".join(rows)
    _output(report)
    return 1 if blocking_failure else 0


def _output(report: str) -> None:
    print(report)
    output_path = os.getenv("GITHUB_OUTPUT")
    if output_path:
        delimiter = "SOURCE_REPORT_END"
        with Path(output_path).open("a", encoding="utf-8") as stream:
            stream.write(f"report<<{delimiter}\n{report}\n{delimiter}\n")


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
