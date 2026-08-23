"""Shared test fixtures. No network access anywhere in the suite."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture()
def catalog_page() -> dict[str, Any]:
    """The real catalog response captured during the step-1.4 live check."""
    raw = (FIXTURES_DIR / "catalog_items_page1.json").read_text(encoding="utf-8")
    data: dict[str, Any] = json.loads(raw)
    return data
