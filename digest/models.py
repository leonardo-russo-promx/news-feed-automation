from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from digest.urls import is_safe_http_url

SourceMethod = Literal["rss", "learn_search", "page"]


class SourceFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    include_categories: list[str] = Field(default_factory=list)
    include_categories_from_settings: str | None = None
    max_items: int | None = Field(default=None, ge=1)


class PageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selector: str = "main"


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    method: SourceMethod
    feed_url: HttpUrl | None = None
    alt_feed_urls: list[HttpUrl] = Field(default_factory=list)
    website_url: HttpUrl | None = None
    area: str
    enabled: bool = True
    filters: SourceFilters = Field(default_factory=SourceFilters)
    learn_scope: str | None = None
    learn_search_terms: str | None = None
    page: PageConfig = Field(default_factory=PageConfig)
    owner: str | None = None
    added_on: date | None = None
    tc_board_id: str | None = None
    notes: str | None = None

    @field_validator("id")
    @classmethod
    def valid_slug(cls, value: str) -> str:
        import re

        if not re.fullmatch(r"[a-z0-9-]+", value):
            raise ValueError("id must be a lowercase slug using letters, digits, and hyphens")
        return value

    @field_validator("feed_url", "website_url", mode="before")
    @classmethod
    def empty_url_is_none(cls, value: object) -> object:
        return None if value == "" else value

    def configuration_error(self) -> str | None:
        if self.method == "rss" and not self.feed_url and not self.website_url:
            return "rss requires feed_url or website_url"
        if self.method == "learn_search" and not self.learn_scope:
            return "learn_search requires learn_scope"
        if self.method == "page" and not self.website_url:
            return "page requires website_url"
        return None

    @property
    def has_todo_configuration(self) -> bool:
        values = (self.learn_scope, self.learn_search_terms)
        return any(value and value.startswith("TODO") for value in values)


class Item(BaseModel):
    id: str
    source_id: str
    source_title: str
    area: str
    kind: str
    title: str
    url: str
    published_at: datetime | None = None
    updated_at: datetime | None = None
    summary_raw: str = ""
    content_text: str = Field(default="", max_length=6000)
    categories: list[str] = Field(default_factory=list)
    extra: dict[str, object] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def require_http_url(cls, value: str) -> str:
        if not is_safe_http_url(value):
            raise ValueError("item URL must be an absolute HTTP(S) URL")
        return value


class EnrichedItem(Item):
    relevance: Literal["high", "medium", "low", "ignore"] = "medium"
    ai_summary: str = ""
    release_status: Literal["preview", "ga", "retirement", "announcement", "unknown"] = "unknown"
    action: Literal["look_into", "test", "inform_customers", "monitor", "ignore"] = "monitor"
    action_reason: str = ""
    review_failed: bool = False


class SourceResult(BaseModel):
    source_id: str
    status: Literal["ok", "empty", "skipped", "error"]
    item_count: int = 0
    error: str | None = None
    warning: str | None = None
    duration_ms: int = 0
    resolved_feed_url: str | None = None
    etag: str | None = None
    last_modified: str | None = None


class CallToAction(BaseModel):
    title: str
    why: str
    item_ids: list[str] = Field(default_factory=list)


class ManagementSummaryItem(BaseModel):
    text: str
    item_ids: list[str] = Field(default_factory=list)


class Digest(BaseModel):
    iso_week: str
    period_start: date
    period_end: date
    management_summary: list[ManagementSummaryItem] = Field(default_factory=list, max_length=7)
    calls_to_action: list[CallToAction] = Field(default_factory=list, min_length=3, max_length=5)
    sections: dict[str, list[EnrichedItem]] = Field(default_factory=dict)
    stats: dict[str, int] = Field(default_factory=dict)
    failed_sources: list[SourceResult] = Field(default_factory=list)
