# VintedBot

Tool personale che monitora i nuovi annunci Vinted secondo filtri salvati
(categoria, brand, taglia, prezzo massimo) e segnala gli articoli
sottoprezzati rispetto ad annunci comparabili.

> ⚠️ Vinted non ha API pubbliche: il tool usa l'endpoint interno del sito
> (vedi [docs/api_notes.md](docs/api_notes.md)). Uso personale, volumi bassi,
> rate limiting prudente.

## Requisiti

- Python ≥ 3.12
- [uv](https://docs.astral.sh/uv/) per la gestione di ambienti e dipendenze

## Setup

```bash
git clone <repo-url> && cd VintedBot
uv sync            # crea .venv e installa dipendenze (incluse quelle dev)
cp .env.example .env   # opzionale: personalizza la configurazione
```

## Uso

```bash
# ricerca con filtri (ID categoria/taglia: vedi docs/api_notes.md)
uv run vintedbot search --catalog 2536 --size 208 --max-price 20

# equivalente:
uv run python -m vintedbot search --keyword "giubbotto" --max-pages 2
```

Opzioni di `search`: `--keyword`, `--catalog ID`, `--brand ID`, `--size ID`,
`--condition ID` (tutte ripetibili), `--min-price`, `--max-price`,
`--max-pages`, `--max-items`. Output: tabella ordinata per data di
pubblicazione decrescente + riga di riepilogo.

## Comandi di sviluppo

```bash
uv run pytest          # test (nessuna chiamata di rete: fixture reali + mock)
uv run mypy            # type check (strict)
uv run ruff check .    # lint
```

## Configurazione

Tutta la configurazione passa da variabili d'ambiente con prefisso
`VINTEDBOT_` (o dal file `.env`). Vedi [.env.example](.env.example) per
l'elenco completo e i default. Nessun valore è hardcoded nel codice.

## Struttura

```
src/vintedbot/     codice applicativo (tipizzato, py.typed)
  config.py        settings via pydantic-settings (.env)
  log.py           logging strutturato (structlog)
  models.py        SearchFilters / Item (parsing tollerante del JSON API)
  client.py        VintedClient async (curl_cffi, rate limit, retry)
  search.py        search_all: paginazione sequenziale + dedup + limiti
  db.py            SQLite: apertura, migrazioni (user_version), pragmas
  cli.py           CLI argparse + tabella rich (unico layer con print)
  __main__.py      python -m vintedbot
tests/             test pytest (+ fixtures/ con risposta API reale)
docs/api_notes.md  reverse engineering dell'endpoint di ricerca Vinted
```

## Stato

- [x] 1.1 Reverse engineering endpoint di ricerca (`docs/api_notes.md`)
- [x] 1.2 Setup progetto (uv, config, logging, test)
- [x] 1.3 Modelli dati Pydantic (`models.py`)
- [x] 1.4 Client HTTP async (`client.py`) — verificato live 2026-08-23
- [x] 1.5 Ricerca paginata (`search.py`)
- [x] 1.6 CLI con output rich (`cli.py`)
- [x] 1.7 Test suite (zero rete)
- [x] 2.1 Modulo DB SQLite (`db.py`: schema seen_items, migrazioni)
- [ ] 2.2 Repository (accesso dati per il flusso di ricerca)
