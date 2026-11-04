from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class Settings(BaseModel):
    timezone: str = "UTC"
    lookback_days: int = Field(default=7, ge=1)
    learn_lookback_days: int = Field(default=14, ge=1)
    max_items_llm: int = Field(default=80, ge=1)
    report_language: str = "en"
    suspend_after_failures: int = Field(default=3, ge=1)
    stale_after_days: int = Field(default=42, ge=1)
    roadmap_products: list[str] = Field(
        default_factory=lambda: [
            "Microsoft Copilot Studio",
            "Microsoft 365 Copilot",
            "Power Apps",
            "Power Automate",
            "Power Pages",
            "Microsoft Dataverse",
            "Dynamics 365 Project Operations",
        ]
    )


def load_settings(path: Path = Path("config/settings.yaml")) -> Settings:
    data: dict[str, Any] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if loaded is not None:
            if not isinstance(loaded, dict):
                raise ValueError(f"{path} must contain a YAML mapping")
            data.update(loaded)
    for key in Settings.model_fields:
        env_key = key.upper()
        if env_key in os.environ:
            data[key] = os.environ[env_key]
    return Settings.model_validate(data)
