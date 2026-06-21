# World Cup × Kalshi — In-Play Edge Engine

A production-grade, modular Python system that ingests live **2026 FIFA World Cup**
match data, runs an in-play win/draw/loss probability model, compares it against
**Kalshi** event-contract prices to detect mispricings, and sizes + (optionally)
places trades — **defaulting to safe local simulation**.

> ⚠️ **Safety first.** The system boots in `paper` mode with **no API keys** and places
> **no real orders**. Real-money trading (`live`) is triple-gated and can never be
> reached by accident. Calibration and sane risk behaviour are the design priorities —
> not raw P&L.

---

## TL;DR — run it in 60 seconds (no keys needed)

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

wck doctor                       # show resolved config + safety checks
wck backtest --matches 200       # evaluate the strategy on synthetic matches
wck run --matches 3 --dashboard  # live paper-trading loop + web dashboard
#   -> open http://127.0.0.1:8000
pytest -q                        # 101 tests, fully offline
```

Everything above runs against a deterministic **match simulator** and a **simulated
Kalshi market**, so the whole pipeline — model → edge → sizing → risk → execution →
dashboard → settlement — works with zero credentials and zero network.

---

## What it does

```
 ┌─────────────────┐     ┌──────────────┐     ┌────────────────────────┐
 │  Football feed  │ ──▶ │ Feature store│ ──▶ │  In-play 1X2 model     │
 │ (sim / API-Ftbl)│     │ (xG, form…)  │     │  (Dixon–Coles)         │
 └─────────────────┘     └──────────────┘     └───────────┬────────────┘
 ┌─────────────────┐     ┌──────────────┐                 │
 │  Kalshi feed    │ ──▶ │ Market-implied│ ───────────────┤
 │ (sim / REST+WS) │     │ prob (de-vig) │                ▼
 └─────────────────┘     └──────────────┘        ┌────────────────┐
                                                 │  Edge detector │  edge = model − market,
                                                 └────────┬───────┘  net of fees+spread+slippage
                                                          ▼
                                            ┌────────────────────────┐
                                            │ Sizing (¼-Kelly) + Risk │  guardrails + kill switch
                                            └────────────┬────────────┘
                                                         ▼
                                       ┌──────────────────────────────────┐
                                       │ Execution: paper │ demo │ live    │  idempotent, audited
                                       └────────────┬─────────────────────┘
                                                    ▼
                                  ┌────────────────────────────────────────┐
                                  │ DB (SQLite→PG) · Dashboard · Alerts ·   │
                                  │ Audit log · Backtest/replay harness     │
                                  └────────────────────────────────────────┘
```

Each stage hides behind an interface and is independently tested and swappable.

---

## Run modes

| Mode | Football feed | Market data | Orders | Default? | How to enable |
|------|---------------|-------------|--------|----------|---------------|
| `paper` | simulator (or real) | **simulated** | simulated fills | ✅ **yes** | nothing — it's the default |
| `demo` | real provider | **Kalshi demo** | Kalshi sandbox (mock funds) | no | `WCK_MODE=demo` + Kalshi key |
| `live` | real provider | **Kalshi prod** | **REAL money** | no | see the gate below |

### The live-mode gate (non-negotiable)

`live` requires **all three** of:
1. `WCK_MODE=live`
2. `WCK_ALLOW_LIVE=true`
3. `execution.live_confirmed: true` in your config

Miss any one and **startup fails loudly**. This is enforced in `config.py::_enforce_safety`
and unit-tested (`tests/test_config.py`). The intended workflow is **paper → demo →
(only once calibrated & proven) live**.

---

## Decision paths (who pulls the trigger)

Orthogonal to the run mode (paper/demo/live), the engine supports two **decision modes**:

| Mode | What happens | Run it |
|------|--------------|--------|
| **advisory** | The engine sizes + risk-checks each edge and posts a **trade proposal** (thesis, edge, expected value, max loss) to the web dashboard. **You** click Approve or Reject. | `wck run --advisory --dashboard` |
| **autonomous** | The engine executes actionable edges itself, weighing the same risk/incentives, subject to every guardrail. | `wck run --autonomous` |

Both honour the identical sizing + guardrail + kill-switch plumbing — autonomous just skips
the human step. Proposals expire (`execution.proposal_ttl_seconds`) and refresh as the match
moves, and approval re-checks risk at execution time. The dashboard's **Pending decisions**
panel is the advisory cockpit; in autonomous mode that panel is hidden and fills stream into
the activity feed. Decision mode is independent of the account/mode, so you can run
advisory-on-demo, autonomous-on-demo, etc., and fund each account as you see fit.

## Configuration

Non-secret config lives in [`config/default.yaml`](config/default.yaml); override with
`config/local.yaml` (git-ignored) or `WCK_CONFIG=/path/to.yaml`. **Secrets come only
from the environment** — copy [`.env.example`](.env.example) to `.env`.

Key knobs:

| Section | Setting | Meaning |
|---------|---------|---------|
| `edge` | `min_edge`, `min_edge_after_costs` | raw / cost-adjusted edge thresholds (5% / 3%) |
| `risk` | `kelly_fraction` | fractional Kelly (default **0.25**, never 1.0) |
| `risk` | `max_position_per_market`, `max_exposure_per_match`, `max_total_open_exposure` | hard position/exposure caps |
| `risk` | `max_daily_loss` | breaching it **halts trading** |
| `kalshi` | `fee_coefficient` | Kalshi fee formula coefficient (default 0.07) |
| `model` | `live_xg_weight`, `red_card_xg_penalty` | how live xG and red cards move the model |

`wck doctor` prints the fully-resolved config and flags anything unsafe.

---

## The model (transparent baseline)

A **Dixon–Coles in-play** model of the **full-time (90′) result with draws** — the
resolvable event for a group-stage match market. Each tick it:

1. derives prior scoring rates from Elo + home designation,
2. projects *remaining* goals by blending the prior per-minute rate with the observed
   **live-xG** per-minute rate (live weight grows with minutes played — early xG is
   noisy, late xG is informative),
3. applies **red-card** and game-state multipliers,
4. convolves the remaining-goal Poissons (with the Dixon–Coles low-score correction)
   and adds the current score → `P(home/draw/away)`.

It implements `ProbabilityModel`, so it can be swapped for an ML model without touching
any downstream stage. xG is the edge: a model that *reads* in-play xG beats a market
that prices only Elo + score + time.

**Edge & costs.** `edge = model_prob − market_prob` (de-vigged). A signal is only
*actionable* when the **executable** edge (buy at ask / sell at bid) clears the
threshold **after** subtracting the Kalshi fee and assumed slippage. Sizing is
**fractional Kelly × a calibration factor** (an un-calibrated model is forced to size
down) and hard-capped by the guardrails.

---

## Risk & the kill switch

Always-on guardrails (`risk/guardrails.py`), checked independently of the sizer
(defence in depth):

- per-market max position, per-match max exposure, total open-exposure cap;
- **daily-loss / drawdown halt** — breaching it stops all new trading;
- **global kill switch** — one click in the dashboard, one `POST /api/kill`, or `Ctrl-C`,
  engages it and stops the loop.

Orders carry a **`client_order_id`** and executors cache by it, so a reconnect or retry
**never double-fires**. Every signal, decision, order, fill, settlement, and guardrail
trip is written to an append-only **audit log** (`data/audit.jsonl`) and the `decisions`
DB table, so any trade can be explained after the fact.

---

## Dashboard

A no-build web app (`dashboard/static/{index.html,styles.css,app.js,charts.js}`) served by
FastAPI; open `http://127.0.0.1:8000` after `wck run --dashboard`.

- **Live view** — active matches (model vs market 1X2, xG, red cards, edges), pending
  proposals with Approve/Reject + a size stepper, active bets (live unrealized P&L), and
  bet history (WON/LOST + realized P&L).
- **Performance analytics** — equity curve, session stats (win rate, best/worst, …), and a
  model-calibration reliability chart (Chart.js via CDN, with a graceful offline fallback).
- **Real-time** — server-sent events (`/api/stream`) push instant refreshes + toasts;
  a heartbeat shows liveness; polling is the fallback.
- **Match drill-down** — click a match for model-vs-market and cumulative-xG charts over the
  game, plus lineups / formations / injuries (API-Football).
- **Polish** — light/dark toggle, toast + optional sound alerts, one-click kill switch.
- **Notifications** — opt-in Discord / Telegram / email pushes for goals, red cards,
  proposals, fills, and guardrail trips (`config.alerts.*` + env secrets).

JSON API: `/api/state`, `/api/equity`, `/api/calibration`, `/api/matches/{id}/history`,
`/api/stream` (SSE), `/api/proposals` (+ `/{id}/approve|reject`), `/api/kill`.

---

## Backtest / replay

The harness runs the **same** `TickProcessor` the live loop uses, so *what you backtest
is what you run*.

```bash
wck backtest --matches 200 --seed 0 --json out.json   # synthetic, no keys
wck replay --db data/wck.sqlite3                       # re-evaluate a captured session
```

Representative synthetic result (200 matches, default config):

```
realized P&L:       +2243.32   (gross pre-fee +2922.65, fees -679.33)
ROI:                +224.33%   per-match t-stat 2.11
Brier:    0.5720   (uniform 1X2 ≈ 0.667)      ECE: 0.0183   (well calibrated)
LogLoss:  0.9633   (uniform ≈ 1.099)          Kelly calibration factor: 0.91
```

**Read this honestly.** P&L is **high-variance** — small samples (e.g. 40 matches) can be
near-breakeven or negative; the edge only shows up over many matches (t-stat ≈ 2 at
n=200). The headline ROI compounds the bankroll. The point of the exercise is the
**machinery and calibration**, not a money printer — and the in-play xG edge over
score+Elo is genuinely small, which is realistic. Calibration is strong (Brier and
log-loss beat the uniform baseline; reliability bins track empirical frequencies).

> ⚠️ The synthetic backtest is **intentionally circular**: the simulator plants a hidden
> per-team xG edge and the simulated market is blind to xG, so the model is *guaranteed*
> to win. `python scripts/backtest.py --sweep` sweeps the market's xG-awareness 0→1 and
> shows the edge collapse to ~0 as the counterparty sharpens. **Synthetic P&L is not
> evidence of real edge.** For that, use real data ↓.

### Real-data backtest (StatsBomb xG + Betfair prices)

`wck historical` replays a **real** per-tick xG/score/red-card timeline through the same
strategy. Build that timeline from free **StatsBomb open data**, and (optionally) attach
real **Betfair** exchange prices to measure edge against a real line:

```bash
# 1) Real calibration on the 2022 World Cup (64 matches; downloads + caches to data/statsbomb/)
wck statsbomb --competition 43 --season 106 --out data/wc2022.jsonl   # season 3 = 2018
wck historical --data data/wc2022.jsonl --stake-mode fixed

# 2) Real edge vs the exchange line (needs your own Betfair Advanced/PRO historical files)
wck statsbomb --competition 43 --season 106 --betfair data/betfair/wc2022/ --out data/wc2022_clv.jsonl
wck historical --data data/wc2022_clv.jsonl --stake-mode fixed
```

Representative **real** calibration (full 2022 WC, model reads real in-play xG — *not*
synthetic, *not* circular):

```
Brier:    0.4335   (uniform 1X2 ≈ 0.667)      ECE: 0.0536   (well calibrated)
LogLoss:  0.7767   (uniform ≈ 1.099)          reliability bins track the diagonal
```

**What each source proves — and does not:**

| Source | Proves | Does **not** prove |
|--------|--------|--------------------|
| StatsBomb only | real **calibration** (Brier / log-loss / ECE) on real WC outcomes | CLV, tradable edge, Kalshi profitability |
| StatsBomb + Betfair | real **edge vs the Betfair line** | literally "Kalshi CLV" (Betfair is an exchange, not Kalshi) |
| `wck record` (prod, 2026) | real **Kalshi** prices for replay | anything until WC match markets list on Kalshi prod |

Honesty notes baked into the tooling:

- **xG-only ⇒ no CLV.** Without `--betfair`, `wck historical` logs a warning and reports
  calibration only — there are no prices to trade against.
- **CLV is reported against three reference lines** because in-play CLV vs the *last* tick
  is degenerate near full time (prices collapse to 0/1). The **pre-off line** is the
  primary signal; `+5min` captures in-play drift; `vs last tick` is shown with a warning.
- **90′ regulation only.** We settle on the score after 90′ (+ stoppage), excluding extra
  time / penalties — matching Kalshi WC match-contract terms. A knockout level at 90′ thus
  settles **DRAW**, which is correct for a 90′ market.
- **Elo priors are 2026-era approximations.** Using them on 2018/2022 introduces a mild
  anachronism; pass `--elo-table teams.json` (a `{team: elo}` snapshot) for date-correct
  priors. `wck statsbomb` prints Elo coverage so you know what you got.
- **Data licensing.** StatsBomb open data is **CC BY-NC-SA** (attribution required); Betfair
  historical files are **user-supplied** under your own licence and need the **Advanced/PRO**
  tier for in-play prices. We parse them locally and **never** redistribute (both are
  git-ignored under `data/`).

### Record-forward (capture live data for replay)

`wck record` runs the pipeline **observe-only** (never trades, never reaches live execution)
and logs real xG + read-only Kalshi prices to a dedicated DB you can replay later:

```bash
wck record --duration 600 --out-db data/record.sqlite3   # demo prices (plumbing test)
wck record --source prod --out-db data/kalshi_wc.sqlite3  # read-only prod prices*
wck replay --db data/record.sqlite3
```

\* `--source demo` (default) records Kalshi **demo** prices — real plumbing, not real WC
liquidity. `--source prod` points the read-only feed at the prod base, but yields data only
once 2026 WC match markets actually list on Kalshi prod (currently `KXWORLDCUP` → 0).

---

## Project layout

```
src/wc_kalshi/
  config.py            loader + the triple-gated live mode
  logging_setup.py     structured JSON logging
  eventbus.py          async pub/sub (alerts, dashboard)
  fees.py              Kalshi fee formula
  util.py
  models/              schemas (MatchSnapshot, MarketSnapshot, …) + SQLAlchemy DB
  ingestion/
    http.py            shared retry/back-off (tenacity, honours Retry-After)
    football/          provider interface + simulator (default), API-Football, TheStatsAPI
    kalshi/            RSA-PSS auth, REST client, market mapping, sim market, market feed
  features/            feature engineering
  modeling/            ProbabilityModel + Dixon–Coles in-play + Poisson math + calibration
  market/              de-vigging (proportional / power / Shin)
  edge/                edge detector
  risk/                fractional-Kelly sizing + guardrails/kill switch
  execution/           paper / Kalshi executors, portfolio, audit
  engine/              wiring, per-tick processor + trade exec, async orchestrator, state
  observability/       alerter (console / webhook / Discord / Telegram / email)
  dashboard/           FastAPI app + static/ front-end (html/css/app.js/charts.js)
  backtest/            replay/synthetic harness + report
  cli.py               `wck` entrypoint
config/default.yaml    non-secret config (advisory.yaml / auto.yaml = the two paths)
docs/research.md       Phase-0 research (endpoints, auth, fees, providers) with citations
tests/                 101 offline tests + sample payloads
```

---

## Testing

```bash
pytest -q          # 101 tests, no network, no keys
```

Covers the model math, Poisson/Dixon–Coles, de-vigging, **edge calc, sizing, guardrail
logic** (spec-required), RSA-PSS signing, fees, portfolio settlement, executor
idempotency, provider/orderbook parsing, the backtest, and the dashboard + kill switch.

---

## Data providers

- **Football** — `simulated` (default, no key), `apifootball` (primary real), `thestatsapi`
  (fallback, live xG). The pure mapping functions are unit-tested against captured
  payloads. **Note:** API-Football's free tier is 100 req/day — a paid plan is required
  for continuous live polling (see [`docs/research.md`](docs/research.md) §2).
- **Kalshi** — REST + RSA-PSS signing implemented and tested; demo/prod base URLs in
  config; World Cup market tickers are **discovered at runtime** (`wck discover-markets`)
  rather than hard-coded.
- **Polymarket** — documented as an optional cross-market consensus signal (off by default).

All factual claims about the above are cited in [`docs/research.md`](docs/research.md).

---

## Status — what works / what's stubbed / next

**Works end-to-end (paper):** ingestion → features → model → de-vig → edge → ¼-Kelly
sizing → guardrails + kill switch → paper execution → portfolio settlement → DB persist
→ dashboard → alerts → backtest/replay → 101 passing tests.

**Implemented but only fully exercisable with credentials (`demo`/`live`):** Kalshi REST
client, RSA-PSS signing, runtime market discovery, and the Kalshi executor. These are
unit-tested at the parsing/signing layer offline; end-to-end demo needs a Kalshi demo key.

**Stubbed / simplified (documented):**
- Live **fill reconciliation** uses the create-order response; precise fills via the
  fills WebSocket/positions endpoint are the next step (`execution/kalshi_exec.py`).
- WebSocket market streaming is described; the live feed currently uses REST polling
  (rate-limit-friendly) with the WS channels mapped in research.
- Kill-switch **flatten** halts new trading + stops the loop; placing closing orders on
  `live` is a documented extension.
- The model covers the **90′ 1X2** result; knockout/extra-time/penalty markets need a
  different resolvable-event model.

See [`docs/research.md`](docs/research.md) for every assumption, and
[`RUNBOOK.md`](RUNBOOK.md) for operating it.

---

## Safety & terms

Defaults to simulation. Real-money execution is explicit, deliberate opt-in, always
behind the guardrails. Respect Kalshi's rules and every data provider's Terms of Service;
the HTTP layer backs off on rate limits. **No secrets in code or git** — they're read
only from the environment. This software is for research/education; it is **not** financial
advice. Trading event contracts carries risk of loss.
