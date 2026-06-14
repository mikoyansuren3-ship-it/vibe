# RUNBOOK — operating the In-Play Edge Engine

Operational guide: how to start/stop, the kill switch, what the guardrails do, and how
to (carefully) move from paper → demo → live.

---

## 0. Prerequisites

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
wck doctor          # sanity-check the resolved config + mode
```

`wck doctor` should report `mode: paper` and `kalshi creds set: False` on a fresh setup.

---

## 1. Start / stop

### Paper (default, safe)
```bash
wck run --matches 3 --dashboard        # simulated matches + dashboard on :8000
wck run --matches 3 --no-trade         # observe only — model vs market, no orders
wck run --duration 120                 # auto-stop after 120s
```
Stop with **Ctrl-C** (graceful: stops the loop, prints a summary, closes the DB).

### Backtest (no keys)
```bash
wck backtest --matches 200 --seed 0 --json result.json
```

### Replay a captured session
```bash
wck replay --db data/wck.sqlite3
```

### Dashboard
`wck run --dashboard` serves `http://127.0.0.1:8000`:
live match cards (model vs market 1X2, xG, red cards, edges), risk panel, open
positions, P&L, a decisions/alerts feed, and the **KILL SWITCH** button.

---

## 2. The kill switch (flatten / stop everything)

Three equivalent ways to engage it — all halt **all new trading** immediately:

1. **Dashboard** — click the red **■ KILL SWITCH** button (asks for confirmation).
2. **API** — `curl -X POST http://127.0.0.1:8000/api/kill`
3. **Signal** — `Ctrl-C` (SIGINT) or `kill -TERM <pid>` stops the orchestrator loop.

Once engaged, `risk.trading_allowed` is `False`, `pre_trade_check` rejects every order,
and the loop winds down. (Engaging the switch is logged to the audit trail.)
> Note: on `live`, the switch **stops new trading**; placing closing orders to fully
> flatten inventory is a documented extension — for now, flatten manually in the Kalshi
> UI if needed.

---

## 3. Guardrails — what trips, and what happens

| Guardrail | Config | Behaviour when breached |
|-----------|--------|--------------------------|
| Per-market max position | `risk.max_position_per_market` | order **clamped** to fit |
| Per-match max exposure | `risk.max_exposure_per_match` | order **clamped** to fit |
| Total open exposure | `risk.max_total_open_exposure` | order **clamped** to fit |
| Daily loss / drawdown | `risk.max_daily_loss` | **HALT** — all new trading stops for the day |
| Price band | `risk.min_price` / `max_price` | order **rejected** (illiquid tails) |
| Kill switch | manual | **HALT** + loop stops |

A halt is logged (`GUARDRAIL TRIPPED`), audited, and pushed to the dashboard + alerter.
A halt clears on the next UTC day rollover **unless** the kill switch is engaged.

To verify the halt works, run a small bankroll / tight limit:
```bash
# tighten max_daily_loss in config/local.yaml, then:
wck run --matches 3
# expect: {"msg":"GUARDRAIL TRIPPED","reason":"daily loss limit hit ..."}
```

---

## 4. Alerts

Priority events (goal, red card, model/market divergence, guardrail trip) log to the
console and, if `alerts.webhook: true` + `ALERTS_WEBHOOK_URL` is set, POST to your
webhook. Toggle per-type in `config.alerts`.

---

## 5. Where the data lives

| Artifact | Path | Notes |
|----------|------|-------|
| Database | `data/wck.sqlite3` | append-only snapshots/probs/edges/orders/fills/decisions |
| Audit log | `data/audit.jsonl` | one JSON object per signal/decision/order/fill/alert |
| Logs | stderr (JSON) | structured; pipe to your collector |

Everything is UTC-timestamped and **append-only** so a run is fully replayable.

---

## 6. Going to DEMO (Kalshi sandbox, mock funds)

1. **Make a demo account** at <https://demo.kalshi.co> (separate from production — use
   mock name/address/SSN; just use a real, accessible email). Demo and prod keys are
   **not** interchangeable.
2. **Create the key** at **Profile → Settings → API Keys → "Create New API Key"** (while
   logged into the *demo* site). You're shown a **Key ID** and a **private key** (RSA PEM)
   **once** — it cannot be retrieved again.
   - Save the PEM to a file **outside the repo**, e.g. `~/.kalshi/demo.pem`, then
     `chmod 600 ~/.kalshi/demo.pem`.
   - (The `Create/Generate API Key` *REST* endpoints require an existing key to call, so
     bootstrap your **first** key through this web UI, not the API.)
3. In `.env`:
   ```
   WCK_MODE=demo
   KALSHI_API_KEY_ID=<the Key ID>
   KALSHI_PRIVATE_KEY_PATH=/Users/you/.kalshi/demo.pem
   ```
4. `wck doctor` → confirm `mode: demo`, `kalshi creds set: True`.
5. Map markets: `wck discover-markets` (lists World Cup events/markets for the series).
6. `wck run --dashboard` — orders now hit the Kalshi **demo** environment.

Validate the **whole** path in demo (orders accepted, fills booked, P&L sane) before even
considering live.

---

## 7. Going to LIVE (real money) — checklist

> Do **not** do this until the model is demonstrably calibrated (good Brier/ECE over many
> completed matches) and the demo path is proven end-to-end.

All of these are required or startup fails:
- [ ] Model calibration verified (`wck backtest` + live demo reliability looks good).
- [ ] Demo execution proven end-to-end (orders → fills → settlement reconcile).
- [ ] Position/exposure/daily-loss limits set conservatively for real capital.
- [ ] `.env`: `WCK_MODE=live` **and** `WCK_ALLOW_LIVE=true`.
- [ ] `config/local.yaml`: `execution.live_confirmed: true`.
- [ ] You understand fees and that contracts settle at $1.00 / $0.00.

Then `wck doctor` should show `mode: live`. Start small. Keep the dashboard open and a
hand on the kill switch.

---

## 8. Incident response

- **Runaway / weird behaviour** → engage the kill switch (§2). Then inspect
  `data/audit.jsonl` (tail it) and the `decisions` table.
- **Rate-limited (429)** → the HTTP layer backs off automatically (honours `Retry-After`);
  if persistent, increase `kalshi.poll_interval_seconds`.
- **Market won't map** → `wck discover-markets --series <TICKER>`; set
  `kalshi.worldcup_series_ticker` to the confirmed series.
- **Daily-loss halt** → expected protective behaviour; review the day's decisions before
  resuming (it auto-clears next UTC day, kill switch aside).
```
tail -f data/audit.jsonl | jq .          # live audit stream
```
