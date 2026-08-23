"""Saved searches: load and validate ``searches.toml``.

The file is the production input of ``run-all``; its fields mirror the
options of the ``search`` command one-to-one (same names, same meaning,
same numeric Vinted IDs) so there is exactly one vocabulary to learn.

Validation is strict and up front — unknown fields included, so a typo
like ``min_scor`` is an error and not a silently ignored line — and every
message names the offending search and field. Callers surface
:class:`SearchConfigError` as a plain message: the user never sees a
traceback.
"""

from __future__ import annotations

import tomllib
from decimal import Decimal  # noqa: TC003 — needed at runtime by pydantic
from typing import TYPE_CHECKING, Annotated, Any

import structlog
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from vintedbot.models import SearchFilters

if TYPE_CHECKING:
    from pathlib import Path

logger = structlog.get_logger(__name__)


class SearchConfigError(Exception):
    """The saved-searches file is missing, malformed or invalid."""


def _as_id_tuple(value: Any) -> Any:
    """Accept a single ID or a list of IDs; reject names with a helpful error."""
    values = value if isinstance(value, list | tuple) else [value]
    for entry in values:
        if isinstance(entry, str):
            raise ValueError(
                f"{entry!r} non è un ID numerico: usa gli ID Vinted "
                "(vedi docs/api_notes.md), i nomi non sono supportati"
            )
    return tuple(values)


IdList = Annotated[tuple[int, ...], BeforeValidator(_as_id_tuple)]


class SavedSearch(BaseModel):
    """One ``[[search]]`` entry of the saved-searches file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, description="Unique identifier, used in logs.")
    enabled: bool = True

    keyword: str | None = None
    catalog: IdList = ()
    brand: IdList = ()
    size: IdList = ()
    condition: IdList = ()
    min_price: Decimal | None = Field(default=None, ge=0)
    max_price: Decimal | None = Field(default=None, ge=0)

    max_pages: int | None = Field(default=None, ge=1)
    max_items: int | None = Field(default=None, ge=1)
    min_score: int | None = Field(default=None, ge=0, le=100)
    strict_score: bool = False

    @model_validator(mode="after")
    def _check_consistency(self) -> SavedSearch:
        if self.strict_score and self.min_score is None:
            raise ValueError("strict_score richiede min_score")
        if (
            self.min_price is not None
            and self.max_price is not None
            and self.min_price > self.max_price
        ):
            raise ValueError("min_price deve essere <= max_price")
        return self

    def to_filters(self) -> SearchFilters:
        """Build the search filters this saved search stands for."""
        return SearchFilters(
            keyword=self.keyword,
            category_ids=self.catalog,
            brand_ids=self.brand,
            size_ids=self.size,
            condition_ids=self.condition,
            price_min=self.min_price,
            price_max=self.max_price,
        )


def load_searches(path: Path) -> list[SavedSearch]:
    """Parse and validate the saved-searches file.

    Returns every search in file order (disabled ones included: the caller
    decides what to run, and ``--only`` must be able to name them).

    Raises:
        SearchConfigError: file missing, malformed TOML, no ``[[search]]``
            entries, duplicate names, invalid or unknown fields, or no
            enabled search at all.
    """
    if not path.exists():
        raise SearchConfigError(
            f"file delle ricerche non trovato: {path}\n"
            "Crealo copiando l'esempio versionato: searches.example.toml"
        )

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise SearchConfigError(f"{path} non è un TOML valido: {exc}") from exc
    except OSError as exc:
        raise SearchConfigError(f"impossibile leggere {path}: {exc}") from exc

    entries = raw.get("search")
    if not isinstance(entries, list) or not entries:
        raise SearchConfigError(
            f"{path} non contiene nessuna ricerca: serve almeno una tabella [[search]]"
        )

    searches: list[SavedSearch] = []
    seen_names: set[str] = set()
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise SearchConfigError(f"ricerca #{position}: deve essere una tabella [[search]]")
        try:
            search = SavedSearch.model_validate(entry)
        except ValidationError as exc:
            raw_name = entry.get("name")
            # Con un nome utilizzabile lo citiamo; altrimenti la posizione.
            label = (
                repr(raw_name) if isinstance(raw_name, str) and raw_name else f"#{position}"
            )
            raise SearchConfigError(_format_errors(label, exc)) from exc

        if search.name in seen_names:
            raise SearchConfigError(
                f"ricerca {search.name!r}: nome duplicato, ogni ricerca deve avere un nome unico"
            )
        seen_names.add(search.name)
        searches.append(search)

    if not any(search.enabled for search in searches):
        raise SearchConfigError(
            f"{path}: nessuna ricerca abilitata (imposta enabled = true su almeno una)"
        )

    logger.debug(
        "searches_loaded",
        path=str(path),
        total=len(searches),
        enabled=sum(search.enabled for search in searches),
    )
    return searches


def _format_errors(label: str, exc: ValidationError) -> str:
    """Turn a pydantic error into lines like: ricerca 'x': campo — messaggio.

    ``label`` arrives ready to print: quoted name, or ``#N`` position when
    the entry has no usable name.
    """
    lines = []
    for error in exc.errors():
        field = ".".join(str(part) for part in error["loc"]) or "(ricerca)"
        message = error["msg"].removeprefix("Value error, ")
        if error["type"] == "extra_forbidden":
            message = "campo sconosciuto (refuso?)"
        lines.append(f"ricerca {label}: {field} — {message}")
    return "\n".join(lines)
