# Phase 0 — Research Findings

> Status: verified against primary sources where possible. Every factual claim is
> cited. Anything I could **not** confirm from an authoritative source is marked
> **[ASSUMPTION]** and is treated as a configurable / runtime-discoverable value in
> the code rather than a hard-coded magic constant.
>
> Last updated: 2026-06-14.

---

## 1. Kalshi REST + WebSocket API

### 1.1 Base URLs (environments)

| Environment | REST base | WebSocket base |
|-------------|-----------|----------------|
| **Production** | `https://external-api.kalshi.com/trade-api/v2` | `wss://external-api-ws.kalshi.com/trade-api/ws/v2` |
| **Demo / sandbox** | `https://external-api.demo.kalshi.co/trade-api/v2` | `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2` |

Legacy aliases still seen in the wild (kept configurable, **[ASSUMPTION]** that they
still resolve): `https://api.elections.kalshi.com/trade-api/v2`,
`https://trading-api.kalshi.com/trade-api/v2`, `https://demo-api.kalshi.co/trade-api/v2`.
The system **defaults to the demo environment** and never to production unless
explicitly configured.

> ✅ **Confirmed 2026-06-14** against the live API reference: every endpoint page
> (e.g. *Generate API Key*) shows the production host `external-api.kalshi.com/trade-api/v2`,
> matching `kalshi.rest_base_prod` in config. The demo host is the `…demo.kalshi.co`
> equivalent.

Sources:
- [Kalshi — API Environments and Endpoints](https://docs.kalshi.com/getting_started/api_environments)
- [Kalshi — Test In The Demo Environment](https://docs.kalshi.com/getting_started/demo_env)
- [Kalshi — Generate API Key (shows prod base URL)](https://docs.kalshi.com/api-reference/api-keys/generate-api-key)

### 1.2 Authentication & request signing

Kalshi's current API uses **API-key + RSA request signing** (the older
email/password token sessions expire ~30 min and are deprecated for programmatic
use). Each request carries three headers:

| Header | Value |
|--------|-------|
| `KALSHI-ACCESS-KEY` | the Key ID (UUID) shown when you create the key |
| `KALSHI-ACCESS-TIMESTAMP` | current time in **milliseconds** since epoch |
| `KALSHI-ACCESS-SIGNATURE` | base64( RSA-PSS-SHA256( `timestamp + METHOD + path` ) ) |

Signing details, confirmed from the docs:
- The signed message is the **concatenation** of `timestamp_ms` (string) + HTTP
  method (e.g. `GET`) + request path.
- The path **includes** the `/trade-api/v2` prefix and **excludes** the query string.
- Algorithm: **RSA-PSS** with **SHA-256** digest, MGF1, salt length = digest length.
- The signature is **base64**-encoded.
- The private key is shown **once** at creation (`RSA PRIVATE KEY` PEM) and is never
  retrievable again — Kalshi only stores the public half.

Implemented in `src/wc_kalshi/ingestion/kalshi/auth.py` and unit-tested with a locally
generated throwaway RSA key in `tests/test_kalshi_auth.py` (no network needed).

> ✅ **Confirmed 2026-06-14** against the live *Generate API Key* reference page: it lists
> exactly these three required headers — `KALSHI-ACCESS-KEY` ("Your API key ID"),
> `KALSHI-ACCESS-SIGNATURE` ("RSA-PSS signature of the request"), and
> `KALSHI-ACCESS-TIMESTAMP` ("Request timestamp in milliseconds") — matching this
> implementation exactly. Note: the first key must be created in the Kalshi **web
> dashboard** (the key-management REST endpoints themselves require an existing key),
> and demo/production keys are **separate**.

Sources:
- [Kalshi — API Keys](https://docs.kalshi.com/getting_started/api_keys)
- [Kalshi — Generate API Key (confirms the three auth headers + RSA-PSS)](https://docs.kalshi.com/api-reference/api-keys/generate-api-key)
- [QuantVPS — Kalshi Order Book API: Endpoints, Auth, and Connection Setup](https://www.quantvps.com/blog/kalshi-order-book-api-endpoints-explained)
- [AgentBets — Kalshi API Guide: RSA Auth & Demo Sandbox (2026)](https://agentbets.ai/guides/kalshi-api-guide/)

### 1.3 REST endpoints we use

| Operation | Method | Path |
|-----------|--------|------|
| List events | GET | `/events` |
| List markets | GET | `/markets` |
| Get single market | GET | `/markets/{ticker}` |
| Get orderbook | GET | `/markets/{ticker}/orderbook` |
| Get portfolio balance | GET | `/portfolio/balance` |
| Get positions | GET | `/portfolio/positions` |
| Create order | POST | `/portfolio/orders` |
| Cancel order | DELETE | `/portfolio/orders/{orderId}` |

`/events` and `/markets` accept filters (`series_ticker`, `event_ticker`, `status`,
`limit`, `cursor` for pagination). Order creation accepts a **`client_order_id`** for
idempotency — we always set it (see §5 of the main spec, idempotent orders).

Source: [Kalshi — API reference (`llms.txt`)](https://docs.kalshi.com/llms.txt)

### 1.4 WebSocket channels

A single authenticated WS connection multiplexes channels via subscribe commands:

| Channel | Purpose |
|---------|---------|
| `orderbook_delta` | real-time order-book price-level changes (snapshot + deltas) |
| `ticker` / `ticker_v2` | market price, volume, open-interest updates |
| `trade` | public trade prints |
| `fill` | **your** order fills (auth) |
| `order` / `market_positions` | your order + position updates (auth) |
| market/event lifecycle | market state changes (open/closed/settled) |

We use `orderbook_delta` + `ticker` for market data and `fill`/`order` for execution
reconciliation. The client falls back to REST polling if the socket drops.

Source: [Kalshi — API reference (`llms.txt`)](https://docs.kalshi.com/llms.txt)

### 1.5 Rate limits

The public docs describe a **token-bucket, tiered** model ("Rate Limits and Tiers";
"maximum batch size scales with your tier's write budget") but the excerpt available
to me did **not** publish per-tier numbers. **[ASSUMPTION]** (from community docs and
prior Kalshi behaviour): the entry "Basic" tier is on the order of **~10 reads/sec and
~5 writes/sec**, with higher tiers (Advanced/Premier) granting more. Because the
numbers are not authoritative, the system:
- treats poll intervals and burst sizes as **config values** (`config/default.yaml`),
- defaults to conservative medium-frequency polling (market data every ~2 s/market,
  not microsecond loops),
- wraps every call in `tenacity` retry/back-off that honours `429` + `Retry-After`.

This matches the spec's guidance that "rate limits make true HFT impractical — design
for event-driven / medium-frequency polling."

Sources:
- [Kalshi — API reference (`llms.txt`)](https://docs.kalshi.com/llms.txt)
- [Parlay.run — Kalshi API: The Complete Developer Guide (2026)](https://www.parlay.run/kalshi-api)

### 1.6 Fees (confirmed Feb 2026 schedule)

Kalshi charges a **parabolic** trading fee, largest at 50¢ and shrinking toward the
price extremes. The general per-order formula (rounded **up** to the next whole cent on
the order total):

```
taker_fee_dollars = ceil( fee_coeff * C * P * (1 - P) * 100 ) / 100
```

- `C` = number of contracts, `P` = price in dollars (0.01–0.99).
- General `fee_coeff = 0.07`. Some series carry a higher coefficient; the **maker** rate
  is roughly **¼** of the taker rate. Per-contract taker fee peaks at **$0.0175** at
  P=0.50 (un-rounded) and the published per-contract cap is **$0.035**.
- We implement this in `src/wc_kalshi/risk/sizing.py::kalshi_fee()` with a configurable
  coefficient + maker/taker flag, and **subtract estimated fees from edge** before any
  trade is considered actionable (spec §4.3).

The authoritative document is the live fee-schedule PDF (some series have bespoke
schedules); we keep the coefficient configurable so a desk can pin the exact series fee.

Sources:
- [Kalshi — Fee Schedule (Feb 2026, PDF)](https://kalshi.com/docs/kalshi-fee-schedule.pdf)
- [Market Math — Kalshi Fees Explained (2026)](https://marketmath.io/blog/kalshi-fees-guide-2026)
- [Deadspin — Understanding Kalshi Trading Fees in 2026](https://deadspin.com/prediction-markets/kalshi/fees/)

### 1.7 World Cup market mapping (live match → Kalshi market)

Kalshi lists 2026 FIFA World Cup markets under **Sports → Soccer → "WC 2026"**:
tournament winner (>$130 M volume; Spain favourite ≈ $0.178 at time of writing), group
winners, USMNT-advancement, Golden Boot, **50+ individual match markets**, and novelty
markets (hat-tricks, penalty shootouts, etc.).

**[ASSUMPTION]** The exact per-match **series/event/market ticker strings** are not
fully published in a stable doc, and Kalshi's sports tickers have changed format
historically. Rather than hard-code an unverified ticker (which would silently break),
the system **discovers** the mapping at runtime:
1. query `GET /events` / `GET /markets` filtered by the configured World Cup
   `series_ticker` (config: `kalshi.worldcup_series_ticker`, default a best-guess that
   is easily overridden),
2. fuzzy-match event titles to the two team names + kickoff date coming from the
   football feed,
3. cache the resolved `event_ticker` → `{home_yes, away_yes, draw_yes}` market tickers.

This mapping logic lives in `src/wc_kalshi/ingestion/kalshi/market_map.py` and is fully
unit-tested against a captured sample `/events` payload so it works offline. A
match-outcome on Kalshi is typically modelled as **three Yes/No markets** (home / draw /
away) or a single multi-outcome event; the code handles both shapes.

Sources:
- [Kalshi News — 2026 World Cup roundup: Spain leads Kalshi's markets](https://news.kalshi.com/p/kalshi-2026-world-cup-roundup)
- [CBS Sports — Kalshi World Cup 2026: how to trade](https://www.cbssports.com/prediction/news/kalshi-world-cup-2026/)
- [NexusFi — 2026 World Cup Prediction Markets on Kalshi & Polymarket](https://nexusfi.com/a/prediction-markets/world-cup-2026-prediction-markets)

---

## 2. Live football-data providers

Requirement: at least one **primary** + one **fallback**, with a note on free-tier
limits, latency, and live-xG availability.

| Provider | Live in-play? | Live xG? | Free tier | Notes |
|----------|---------------|----------|-----------|-------|
| **API-Football** (api-sports.io) — *PRIMARY* | Yes (`/fixtures?live=all`, `/fixtures/statistics`, `/fixtures/events`) | **Inconsistent** — `expected_goals` present in stats for some leagues/plans only | **100 req/day** (testing only) | Widest coverage, best docs. 100/day is far too low for continuous live polling → a **paid plan is required for real live trading**; default runtime uses the simulated feed so we never silently burn the quota. |
| **TheStatsAPI** — *FALLBACK / xG* | Yes | **Yes** — match xG via `/football/matches/{id}/stats`, per-shot xG via `/football/matches/{id}/shotmap`, on every plan for supported competitions | Free tier available | Strong live xG + analytics layer; used as the xG fallback. |
| **Sportmonks** | Yes | Yes (all xG metrics live) | Paid (trial) | Good live xG, heavier price. Listed as a secondary option. |
| **StatsBomb / FBref** | No (post-match) | Yes (high quality) | Scraping discouraged / ToS-restricted | Great for **priors & backtests**, not live. We do **not** scrape FBref in the live loop. |
| **Understat** | No official API | Yes (xG) | Scraping tolerated but no official API | Useful for historical xG priors only; **[ASSUMPTION]** scraping is tolerated — we gate it behind a config flag, respect robots.txt, and never use it in the hot path. |

**Decision:** `API-Football` = primary live feed; `TheStatsAPI` = fallback + live-xG
source; `SimulatedFootballProvider` = the **default** (no key, deterministic, drives the
whole pipeline offline for dev/test/CI). All three sit behind a single
`FootballDataProvider` interface (`ingestion/football/base.py`) so they are swappable.

**Live-data caveat:** with US summer kickoffs and a 100-req/day free tier, you cannot
realistically poll a live match continuously without a paid plan. This is documented in
the README so the user supplies a paid key (or points at the simulated/replay feed)
before expecting real end-to-end live ingestion.

Sources:
- [TheStatsAPI vs API-Football: Free Tier, Pricing & xG Compared](https://www.thestatsapi.com/blog/thestatsapi-vs-api-football)
- [TheStatsAPI — Football xG API (Expected Goals Data)](https://www.thestatsapi.com/football/xg)
- [Sportmonks — xG Data API](https://www.sportmonks.com/football-api/xg-data/)
- [TheStatsAPI — Best Free Football APIs in 2026](https://www.thestatsapi.com/blog/free-football-api-alternatives)

---

## 3. Cross-market consensus (Polymarket)

Polymarket exposes the same World Cup matches and is useful as a **consensus /
arbitrage signal**. Its public **Gamma API** is read-only, needs no signed wallet
transactions, and (per the docs) has "no rate limits to manage" — ideal for pulling a
second market-implied probability. The **CLOB API** powers order books / execution
(we only *read* it). A known data-quality gotcha: during a recent AFCON tournament the
Gamma API briefly marked ended matches / eliminated teams as still
active/accepting-orders — so we treat Polymarket as an **advisory** consensus input,
never as a sole source of truth, and cross-check `closed`/`active` flags.

**Decision:** implement a read-only `PolymarketConsensus` client
(`ingestion/consensus/polymarket.py`) that maps a match to a Polymarket market and
returns its implied probability for an optional "two-venue consensus" and
divergence/arbitrage alert. It is **off by default** (one config flag) and never places
orders.

Sources:
- [Chainstack — Polymarket API for Developers: Gamma API, Data, and Polygon RPC](https://chainstack.com/polymarket-api-for-developers/)
- [pm.wiki — Polymarket API Guide 2026 (CLOB, Gamma & Data API)](https://pm.wiki/learn/polymarket-api)
- [Polymarket/rs-clob-client #199 — Gamma API marks ended AFCON matches as active](https://github.com/Polymarket/rs-clob-client/issues/199)

---

## 4. Implications baked into the architecture

1. **Demo-first.** The default execution target is the Kalshi demo environment; `live`
   requires an explicit, separate flag *and* clears all guardrails.
2. **Runtime ticker discovery**, not hard-coded tickers, because the exact World Cup
   tickers aren't authoritatively published and Kalshi has changed sports-ticker
   formats before.
3. **Simulated feed is the default** football source so the system boots and the full
   pipeline runs with **zero API keys and zero quota burn**; real providers are opt-in.
4. **Conservative, configurable polling** + `tenacity` back-off honouring `429`, because
   exact rate-limit numbers aren't published and HFT is impractical here.
5. **Fees subtracted from edge** using the confirmed parabolic formula before any trade
   is deemed actionable, with the coefficient left configurable for bespoke series.

---

## 5. Open items / things to confirm before live trading

- [ ] Confirm the **exact** World Cup series ticker(s) by hitting `GET /events` with a
      funded/live key and snapshotting the response (the discovery code already does
      this; we just need to pin the cache).
- [ ] Confirm current **rate-limit tier numbers** for the account's tier from the
      Kalshi dashboard and set them in `config/default.yaml`.
- [ ] Confirm the **exact fee coefficient** for the World Cup series from the live fee
      schedule (default 0.07 general).
- [ ] Verify the chosen football provider actually returns **live xG** for World Cup
      fixtures on the purchased plan (xG coverage is plan/league dependent).

---

## 6. Historical / backtest data sources (real, non-circular evidence)

The synthetic backtest cannot prove a real edge (the simulated market is our own
invention). `wck historical` instead replays **real** timelines; two free/low-cost
sources feed it. Implemented in `backtest/statsbomb.py` and `backtest/betfair.py`.

### 6.1 StatsBomb open data (per-shot xG, goals, cards)

- Repo: [statsbomb/open-data](https://github.com/statsbomb/open-data) — licence
  **CC BY-NC-SA** (attribution required, non-commercial). We fetch from
  `raw.githubusercontent.com/statsbomb/open-data/master/data/...`, cache under
  `data/statsbomb/`, and **never redistribute** (git-ignored).
- World Cups with per-shot xG (verified against `competitions.json`): **men's
  competition 43, seasons 106 (2022) & 3 (2018)**; women's **72**, seasons 107 (2023) &
  30 (2019). Older men's seasons exist but lack xG and are skipped with a warning.
- Event schema used (verified against real data, e.g. 2022 final match `3869685`):
  `shot.statsbomb_xg`, `shot.outcome.name == "Goal"`, `type.name` ∈
  {`Shot`, `Own Goal For`, `Bad Behaviour`, `Foul Committed`}, card via
  `bad_behaviour.card.name` / `foul_committed.card.name` ∈ {`Red Card`, `Second Yellow`}.
- **Time semantics gotcha:** `minute` is period-based-continuous, **not** reset per period.
  The 1st half runs 0→45+ (stoppage can reach the low 50s) and the **2nd half starts at
  minute 45** and runs 45→90+. So 1st-half stoppage (min 46–52) *overlaps* 2nd-half
  regulation (min 45+). We therefore order events by `(period, minute, second, index)` and
  clamp each half onto its own band of a 0..90 grid (P1 → `min(m,45)`, P2 → `min(m,90)` —
  **no `+45` offset**, since P2's minute is already 45-based).
- StatsBomb has **no market prices** → this source measures **calibration only**.

### 6.2 Betfair historical exchange data (MATCH_ODDS prices → CLV)

- The closest analogue to Kalshi (an exchange, not a bookmaker), but **not Kalshi** — so
  results are reported as "edge vs the Betfair line", never "Kalshi CLV".
- Format: historical **stream** files (NDJSON `mcm` market-change messages, optionally
  `.bz2` / packed in `.tar`). Runner identity is taken from `marketDefinition.runners[]`
  (selectionId → name) — **never** inferred from `rc[]` alone. Prices from `ltp` (and
  `atb`/`atl` ladders when present). The **Advanced/PRO** tier is required for in-play
  prices; **BASIC** is pre-off only. Files are **user-supplied** under your own licence.
- **Clock alignment is best-effort.** Betfair soccer keeps `inPlay=true` across half-time
  (HT shows as `status: SUSPENDED`), so we detect HT from several signals — an inPlay
  flip, a sustained suspension, or the largest update gap in the 35–70-min window — and
  fall back to a fixed **+15′** offset (flagged as `clock_mode`). The **pre-off line**
  (last quote before in-play) is clock-independent and is our primary CLV reference.

### 6.3 Kalshi 90′ settlement (why we exclude ET/penalties)

Kalshi World Cup match contracts resolve on the result **after 90 minutes plus stoppage
time — not extra time or penalties**, and all 2026 venues are neutral for settlement. The
loader matches this: it keeps only StatsBomb periods 1–2 and settles on the 90′ score, so a
knockout level at 90′ settles **DRAW** (correct for a 90′ market). Confirm the exact
per-series wording from the live Kalshi contract before trading.

### 6.4 CLV measured against three reference lines

In-play CLV vs the *last* observed tick is degenerate near full time (prices → 0/1), so
`BacktestResult` reports CLV vs (a) the **pre-off** line (primary), (b) the quote **+5
match-minutes** after entry (drift), and (c) the **last tick** (shown with a warning).

### 6.5 Record-forward recorder

`wck record` runs the existing orchestrator **observe-only** (`trade=False`), persisting
real xG + read-only Kalshi snapshots to a DB that `wck replay` re-evaluates. It is the only
route to a real **Kalshi** CLV — available once 2026 WC markets list on prod.
