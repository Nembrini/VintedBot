# Vinted — note di reverse engineering dell'API interna

> Ultimo aggiornamento: 2026-08-23 — verificato live su `www.vinted.it`
> (browser + probe `curl_cffi`). Nessuna API pubblica documentata: tutto
> ciò che segue può cambiare senza preavviso.

## 1. Endpoint di ricerca catalogo

```
GET https://www.vinted.it/api/v2/catalog/items
```

- Metodo: `GET`, risposta `application/json`.
- Stesso endpoint su ogni dominio nazionale (`vinted.fr`, `vinted.de`, …);
  i cookie di sessione sono **per dominio**: bootstrap e chiamate API devono
  usare lo stesso host.
- Il primo render della pagina catalogo è server-side: la XHR compare solo
  per paginazione/cambio filtri. La chiamata diretta all'endpoint funziona
  comunque (verificato).

### Query params osservati / noti

| Param | Tipo | Note |
|---|---|---|
| `page` | int | 1-based |
| `per_page` | int | il sito usa 96; valori piccoli ok |
| `search_text` | str | ricerca full-text, opzionale |
| `catalog_ids` | csv di int | categoria (es. `2536`); sull'URL web appare come `catalog[]` |
| `brand_ids` | csv di int | marca |
| `size_ids` | csv di int | taglia (es. `208` = M uomo) |
| `status_ids` | csv di int | condizione (nuovo con cartellino, ottime, …) |
| `color_ids` / `material_ids` | csv di int | colore / materiale |
| `price_from` / `price_to` | decimale | range prezzo |
| `currency` | str | es. `EUR` |
| `order` | str | `newest_first` (il nostro caso), `relevance`, `price_low_to_high`, `price_high_to_low` |

**Come scoprire gli ID** (categoria/brand/taglia/condizione): applicare il
filtro sul sito e leggerli dall'URL della pagina catalogo
(`catalog[]=2536&size_ids[]=208&price_to=20…`) o dalla XHR in DevTools →
Network. Gli ID sono stabili e condivisi tra i domini nazionali.

## 2. Autenticazione e anti-bot (il punto critico)

1. **Serve una sessione anonima**: `GET https://www.vinted.it/` imposta i
   cookie `access_token_web` (JWT anonimo), `refresh_token_web`, `anon_id`,
   `v_udt`. Con questi cookie le chiamate `/api/v2/*` rispondono 200.
   Non serve alcun login.
2. **DataDome + TLS fingerprinting**: client HTTP "nudi" (curl, `requests`,
   `httpx`) ricevono **403 già sulla homepage** — il fingerprint TLS viene
   riconosciuto come bot, i cookie di sessione non vengono mai emessi.
   Verificato: `curl` → 403; `curl_cffi` con `impersonate="chrome"` → 200.
3. **Scelta client**: `curl_cffi` (impersona il TLS/HTTP2 fingerprint di
   Chrome reale). Header minimi: quelli iniettati da `impersonate` +
   `Accept: application/json`.

### Ciclo di vita della sessione

- Bootstrap: 1 GET alla homepage → cookie jar pronto.
- `401` su una chiamata API → token scaduto: rifare il bootstrap
  (ri-GET homepage) e ritentare una volta.
- `403` → blocco DataDome: backoff lungo (minuti), non insistere;
  eventualmente ruotare la sessione.
- Rate limit non documentato: restare prudenti (default del progetto:
  ≤ 12 richieste/minuto, configurabile).

## 3. Struttura della risposta

```jsonc
{
  "items": [ /* array di item, vedi sotto */ ],
  "pagination": {
    "current_page": 1,
    "total_pages": 480,
    "total_entries": 960,   // ⚠ cap: non è il totale reale del catalogo
    "per_page": 2,
    "time": 1787478123      // unix ts della ricerca
  },
  "search_tracking_params": { /* telemetria, ignorabile */ },
  "code": 0
}
```

### Campi rilevanti di un item

```jsonc
{
  "id": 9748292060,
  "title": "Giubbotto uomo",
  "price":            { "amount": "3.0",  "currency_code": "EUR" }, // ⚠ amount è stringa
  "service_fee":      { "amount": "...", "currency_code": "EUR" },  // protezione acquisti
  "total_item_price": { "amount": "...", "currency_code": "EUR" },  // prezzo + fee
  "brand_title": "Givova",          // stringa, può essere ""
  "size_title": "M",                // stringa localizzata
  "status": "Ottime",               // condizione, LOCALIZZATA (it) — non è un enum stabile
  "url": "https://www.vinted.it/items/9748292060-giubbotto-uomo",
  "path": "/items/9748292060-giubbotto-uomo",
  "promoted": false,                // annuncio sponsorizzato
  "favourite_count": 1,
  "view_count": 0,
  "is_visible": true,
  "user": {
    "id": 49348819,
    "login": "sabrina.m7980",
    "profile_url": "https://www.vinted.it/member/49348819-sabrinam7980",
    "business": false
  },
  "photo": { /* foto principale, stessa struttura degli elementi di photos */ },
  "photos": [
    {
      "url": ".../f800/....jpeg?s=...",        // ~800px
      "full_size_url": "...",
      "dominant_color": "#85838B",
      "is_main": true,
      "thumbnails": [ { "type": "thumb310x430", "url": "..." }, ... ],
      "high_resolution": { "id": "...", "timestamp": 1787477967 }  // ⚠ vedi sotto
    }
  ],
  "item_box": { "first_line": "...", "second_line": "...", ... }  // testo UI card
}
```

**Note importanti:**

- **Data di pubblicazione: non esposta direttamente.** Il proxy
  comunemente usato è `photo.high_resolution.timestamp` (unix ts di upload
  della foto ≈ momento di creazione dell'annuncio). Per il monitoraggio
  "nuovi annunci" conviene comunque basarsi su `order=newest_first` +
  dedup per `id` nel nostro DB, non sul timestamp.
- La **descrizione completa** dell'articolo NON è nella risposta di
  catalogo: serve `GET /api/v2/items/{id}` (o la pagina item) — da
  verificare quando servirà.
- `price.amount` è una **stringa decimale**: parsare come `Decimal`, mai float.
- `status` è localizzato secondo il dominio: filtrare per condizione va
  fatto lato richiesta con `status_ids`, non confrontando la stringa.
- Filtrare `promoted == true` se vogliamo solo annunci organici.

## 4. Altri endpoint osservati (non usati per ora)

- `GET /api/v2/info_banners/catalog`, `/api/v2/banners` — UI, ignorabili.
- `GET /api/v2/promoted_closets?...` — armadi sponsorizzati; accetta gli
  stessi parametri di filtro del catalogo.

## 5. Rischi e vincoli

- Scraping vietato dai ToS Vinted: uso personale, volumi bassi, rate
  limiting prudente. Rischio pratico: ban IP / blocco DataDome.
- Endpoint e schema non contrattuali: i modelli Pydantic devono tollerare
  campi extra (`extra="ignore"`) e i test devono usare fixture JSON reali
  salvate, così un cambio schema si individua subito.
