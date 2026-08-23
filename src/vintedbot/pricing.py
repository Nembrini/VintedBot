"""Market-price estimation: median + deal score. Pure module, no I/O.

The observations come from :meth:`PriceRepository.get_observations`
(already deduped per item and windowed); this module only does math.

Score curve (0-100), documented by example with the defaults
(``max_discount=0.60``, ``confidence_k=10``):

    discount = (median - price) / median          # quota di sconto vs mercato
    raw      = clamp(discount / max_discount, 0, 1) * 100
    confidence = n / (n + k)                      # shrinkage sul campione
    score    = round(raw * confidence)

Example: median 40.00, price 22.00 → discount 0.45; raw = 0.45/0.60·100
= 75. With n=62 observations: confidence = 62/72 ≈ 0.861 → score 65.
Same discount with n=8: confidence = 8/18 ≈ 0.444 → score 33 — a small
sample must not scream "affare".

Below ``min_sample_size`` the median is still reported when computable,
but score and discount are None: "non lo so" is different from "non è
un affare".
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from decimal import Decimal

    from vintedbot.repository import PriceObservation


@dataclass(frozen=True, slots=True)
class PriceEstimate:
    """Outcome of a market comparison for one price.

    Attributes:
        median: market median of the (deduped, windowed) observations;
            None when there are no observations at all.
        sample_size: observations the estimate is based on.
        observed_from / observed_to: ISO timestamps of the oldest and
            newest observation in the sample (None when empty).
        score: 0-100 deal score; None when the sample is too small.
        discount_pct: (median - price) / median as a float (can be
            negative when the price is above median); None like score.
    """

    median: Decimal | None
    sample_size: int
    observed_from: str | None
    observed_to: str | None
    score: int | None
    discount_pct: float | None


def estimate(
    price: Decimal,
    observations: Sequence[PriceObservation],
    *,
    min_sample_size: int,
    max_discount: float,
    confidence_k: int,
) -> PriceEstimate:
    """Estimate how good ``price`` is against the observed market.

    Decimal end to end; floats appear only in the final curve (ratios).
    A price at or above the median scores 0 — never negative.
    """
    sample_size = len(observations)
    if sample_size == 0:
        return PriceEstimate(
            median=None,
            sample_size=0,
            observed_from=None,
            observed_to=None,
            score=None,
            discount_pct=None,
        )

    timestamps = [obs.observed_at for obs in observations]
    median = statistics.median(obs.price for obs in observations)

    if sample_size < min_sample_size:
        return PriceEstimate(
            median=median,
            sample_size=sample_size,
            observed_from=min(timestamps),
            observed_to=max(timestamps),
            score=None,
            discount_pct=None,
        )

    discount = (median - price) / median  # Decimal
    discount_pct = float(discount)
    raw = min(max(discount_pct / max_discount, 0.0), 1.0) * 100.0
    confidence = sample_size / (sample_size + confidence_k)
    score = round(raw * confidence)

    return PriceEstimate(
        median=median,
        sample_size=sample_size,
        observed_from=min(timestamps),
        observed_to=max(timestamps),
        score=score,
        discount_pct=discount_pct,
    )
