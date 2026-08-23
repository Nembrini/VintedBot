"""Typed domain models for Vinted search filters and catalog items.

The raw Vinted API parameter/field names (see ``docs/api_notes.md``) are
confined to this module: :meth:`SearchFilters.to_query_params` emits the
exact query parameters the catalog endpoint expects, and :class:`Item`
maps the raw catalog JSON onto speaking, typed attributes. The rest of
the codebase never touches raw API names.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — needed at runtime by pydantic
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import (
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = structlog.get_logger(__name__)


class SortOrder(StrEnum):
    """Sort orders accepted by the catalog endpoint (values are the API's own)."""

    NEWEST_FIRST = "newest_first"
    RELEVANCE = "relevance"
    PRICE_LOW_TO_HIGH = "price_low_to_high"
    PRICE_HIGH_TO_LOW = "price_high_to_low"


class SearchFilters(BaseModel):
    """A saved search: what to look for on Vinted.

    Every field is optional — the API happily accepts an unfiltered search —
    but in practice you will want at least a category or a keyword.
    Numeric IDs (category, brand, size, condition) are the stable Vinted IDs;
    how to discover them is documented in ``docs/api_notes.md``.
    """

    model_config = ConfigDict(frozen=True)

    keyword: str | None = Field(
        default=None, description="Full-text search query (API: search_text)."
    )
    category_ids: tuple[int, ...] = Field(
        default=(), description="Vinted catalog/category IDs (API: catalog_ids)."
    )
    brand_ids: tuple[int, ...] = Field(default=(), description="Brand IDs.")
    size_ids: tuple[int, ...] = Field(default=(), description="Size IDs.")
    condition_ids: tuple[int, ...] = Field(
        default=(), description="Item condition IDs (API: status_ids)."
    )
    price_min: Decimal | None = Field(default=None, ge=0, description="Minimum price.")
    price_max: Decimal | None = Field(default=None, ge=0, description="Maximum price.")
    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")
    order: SortOrder = Field(
        default=SortOrder.NEWEST_FIRST, description="Result ordering; default newest first."
    )

    @model_validator(mode="after")
    def _price_range_consistent(self) -> SearchFilters:
        if (
            self.price_min is not None
            and self.price_max is not None
            and self.price_min > self.price_max
        ):
            raise ValueError("price_min must be <= price_max")
        return self

    def to_query_params(self) -> dict[str, str]:
        """Serialize to the exact query params of ``GET /api/v2/catalog/items``.

        Pagination (``page``/``per_page``) is intentionally NOT included:
        it belongs to the caller, not to the saved search.
        """
        params: dict[str, str] = {
            "order": self.order.value,
            "currency": self.currency,
        }
        if self.keyword:
            params["search_text"] = self.keyword
        if self.category_ids:
            params["catalog_ids"] = _csv(self.category_ids)
        if self.brand_ids:
            params["brand_ids"] = _csv(self.brand_ids)
        if self.size_ids:
            params["size_ids"] = _csv(self.size_ids)
        if self.condition_ids:
            params["status_ids"] = _csv(self.condition_ids)
        if self.price_min is not None:
            params["price_from"] = str(self.price_min)
        if self.price_max is not None:
            params["price_to"] = str(self.price_max)
        return params


def _csv(ids: tuple[int, ...]) -> str:
    return ",".join(str(i) for i in ids)


class Money(BaseModel):
    """An amount in a given currency. Amount is Decimal — never float."""

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    amount: Decimal
    currency: str = Field(validation_alias="currency_code")


class Seller(BaseModel):
    """The listing's seller (raw API object: ``user``)."""

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    id: int
    username: str = Field(validation_alias="login")


class Item(BaseModel):
    """One catalog listing, mapped from the raw item JSON of the catalog endpoint.

    Parsing is tolerant by design (schema is not contractual): extra fields
    are ignored and optional fields survive absence. Use :func:`parse_items`
    to parse a whole page without letting one malformed entry fail the rest.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    id: int
    title: str
    price: Money
    brand: str | None = Field(default=None, validation_alias="brand_title")
    size: str | None = Field(default=None, validation_alias="size_title")
    condition: str | None = Field(
        default=None,
        validation_alias="status",
        description="Localized condition label (it); filter via condition_ids, not this string.",
    )
    url: str = Field(description="Absolute URL of the listing page.")
    photo_url: str | None = Field(
        default=None,
        validation_alias=AliasPath("photo", "url"),
        description="Main photo (~800px).",
    )
    photo_urls: tuple[str, ...] = Field(
        default=(),
        validation_alias="photos",
        description="All photo URLs, main first (for Telegram albums).",
    )
    seller: Seller | None = Field(
        default=None,
        validation_alias="user",
        description=(
            "Present when parsed from the API; None for items rebuilt from "
            "the local DB (not persisted — not needed for notifications)."
        ),
    )
    published_at: datetime | None = Field(
        default=None,
        validation_alias=AliasPath("photo", "high_resolution", "timestamp"),
        description=(
            "Aware datetime (UTC). Proxy: upload timestamp of the main photo — "
            "the API exposes no explicit publication date (see api_notes.md)."
        ),
    )

    @field_validator("brand", "size", "condition", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        """The API uses empty strings for missing brand/size; normalize to None."""
        return v if v != "" else None

    @field_validator("photo_urls", mode="before")
    @classmethod
    def _extract_photo_urls(cls, v: object) -> object:
        """Accept both the API's photos[] (objects with url) and plain URL lists."""
        if isinstance(v, list | tuple):
            urls: list[str] = []
            for photo in v:
                if isinstance(photo, str):
                    urls.append(photo)
                elif isinstance(photo, dict) and isinstance(photo.get("url"), str):
                    urls.append(photo["url"])
            return tuple(urls)
        return v


def parse_items(raw_items: Iterable[Any]) -> list[Item]:
    """Parse raw catalog items tolerantly.

    A malformed entry is logged (warning, with its id when available) and
    discarded; it never fails the whole page. Callers can compute the number
    of discarded entries as ``len(raw) - len(result)``.
    """
    items: list[Item] = []
    for raw in raw_items:
        try:
            items.append(Item.model_validate(raw))
        except ValidationError as exc:
            raw_id = raw.get("id") if isinstance(raw, dict) else None
            logger.warning(
                "item_discarded",
                item_id=raw_id,
                error_count=exc.error_count(),
                first_error=str(exc.errors()[0].get("msg", "")) if exc.errors() else "",
            )
    return items
