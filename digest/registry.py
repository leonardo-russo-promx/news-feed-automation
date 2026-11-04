from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from digest.models import Source


def load_sources(path: Path = Path("config/sources.yaml")) -> list[Source]:
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("sources"), list):
        raise ValueError(f"{path} must contain a 'sources' list")
    sources = [Source.model_validate(entry) for entry in raw["sources"]]
    ids = [source.id for source in sources]
    if len(ids) != len(set(ids)):
        raise ValueError("source ids must be unique")
    for source in sources:
        error = source.configuration_error()
        if error:
            raise ValueError(f"{source.id}: {error}")
    return sources


def validate_schema(path: Path, schema_path: Path) -> list[str]:
    import jsonschema

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(
        schema,
        format_checker=jsonschema.FormatChecker(),
    )
    errors = [error.message for error in validator.iter_errors(document)]
    return sorted(errors)
