"""Tests for the pure pricing engine (curve, shrinkage, edge cases)."""

from __future__ import annotations

from decimal import Decimal

from vintedbot.pricing import estimate
from vintedbot.repository import PriceObservation

DEFAULTS = {"min_sample_size": 8, "max_discount": 0.60, "confidence_k": 10}


def obs(prices: list[str]) -> list[PriceObservation]:
    return [
        PriceObservation(
            item_id=i,
            price=Decimal(p),
            observed_at=f"2026-08-{(10 + i) % 28 + 1:02d}T00:00:00+00:00",
        )
        for i, p in enumerate(prices, start=1)
    ]


# ------------------------------------------------------------- (a) mediana


def test_median_odd_and_even_samples() -> None:
    odd = estimate(Decimal("10"), obs(["10", "20", "30"]), **DEFAULTS)
    assert odd.median == Decimal("20")

    even = estimate(Decimal("10"), obs(["10", "20", "30", "40"]), **DEFAULTS)
    assert even.median == Decimal("25")
    assert even.observed_from < even.observed_to  # span coerente


# ------------------------------------------------- (b) campione sotto minimo


def test_small_sample_has_median_but_no_score() -> None:
    result = estimate(Decimal("5"), obs(["10", "20", "30", "40"]), **DEFAULTS)

    assert result.median == Decimal("25")
    assert result.sample_size == 4
    assert result.score is None            # "non lo so"...
    assert result.discount_pct is None     # ...è diverso da "non è un affare"


def test_no_observations_at_all() -> None:
    result = estimate(Decimal("5"), [], **DEFAULTS)
    assert result.median is None
    assert result.sample_size == 0
    assert result.score is None


# ------------------------------------------------------------- (c) la curva


def flat_market(n: int, price: str = "100") -> list[PriceObservation]:
    return obs([price] * n)


def test_price_at_median_scores_zero() -> None:
    result = estimate(Decimal("100"), flat_market(50), **DEFAULTS)
    assert result.score == 0


def test_price_above_median_never_negative() -> None:
    result = estimate(Decimal("150"), flat_market(50), **DEFAULTS)
    assert result.score == 0
    assert result.discount_pct is not None and result.discount_pct < 0


def test_max_discount_with_huge_sample_approaches_100() -> None:
    # sconto 60% = max_discount → raw 100; n=1000 → confidence ≈ 0.99
    result = estimate(Decimal("40"), flat_market(1000), **DEFAULTS)
    assert result.score is not None and result.score >= 95


def test_thirty_percent_discount_with_huge_sample_is_about_50() -> None:
    result = estimate(Decimal("70"), flat_market(1000), **DEFAULTS)
    assert result.score is not None and 48 <= result.score <= 50


def test_shrinkage_penalizes_small_samples() -> None:
    small = estimate(Decimal("70"), flat_market(8), **DEFAULTS)    # n = minimo
    large = estimate(Decimal("70"), flat_market(200), **DEFAULTS)

    assert small.score is not None and large.score is not None
    assert small.score < large.score - 15  # stesso sconto, fiducia ben diversa
    assert small.score >= 0


def test_discount_beyond_max_is_clamped() -> None:
    # sconto 90% > max 60% → raw resta 100, non esplode
    capped = estimate(Decimal("10"), flat_market(1000), **DEFAULTS)
    at_max = estimate(Decimal("40"), flat_market(1000), **DEFAULTS)
    assert capped.score == at_max.score
