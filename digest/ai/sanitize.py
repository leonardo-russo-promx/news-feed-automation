from __future__ import annotations

import re

URL_PATTERN = re.compile(r"(?i)\b(?:https?://|www\.)\S+")


def sanitize_text(value: str, valid_ids: set[str] | None = None) -> str:
    text = URL_PATTERN.sub("[link omitted]", value)
    if valid_ids is not None:
        text = re.sub(
            r"\b[a-f0-9]{64}\b",
            lambda match: match.group(0) if match.group(0) in valid_ids else "[unknown item]",
            text,
            flags=re.IGNORECASE,
        )
    return text.strip()
