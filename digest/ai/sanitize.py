from __future__ import annotations

import re
from html import unescape

URL_PATTERN = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
BLOCK_PATTERN = re.compile(r"(?is)<(script|style)(?=[\s/>])[^<>]*>.*?</\1\s*>")
COMMENT_PATTERN = re.compile(r"(?s)<!--.*?-->")
ATTRIBUTE = r"""\s+[A-Za-z_:][-\w:.]*(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'<>=`]+))?"""
TAG_PATTERN = re.compile(
    rf"</?[A-Za-z][A-Za-z0-9-]*(?:{ATTRIBUTE})*\s*/?>|<![A-Za-z][^<>]*>"
)


def strip_html(value: str) -> str:
    # Feeds often double-escape markup (``&lt;p&gt;``); unescape and strip until stable.
    text = value
    for _ in range(3):
        stripped = TAG_PATTERN.sub("", BLOCK_PATTERN.sub("", COMMENT_PATTERN.sub("", text)))
        unescaped = unescape(stripped)
        if unescaped == text:
            break
        text = unescaped
    return text


def sanitize_text(value: str, valid_ids: set[str] | None = None) -> str:
    text = URL_PATTERN.sub("[link omitted]", strip_html(value))
    if valid_ids is not None:
        text = re.sub(
            r"\b[a-f0-9]{64}\b",
            lambda match: match.group(0) if match.group(0) in valid_ids else "[unknown item]",
            text,
            flags=re.IGNORECASE,
        )
    return text.strip()
