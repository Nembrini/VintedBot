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

**Dedup tra esecuzioni**: per default vengono mostrati SOLO gli annunci
mai visti prima; quelli mostrati vengono marcati nel DB SQLite
(`VINTEDBOT_DB_PATH`). Esecuzioni ripetute mostrano quindi solo le novità
("Nessun nuovo annuncio" è l'esito normale, exit code 0). Alla **prima
esecuzione assoluta** il DB è vuoto: tutti i risultati appaiono come
nuovi — è atteso.

Flag aggiuntivi:
- `--all` — modalità consultazione: mostra tutti i risultati, bypassa il
  filtro e **non scrive nulla** nel DB;
- `--purge-days N` — prima della ricerca elimina i record visti da più
  di N giorni (N deve essere positivo).

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
  repository.py    ItemRepository: tutte le query su seen_items (unico SQL)
  app.py           orchestrazione: cerca → filtra visti → render → mark_seen
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
- [x] 2.2 Repository (`repository.py`: filter_new, mark_seen, purge, count)
- [x] 2.3 Integrazione nel CLI (`app.py`: dedup tra esecuzioni, --all, --purge-days)
- [ ] 2.4 Test end-to-end + verifica reale con doppia esecuzione
