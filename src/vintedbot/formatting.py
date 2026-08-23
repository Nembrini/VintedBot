"""Telegram caption building for items. Pure module: no I/O, fully testable.

Parse mode is HTML, NOT MarkdownV2: HTML escaping needs only three
entities (& < >) versus MarkdownV2's ~18 reserved characters, and Vinted
titles are full of characters that would break markdown. Every field
coming from Vinted (title, brand, size, condition) is untrusted input and
goes through :func:`html.escape`.

Telegram photo captions are capped at 1024 characters: the TITLE is
trimmed (with an ellipsis) until the whole message fits — trimming
happens on the raw title BEFORE escaping/composition, so an HTML entity
or tag can never be cut in half.

Layout (future steps will extend it with the estimated market price):

    <b>{titolo}</b>
    💰 {prezzo} {valuta}
    🏷 {brand} · taglia {taglia} · {condizione}
    🕒 Caricato: {gg/mm/aaaa HH:MM}
    🔗 <a href="{url}">Apri su Vinted</a>
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from vintedbot.models import Item
    from vintedbot.pricing import PriceEstimate

#: Timezone used to DISPLAY the upload time (stored as UTC).
_DISPLAY_TZ = ZoneInfo("Europe/Rome")

#: Telegram's hard limit for photo captions.
CAPTION_MAX_CHARS = 1024

_ELLIPSIS = "…"


def format_item_message(item: Item, estimate: PriceEstimate | None = None) -> str:
    """Build the HTML caption for one item, always within Telegram's limit.

    Missing fields (brand/size/condition) drop out of their line without
    leaving "None" or orphan separators; if all three are missing the
    whole 🏷 line disappears. The market-estimate line appears when an
    estimate is provided: full (💎 score) with enough history, "storico
    insufficiente" (📊) with a small sample, nothing at all with no data.
    """
    message = _compose(item, item.title, estimate)
    if len(message) <= CAPTION_MAX_CHARS:
        return message

    # Troppo lungo: accorcia il titolo GREZZO finché il totale rientra.
    # L'escaping può allungare il testo, quindi si itera sul risultato
    # composto invece di fare aritmetica sull'input.
    max_len = len(item.title)
    while max_len > 0:
        candidate = item.title[:max_len].rstrip() + _ELLIPSIS
        message = _compose(item, candidate, estimate)
        if len(message) <= CAPTION_MAX_CHARS:
            return message
        max_len -= max(len(message) - CAPTION_MAX_CHARS, 1)
    return _compose(item, _ELLIPSIS, estimate)  # titolo irrilevante: resta il resto


def _compose(item: Item, raw_title: str, estimate: PriceEstimate | None = None) -> str:
    """Assemble the caption with the given (possibly trimmed) raw title."""
    lines = [
        f"<b>{_escape(raw_title)}</b>",
        f"💰 {item.price.amount} {item.price.currency}",
    ]

    details = [
        detail
        for detail in (
            item.brand,
            f"taglia {item.size}" if item.size else None,
            item.condition,
        )
        if detail
    ]
    if details:
        lines.append("🏷 " + " · ".join(_escape(detail) for detail in details))

    if item.published_at is not None:
        local_time = item.published_at.astimezone(_DISPLAY_TZ)
        lines.append(f"🕒 Caricato: {local_time:%d/%m/%Y %H:%M}")

    estimate_line = _estimate_line(item, estimate)
    if estimate_line is not None:
        lines.append(estimate_line)

    lines.append(f'🔗 <a href="{html.escape(item.url, quote=True)}">Apri su Vinted</a>')
    return "\n".join(lines)


def _estimate_line(item: Item, estimate: PriceEstimate | None) -> str | None:
    """Market-estimate line: 💎 full score, 📊 small sample, None with no data."""
    if estimate is None or estimate.sample_size == 0:
        return None
    if (
        estimate.score is None
        or estimate.median is None
        or estimate.discount_pct is None
    ):
        return f"📊 Storico insufficiente per una stima (n={estimate.sample_size})"

    pct = round(estimate.discount_pct * 100)
    sign = "−" if pct >= 0 else "+"
    return (
        f"💎 Affare: {estimate.score}/100 · {sign}{abs(pct)}%"
        f" vs ~{estimate.median:.2f} {item.price.currency}"
        f" ({estimate.sample_size} annunci)"
    )


def _escape(value: str) -> str:
    """HTML-escape untrusted text for Telegram (entities: & < > only)."""
    return html.escape(value, quote=False)
