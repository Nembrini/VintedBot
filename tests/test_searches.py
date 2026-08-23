"""Tests for loading and validating searches.toml (pure, no I/O beyond tmp files)."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from vintedbot.searches import SearchConfigError, load_searches

if TYPE_CHECKING:
    from pathlib import Path

VALID = """
[[search]]
name = "cavalli"
catalog = 257
brand = [1965, 20117]
size = [208, 209]
max_price = 100
min_score = 60

[[search]]
name = "giubbotti"
enabled = false
catalog = 2536
size = 208
keyword = "piumino"

[[search]]
name = "minimal"
"""


def write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "searches.toml"
    path.write_text(content, encoding="utf-8")
    return path


# ------------------------------------------------------------ (a) parsing ok


def test_valid_file_parses_all_and_marks_enabled(tmp_path: Path) -> None:
    searches = load_searches(write(tmp_path, VALID))

    assert [s.name for s in searches] == ["cavalli", "giubbotti", "minimal"]
    assert [s.name for s in searches if s.enabled] == ["cavalli", "minimal"]

    first = searches[0]
    assert first.catalog == (257,)
    assert first.brand == (1965, 20117)
    assert first.size == (208, 209)
    assert first.max_price == Decimal("100")
    assert first.min_score == 60
    assert first.strict_score is False
    # scalare accettato come lista di uno
    assert searches[1].size == (208,)


def test_to_filters_maps_onto_search_options(tmp_path: Path) -> None:
    search = load_searches(write(tmp_path, VALID))[0]
    filters = search.to_filters()

    assert filters.category_ids == (257,)
    assert filters.brand_ids == (1965, 20117)
    assert filters.size_ids == (208, 209)
    assert filters.price_max == Decimal("100")
    assert filters.to_query_params()["catalog_ids"] == "257"


# ------------------------------------------------- (b) errori di validazione


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ('[[search]]\nname = "x"\ncatalog = [\n', "non è un TOML valido"),
        ('[[search]]\nname = "x"\n\n[[search]]\nname = "x"\n', "nome duplicato"),
        ('[[search]]\nname = "x"\nmin_scor = 60\n', "campo sconosciuto"),
        ('[[search]]\nname = "x"\nmin_score = 150\n', "min_score"),
        ('[[search]]\nname = "x"\nstrict_score = true\n', "strict_score richiede min_score"),
        ('[[search]]\nname = "x"\nenabled = false\n', "nessuna ricerca abilitata"),
        ("", "nessuna ricerca"),
        ('[[search]]\nname = "x"\nbrand = "Carhartt"\n', "non è un ID numerico"),
        ('[[search]]\nname = ""\n', "name"),
        ('[[search]]\nname = "x"\nmin_price = 50\nmax_price = 10\n', "min_price"),
    ],
)
def test_invalid_files_raise_readable_errors(
    tmp_path: Path, content: str, expected: str
) -> None:
    with pytest.raises(SearchConfigError) as excinfo:
        load_searches(write(tmp_path, content))

    message = str(excinfo.value)
    assert expected in message
    assert "Traceback" not in message
    # gli errori di campo citano la ricerca colpevole: nome, o posizione
    # quando il nome manca/è vuoto (quelli di file non citano nulla)
    if message.startswith("ricerca "):
        assert "'x'" in message or "#1" in message


def test_min_score_error_names_search_and_field(tmp_path: Path) -> None:
    path = write(tmp_path, '[[search]]\nname = "maglioni-m"\nmin_score = 150\n')

    with pytest.raises(SearchConfigError) as excinfo:
        load_searches(path)

    assert "ricerca 'maglioni-m'" in str(excinfo.value)
    assert "min_score" in str(excinfo.value)


# ------------------------------------------------------- (c) file assente


def test_missing_file_suggests_the_example(tmp_path: Path) -> None:
    with pytest.raises(SearchConfigError, match="searches.example.toml"):
        load_searches(tmp_path / "assente.toml")


def test_shipped_example_file_is_valid() -> None:
    from pathlib import Path as RealPath

    example = RealPath("searches.example.toml")
    if not example.exists():  # pragma: no cover - eseguito dalla root del progetto
        pytest.skip("eseguire dalla radice del progetto")

    searches = load_searches(example)
    assert [s.name for s in searches] == ["cavalli-jeans", "giubbotti-m-economici"]
