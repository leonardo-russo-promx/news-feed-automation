from __future__ import annotations

import re
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from digest.collectors.discovery import discover_feed
from digest.models import Source


def parse_issue_body(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    matches = list(re.finditer(r"^###\s+(.+?)\s*$", body, re.MULTILINE))
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        key = re.sub(r"\s*\(.*\)$", "", match.group(1).strip()).casefold().replace(" ", "_")
        key = {
            "collection_method": "method",
            "sender_address_filter": "from_filter",
        }.get(key, key)
        value = body[start:end].strip()
        fields[key] = "" if value.casefold() in {"_no response_", "no response"} else value
    return fields


def source_entry(fields: dict[str, str]) -> dict[str, Any]:
    title = fields.get("title", "").strip()
    website_url = fields.get("website_url", "").strip()
    method = fields.get("method", "").strip()
    area = fields.get("area", "").strip()
    if not title or not website_url or not method or not area:
        raise ValueError("Title, website_url, method, and area are required.")
    if "\n" in title or "\r" in title or len(title) > 160:
        raise ValueError("Title must be a single line of at most 160 characters.")
    if area not in {
        "AI",
        "Azure",
        "Development",
        "Developer",
        "Dynamics",
        "Power Platform",
        "Security",
        "License",
        "Roadmap",
        "Microsoft Platform",
    }:
        raise ValueError("Area is not one of the source-request form options.")
    source_id = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", title.casefold())).strip("-")
    if not source_id:
        raise ValueError("Title must produce a valid source slug.")
    entry: dict[str, Any] = {
        "id": source_id,
        "title": title,
        "method": method,
        "website_url": website_url,
        "area": area,
        "enabled": True,
        "added_on": date.today().isoformat(),
    }
    if fields.get("feed_url", "").strip():
        entry["feed_url"] = fields["feed_url"].strip()
    if method == "learn_search" and fields.get("learn_scope", "").strip():
        entry["learn_scope"] = fields["learn_scope"].strip()
        entry["learn_search_terms"] = fields.get("learn_search_terms", title).strip()
    return entry


def append_source(path: Path, entry: dict[str, Any]) -> None:
    roundtrip = YAML()
    roundtrip.preserve_quotes = True
    with path.open(encoding="utf-8") as stream:
        document = roundtrip.load(stream)
    if any(source.get("id") == entry["id"] for source in document["sources"]):
        raise ValueError(f"source id already exists: {entry['id']}")
    document["sources"].append(entry)
    with path.open("w", encoding="utf-8") as stream:
        roundtrip.dump(document, stream)


async def validate_entry(entry: dict[str, Any]) -> tuple[Source, str]:
    source = Source.model_validate(entry)
    error = source.configuration_error()
    if error:
        raise ValueError(error)
    if source.method == "rss" and not source.feed_url and source.website_url:
        import httpx

        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            found = await discover_feed(str(source.website_url), client)
        if found:
            entry["feed_url"] = found
            source = Source.model_validate(entry)
        else:
            raise ValueError("No RSS/Atom feed was found; supply a feed_url.")
    if source.method in {"rss", "learn_search", "page"}:
        import httpx

        from digest.collectors.learn_search import LearnSearchCollector
        from digest.collectors.page_diff import PageDiffCollector
        from digest.collectors.rss import RssCollector

        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            if source.method == "rss":
                collector = RssCollector()
                items = await collector.collect(source, client)
                feed_url = collector.resolved_feed_url or str(source.feed_url or "")
            elif source.method == "learn_search":
                items = await LearnSearchCollector().collect(source, client)
                feed_url = "https://learn.microsoft.com/api/search/rss"
            else:
                items = await PageDiffCollector().collect(source, client, {})
                feed_url = str(source.website_url)
        if source.method != "page" and not items:
            raise ValueError("source responded but returned no items")
        newest = max(
            (
                item.updated_at or item.published_at
                for item in items
                if item.updated_at or item.published_at
            ),
            default=None,
        )
        return (
            source,
            f"live check passed; items={len(items)}; "
            f"newest={newest.isoformat() if newest else 'unknown'}; feed={feed_url}",
        )
    return source, "configuration accepted; mail source probe deferred to the scheduled Graph run."


def main() -> None:
    import asyncio
    import json
    import os

    event_path = os.getenv("GITHUB_EVENT_PATH")
    if not event_path:
        raise SystemExit("GITHUB_EVENT_PATH is required")
    event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    body = event.get("issue", {}).get("body", "")
    fields = parse_issue_body(body)
    try:
        entry = source_entry(fields)
        source, validation_message = asyncio.run(validate_entry(entry))
        append_source(Path("config/sources.yaml"), entry)
        Path("out").mkdir(exist_ok=True)
        result = {
            "ok": True,
            "id": source.id,
            "title": source.title,
            "reason": fields.get("reason", ""),
            "validation": validation_message,
        }
        Path("out/source-request.json").write_text(json.dumps(result), encoding="utf-8")
        output_path = os.getenv("GITHUB_OUTPUT")
        if output_path:
            delimiter = f"REASON_{uuid.uuid4().hex}"
            with Path(output_path).open("a", encoding="utf-8") as stream:
                stream.write(f"valid=true\nsource_id={source.id}\nsource_title={source.title}\n")
                stream.write(
                    f"source_reason<<{delimiter}\n{fields.get('reason', '')}\n{delimiter}\n"
                )
                validation_delimiter = f"VALIDATION_{uuid.uuid4().hex}"
                stream.write(
                    f"validation<<{validation_delimiter}\n{validation_message}\n"
                    f"{validation_delimiter}\n"
                )
    except Exception as exc:
        Path("out").mkdir(exist_ok=True)
        message = f"{type(exc).__name__}: {exc}"
        Path("out/source-request.json").write_text(
            json.dumps({"ok": False, "error": message}), encoding="utf-8"
        )
        output_path = os.getenv("GITHUB_OUTPUT")
        if output_path:
            with Path(output_path).open("a", encoding="utf-8") as stream:
                delimiter = f"ERROR_{uuid.uuid4().hex}"
                stream.write(f"valid=false\nrequest_error<<{delimiter}\n{message}\n{delimiter}\n")


if __name__ == "__main__":
    main()
