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

**Notifiche Telegram** (in costruzione — step 3): configura
`TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID` nel `.env` (bot creato con
@BotFather; ricorda di avviarlo con /start) e verifica con:

```bash
uv run vintedbot notify-test
```

Senza credenziali Telegram il comando `search` funziona comunque.

Flag aggiuntivi:
- `--all` — modalità consultazione: mostra tutti i risultati, bypassa il
  filtro "già visti" e non notifica. Eccezione (voluta): le
  **osservazioni prezzo vengono registrate comunque** — osservare non è
  notificare;
- `--purge-days N` — prima della ricerca elimina, con la stessa soglia,
  sia i record visti sia le **osservazioni prezzo** più vecchie di N
  giorni (N deve essere positivo);
- `--no-notify` — salta le notifiche Telegram (solo tabella).

### Storico prezzi (base della stima di mercato)

Ogni esecuzione di `search` registra il prezzo di TUTTI gli item
restituiti (nuovi e già visti) in `price_observations`: una osservazione
per item per esecuzione — un annuncio riapparso ribassato è informazione
preziosa. Brand normalizzato (trim+lowercase), categoria attribuita solo
se la ricerca aveva una sola `--catalog`. Ispezione:

```bash
uv run vintedbot stats
```

mostra le combinazioni (brand, categoria) con osservazioni, campione
effettivo (dedup per item + finestra `PRICING_MAX_AGE_DAYS`) e mediana.
Con `stats --evaluate PREZZO --brand X [--catalog Y]` valuti un prezzo a
mano contro lo storico. Per popolare lo storico senza toccare visto/
notifiche: `vintedbot backfill --catalog … --brand …` (ignora
`--max-price` con un warning: lo storico ha bisogno anche dei prezzi
alti).

## Punteggio affare

### Come funziona, in parole semplici

1. Per ogni annuncio si guarda **quanto costano di solito** gli articoli
   dello stesso brand nella stessa categoria (la **mediana** dello
   storico prezzi, entro la finestra temporale).
2. Si calcola **quanto è più economico** di quella mediana.
3. Lo sconto diventa un punteggio 0-100: uno sconto pari a
   `MAX_DISCOUNT` vale il massimo, la mediana vale 0, un prezzo sopra la
   mediana vale comunque 0 (mai negativo).
4. Il punteggio viene poi **ridotto se lo storico è povero**: con pochi
   annunci di confronto la stima è incerta, quindi il bot è prudente.

```
sconto     = (mediana − prezzo) / mediana
grezzo     = min(sconto / MAX_DISCOUNT, 1) · 100
fiducia    = n / (n + K)          # n = annunci di confronto
punteggio  = grezzo · fiducia
```

Sotto `MIN_SAMPLE_SIZE` annunci di confronto il punteggio è **n/d**:
"non lo so" è diverso da "non è un affare".

### I quattro parametri di taratura (`.env`)

| Variabile | Default | Effetto pratico |
|---|---|---|
| `VINTEDBOT_PRICING_MAX_DISCOUNT` | `0.60` | Quale sconto vale 100. **Abbassarlo rende la curva più generosa** (con 0.50, −40% vale 76 invece di 63); alzarlo la rende severa. |
| `VINTEDBOT_PRICING_CONFIDENCE_K` | `10` | **Alzarlo rende il punteggio più prudente sui brand con poco storico** (con K=50 servono molti più annunci prima di dare punteggi alti); abbassarlo si fida subito. |
| `VINTEDBOT_PRICING_MIN_SAMPLE_SIZE` | `8` | Sotto questa soglia niente punteggio. Alzarlo = più annunci "n/d" ma stime più solide. |
| `VINTEDBOT_PRICING_MAX_AGE_DAYS` | `90` | Quanto guardare indietro. Abbassarlo segue i prezzi correnti, alzarlo dà campioni più grandi ma più vecchi. |

Il punteggio appare nella colonna "Affare" della tabella e nella caption
Telegram: 💎 con stima piena, 📊 se lo storico è insufficiente, nessuna
riga se non ci sono dati.

### Filtrare le notifiche

```bash
# notifica solo gli affari veri (score >= 60)
uv run vintedbot search --catalog 257 --brand 20117 --min-score 60

# ...e scarta anche quelli senza storico sufficiente
uv run vintedbot search --catalog 257 --brand 20117 --min-score 60 --strict-score
```

- Senza `--min-score` il filtro è **spento**: si notifica tutto, con il
  punteggio in caption dove disponibile.
- Gli annunci **senza punteggio passano** (meglio un falso allarme che un
  affare perso), salvo `--strict-score`.
- Un annuncio sotto soglia è scartato **definitivamente** (`skipped_at`):
  non rientra dalla coda nei giri successivi.
- Gli arretrati sono rivalutati a ogni giro coi criteri correnti; la coda
  è ordinata per punteggio decrescente, quindi quando l'anti-valanga
  taglia partono prima gli affari migliori.
- La tabella a schermo mostra sempre **tutti** i nuovi: il filtro agisce
  solo sulle notifiche.

### Procedura di collaudo dello step 4 (eseguita il 2026-08-23)

1. `uv run pytest` verde; `uv run vintedbot stats` per verificare che la
   combinazione da collaudare abbia campione ≥ 30.
2. Taratura a tavolino: `stats --evaluate` su mediana, −20%, −40%, −60%
   e confronto della curva; eventuale ritocco dei parametri nel `.env`.
3. Run 1 senza filtro → notifiche con riga punteggio, ordinate per score.
4. Run 2 con `--min-score` → solo sopra soglia, `S sotto soglia scartati`
   nel riepilogo, `skipped_at` valorizzato in DB.
5. Run 3 identica → gli scartati non tornano; la coda arretrata si smaltisce.
6. Run 4 con `--strict-score` su un brand senza storico → zero notifiche,
   tutti skipped.

### Semantica visto / notificato

Sono due momenti distinti:
- **visto** (`first_seen_at`): l'annuncio è stato mostrato in tabella —
  non riapparirà nelle esecuzioni successive;
- **notificato** (`notified_at`): la notifica Telegram è partita DAVVERO.
  Viene valorizzato solo a invio riuscito, item per item.

Gli item "visti ma non notificati" (invio fallito, o oltre il limite
anti-valanga) restano in coda e vengono **ritentati automaticamente** ai
giri successivi. Un errore di configurazione (token invalido, chat non
trovata) interrompe l'intera coda con exit code ≠ 0.

**Anti-valanga**: al massimo `VINTEDBOT_MAX_NOTIFICATIONS_PER_RUN`
notifiche per esecuzione (default 10), con una pausa di
`VINTEDBOT_NOTIFY_PAUSE_SECONDS` tra gli invii (default 1s). Alla prima
esecuzione su DB vuoto arrivano quindi al più 10 messaggi; il resto viene
smaltito nei giri successivi.

**Formato notifica**: album Telegram con TUTTE le foto dell'annuncio
(max 10) e didascalia con titolo, prezzo, brand/taglia/condizione, data
e ora di caricamento (fuso italiano) e link all'annuncio. Se Telegram
non riesce a scaricare una foto (capita con il CDN Vinted:
`WEBPAGE_CURL_FAILED`), il bot scarta solo quella e ritenta l'album;
in ultima istanza degrada a foto singola e poi a solo testo — la
notifica arriva comunque.

### Procedura di collaudo dello step 3 (eseguita il 2026-08-23)

1. Suite verde: `uv run pytest`. DB azzerato per stato noto.
2. Run 1 (`--max-items 12`, cap 10): 12 nuovi → tabella, 10 notifiche
   con album+data ricevute, 2 in coda → anti-valanga OK.
3. Verifica DB: `notified_at` valorizzato solo per gli invii riusciti.
4. Run 2 identica: 0 nuovi, coda arretrata smaltita, **zero doppioni**.
5. Run 3 a coda vuota: "Nessun nuovo annuncio", zero notifiche, exit 0.
6. Run 4 `--no-notify`: zero messaggi Telegram.

## Come funziona il tracking dei doppioni

Ogni annuncio mostrato viene registrato in una tabella `seen_items` del
DB SQLite (`VINTEDBOT_DB_PATH`, default `data/vintedbot.db`): id Vinted,
titolo, prezzo (stringa decimale), valuta, brand, URL, `first_seen_at`
(UTC ISO-8601) e `notified_at` (NULL finché non esisteranno le notifiche,
step 3). Alle esecuzioni successive gli id già presenti vengono filtrati
e non riappaiono.

- **Azzerare il tracking**: eliminare il file del DB (`data/vintedbot.db`
  più gli eventuali `-wal`/`-shm`); verrà ricreato alla prossima
  esecuzione e tutti gli annunci torneranno "nuovi".
- **`--purge-days N`**: pulizia selettiva — elimina solo i record visti
  da più di N giorni (utile per non far crescere il DB all'infinito).
- **Nota**: può capitare che un'esecuzione ravvicinata mostri come
  "nuovi" annunci con data di pubblicazione vecchia: sono annunci
  rientrati nella prima pagina (bump/riattivazione lato Vinted). È il
  comportamento corretto del dedup: mai visti prima → mostrati una volta.

### Procedura di collaudo riproducibile

1. Suite verde senza rete: `uv run pytest` (47 test).
2. Stato noto: rimuovere `data/vintedbot.db` se presente.
3. `uv run vintedbot search --catalog 2536 --size 208 --max-price 20
   --max-pages 1` → attesi *N nuovi / 0 già visti / N totali*, DB creato
   con N righe (`notified_at` NULL, prezzi stringhe decimali).
4. Stesso comando subito dopo → attesi 0 nuovi ("Nessun nuovo annuncio",
   exit 0) o pochissimi annunci realmente mai visti (vedi Nota sopra).
5. Stesso comando con `--all` → tutti gli item di nuovo visibili,
   conteggio righe del DB invariato.

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
  notifier.py      TelegramNotifier (API Bot HTTP, retry, token mai nei log)
  formatting.py    caption HTML per gli item (escaping, limite 1024)
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
- [x] 2.4 Collaudo reale end-to-end (doppia esecuzione + --all) — 2026-08-23
- [x] 3.1 Notifier Telegram (`notifier.py` + `notify-test`) — verificato live
- [x] 3.2 Notifica item con foto (`formatting.py`, `send_item`, `notify-test --with-item`) — verificata live
- [x] 3.3 Notifiche nel flusso di ricerca (notified_at, retry arretrati, anti-valanga, --no-notify)
- [x] 3.4 Collaudo end-to-end reale — 2026-08-23 (album foto + data caricamento aggiunti su richiesta)
- [x] 4.1 Storico prezzi (`price_observations`, migrazione v4, comando `stats`)
- [x] 4.2 Motore di stima (`pricing.py`), `backfill`, filtro `--min-score` (migrazione v5)
- [x] 4.3 Collaudo reale end-to-end del filtro affare — 2026-08-23
- [ ] 5. Scheduler (esecuzione periodica automatica)
