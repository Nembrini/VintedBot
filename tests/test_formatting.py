"""Tests for vintedbot.formatting — pure, no mocks, no network."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from vintedbot.formatting import CAPTION_MAX_CHARS, format_item_message
from vintedbot.models import Item, parse_items
from vintedbot.pricing import PriceEstimate


def make_item(
    title: str = "Giubbotto",
    brand: str | None = "Nike",
    size: str | None = "M",
    condition: str | None = "Ottime",
) -> Item:
    return Item.model_validate(
        {
            "id": 1,
            "title": title,
            "price": {"amount": "12.50", "currency_code": "EUR"},
            "brand_title": brand or "",
            "size_title": size or "",
            "status": condition or "",
            "url": "https://www.vinted.it/items/1-giubbotto",
            "user": {"id": 2, "login": "seller"},
        }
    )


def assert_balanced_html(message: str) -> None:
    assert message.count("<b>") == message.count("</b>")
    assert message.count("<a ") == message.count("</a>")
    # nessun tag lasciato aperto in coda da un troncamento
    assert not message.rstrip().endswith("<")


# ------------------------------------------------------ (a) item completo


def test_full_item_message_layout() -> None:
    message = format_item_message(make_item())

    assert "<b>Giubbotto</b>" in message
    assert "💰 12.50 EUR" in message
    assert "🏷 Nike · taglia M · Ottime" in message
    assert '🔗 <a href="https://www.vinted.it/items/1-giubbotto">Apri su Vinted</a>' in message
    assert_balanced_html(message)


# ------------------------------------------------------------ (b) escaping


def test_untrusted_fields_are_escaped() -> None:
    item = make_item(
        title='<b>occasione</b> & "roba" _*[markdown]*_',
        brand="H&M",
        condition="Nuovo <con> cartellino",
    )
    message = format_item_message(item)

    # Il testo ostile compare SOLO in forma escapata...
    assert "&lt;b&gt;occasione&lt;/b&gt; &amp;" in message
    assert "H&amp;M" in message
    assert "Nuovo &lt;con&gt; cartellino" in message
    # ...e i caratteri markdown restano intatti (siamo in HTML).
    assert '_*[markdown]*_' in message
    # L'unico <b> presente è il NOSTRO tag del titolo.
    assert message.count("<b>") == 1
    assert_balanced_html(message)


# ------------------------------------------- (c) campi mancanti


def test_missing_brand_and_size_leave_no_none_or_orphans() -> None:
    message = format_item_message(make_item(brand=None, size=None))

    assert "None" not in message
    assert "🏷 Ottime" in message  # resta solo la condizione, senza "·" orfani
    assert " · " not in message.split("🏷 ")[1].splitlines()[0].replace("Ottime", "")


def test_all_details_missing_drops_the_line() -> None:
    message = format_item_message(make_item(brand=None, size=None, condition=None))

    assert "🏷" not in message
    assert "None" not in message
    assert_balanced_html(message)


# ------------------------------------------------- (d) titolo lunghissimo


def test_huge_title_is_trimmed_within_limit() -> None:
    item = make_item(title="Grandissima occasione <&> " * 300)  # >8000 char, con escape
    message = format_item_message(item)

    assert len(message) <= CAPTION_MAX_CHARS
    assert "…" in message
    assert_balanced_html(message)
    # il resto del layout è sopravvissuto al troncamento
    assert "💰 12.50 EUR" in message
    assert "Apri su Vinted</a>" in message


# ------------------------------------------------- data di caricamento


def test_published_at_shown_in_italian_local_time() -> None:
    raw = {
        "id": 1,
        "title": "x",
        "price": {"amount": "5.0", "currency_code": "EUR"},
        "url": "https://www.vinted.it/items/1-x",
        "user": {"id": 2, "login": "s"},
        "published_at": "2026-01-15T12:00:00+00:00",  # inverno: Roma = UTC+1
    }
    message = format_item_message(Item.model_validate(raw))
    assert "🕒 Caricato: 15/01/2026 13:00" in message


def test_no_published_at_no_clock_line() -> None:
    message = format_item_message(make_item())
    assert "🕒" not in message


# ----------------------------------------------- (m) riga stima di mercato


def make_estimate(**overrides: Any) -> PriceEstimate:
    defaults: dict[str, Any] = {
        "median": Decimal("34.00"),
        "sample_size": 62,
        "observed_from": "2026-06-01T00:00:00+00:00",
        "observed_to": "2026-08-20T00:00:00+00:00",
        "score": 78,
        "discount_pct": 0.45,
    }
    return PriceEstimate(**{**defaults, **overrides})


def test_caption_with_full_estimate() -> None:
    message = format_item_message(make_item(), make_estimate())

    assert "💎 Affare: 78/100 · −45% vs ~34.00 EUR (62 annunci)" in message
    assert len(message) <= CAPTION_MAX_CHARS
    assert_balanced_html(message)


def test_caption_with_insufficient_history() -> None:
    message = format_item_message(
        make_item(),
        make_estimate(sample_size=4, score=None, discount_pct=None),
    )

    assert "📊 Storico insufficiente per una stima (n=4)" in message
    assert "💎" not in message
    assert len(message) <= CAPTION_MAX_CHARS
    assert_balanced_html(message)


def test_caption_with_no_data_has_no_estimate_line() -> None:
    empty = make_estimate(
        median=None, sample_size=0, observed_from=None, observed_to=None,
        score=None, discount_pct=None,
    )
    for estimate_arg in (None, empty):
        message = format_item_message(make_item(), estimate_arg)
        assert "💎" not in message and "📊" not in message
        assert len(message) <= CAPTION_MAX_CHARS
        assert_balanced_html(message)


def test_caption_price_above_median_shows_plus() -> None:
    message = format_item_message(
        make_item(), make_estimate(score=0, discount_pct=-0.10)
    )
    assert "+10% vs" in message  # sopra mediana: mai "−-10%"


# ------------------------------------------------ (e) round-trip fixture


def test_every_fixture_item_produces_valid_caption(catalog_page: dict[str, Any]) -> None:
    items = parse_items(catalog_page["items"])
    assert items
    for item in items:
        message = format_item_message(item)
        assert 0 < len(message) <= CAPTION_MAX_CHARS
        assert_balanced_html(message)
        assert "Apri su Vinted</a>" in message
