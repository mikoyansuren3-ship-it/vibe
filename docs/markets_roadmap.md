# WC market expansion roadmap

Kalshi lists **~99 World Cup series**. Today the engine trades exactly one: `KXWCGAME`
(in-play 1X2 match result, settled at 90′). This roadmap plans the rest, grouped by the
**shared infrastructure each group needs** (so we build once per tier, not once per market).

The natural extension is the **per-match in-play family** (Tiers 1–4, ~30 markets) — these
fit the existing in-play loop and recorder. Player props, tournament futures, and novelty
markets (Tiers 5–8) need different infrastructure and are separate tracks.

> Reusable across all tiers: `market/implied.py` (de-vig), `edge/` (edge + sizing + risk),
> `ingestion/kalshi/feed.py` (now parses the dollar/`_fp` schema), `backtest/replay.py`
> (paper sim + CLV), and the recorder. New work per market = a **market-map entry**
> (`ingestion/kalshi/market_map.py`) + an **outcome probability calculator** + a settlement
> rule. The de-vig/edge/sizing/execution plumbing is unchanged.

---

## Tier 0 — live today
- **`KXWCGAME`** — match 1X2 (home/draw/away), 90′. ✅ shipped.

---

## Tier 1 — derivable from the 90′ scoreline matrix (LOW effort, HIGH value)

These are **per-match** markets that resolve on the final 90′ scoreline (verified against the
API: `rules_primary` says "after 90 minutes plus stoppage time … not extra time or
penalties" — exactly our model's event). Every one is a sum over the joint scoreline matrix
the model already builds. **No new model, no new data.**

> ⚠️ Resolution check done 2026-06-23: four deceptively-named series are NOT per-match and
> were moved OUT of this tier → `KXWCTOTALGOAL` (full-tournament total), `KXWCTEAMGOALS`
> ("USA: Group Stage Goals"), `KXWCGOALSALLOWED` (group-stage clean sheets), `KXWCTEAMH2H`
> ("who advances further") → see Tiers 6/7. The confirmed per-match set:

| Series | Yes example | P(Yes) from the matrix `M[i,j]` |
|--------|-------------|-------------------------------|
| `KXWCTOTAL` | "Over 2.5 goals scored" | `Σ M[i,j] where i+j > strike` |
| `KXWCSPREAD` | "Argentina wins by more than 1.5" | `Σ M[i,j] where (i−j) > strike` (sign per team) |
| `KXWCBTTS` | "Both Teams To Score" | `Σ M[i,j] where i≥1 and j≥1` |
| `KXWCSCORE` | "Korea wins 2-0" | single cell `M[i,j]` (+ a "scores higher" bucket) |
| `KXWCTEAMTOTAL` | "Jordan over 1.5 goals" | marginal `Σ M[i,·] where i > strike` |
| `KXWCWINMARGIN` | exact margin (no open events yet) | score-diff dist `i−j` → bucket — **confirm strikes** |

### Implementation (concrete)

1. **`modeling/inplay.py` — expose the matrix.** The model already calls
   `remaining_goal_matrix(λ_rem, μ_rem, rho, max_goals)` then `outcome_probs(M, current_diff)`
   internally. Add a public method returning the **final** scoreline distribution:
   ```python
   def scoreline_matrix(self, match) -> np.ndarray:   # P(home_final=i, away_final=j)
       lam, mu = self._remaining_rates(match)
       M = remaining_goal_matrix(lam, mu, rho=self.cfg.draw_rho, max_goals=self.cfg.max_goals)
       return shift_by_score(M, match.home_score, match.away_score)  # pad+roll the axes
   ```
   `outcome_probs` already proves the collapse pattern; this just keeps the 2-D matrix.

2. **New `modeling/derived.py` — pure outcome calculators** (unit-tested on a known matrix):
   `prob_total_over(M, line)`, `prob_spread(M, side, line)`, `prob_btts(M)`,
   `prob_correct_score(M, i, j)`, `prob_team_total_over(M, side, line)`, `prob_margin(M, k)`.
   Each is a few lines of `np` masking over `M`.

3. **`ingestion/kalshi/market_map.py` — generalise.** Today it only classifies home/draw/away
   by label. Add a per-series classifier that reads the **structured strike fields**
   (`floor_strike`, `cap_strike`, `strike_type`) — no text parsing — and emits
   `(series, market_type, side, line)` per market ticker. The home/away "side" comes from the
   event's two team names (already resolved for `KXWCGAME`).

4. **Edge/sizing/risk/execution: unchanged.** Each market is still a Yes/No contract with a
   model prob vs a de-vigged price. The de-vig differs by market *structure*, though:
   - 1X2 / correct-score / first-team: mutually-exclusive multi-outcome → existing
     proportional/Shin de-vig across the set.
   - Total / spread / team-total / BTTS: **two-sided Yes/No** (Over vs Under) → de-vig is just
     the Yes/No overround on the pair. Add a 2-outcome de-vig path in `market/implied.py`.

5. **Settlement.** Generalise the resolver so each market type settles on the final 90′ score
   (Total→i+j, Spread→i−j, BTTS→min(i,j)≥1, Score→(i,j), TeamTotal→i or j). The
   settlement-capture work already provides the settled final tick.

**Prerequisite for validation (do first):** the recorder + `market_map` currently capture
ONLY `KXWCGAME`. To CLV-validate these on real data we must **record their prices too** — add
the Tier-1 series to the discovery/record list so `data/wc_tournament.sqlite3` carries them.
Until then we can compute model probs but have no market to score against.

**Effort:** ~2–3 days incl. the de-vig 2-outcome path + recorder capture + tests.
**Do this tier first** — it's the most market coverage per unit of new code.

---

## Tier 2 — per-half markets (needs a half-split of the scoring rate)

Same scoreline math as Tier 1, but applied to a **single half**. The one new piece is
projecting goals for a half instead of the full 90′.

| Series | Resolves on | Reuse |
|--------|-------------|-------|
| `KXWC1H` / `KXWC2H` | Half winner (1X2) | half matrix → `outcome_probs` |
| `KXWC1HTOTAL` / `KXWC2HTOTAL` | Half total over/under | `derived.prob_total_over` on half matrix |
| `KXWC1HSPREAD` / `KXWC2HSPREAD` | Half spread | `derived.prob_spread` on half matrix |
| `KXWC1HBTTS` / `KXWC2HBTTS` | Both score in the half | `derived.prob_btts` on half matrix |
| `KXWC1HSCORE` | 1st-half correct score | `derived.prob_correct_score` on half matrix |

### Implementation (concrete)

1. **Half-rate split.** Goals are not uniform across halves — empirically the **2nd half has
   more** (the WC StatsBomb data we already loaded has per-event minutes, so fit the real
   share `s1` = P(goal in 1H) rather than assume 45/55). Add config `first_half_goal_share`
   fitted via a small extension to `modeling/fit.py`.
2. **Half matrix.** For the **target half**, scale the per-minute rate over the minutes
   remaining *in that half* and build the matrix with `remaining_goal_matrix`. In-play:
   - 1st-half markets are live only while `period ∈ {1H}` (or pre-match); after HT they settle.
   - 2nd-half markets: before/at HT, project the whole 2nd half; in 2H, project the remainder.
   Goals already scored *in the current half* shift that half's matrix (reuse `shift_by_score`
   with the half's running goals, which we derive from the snapshot timeline).
3. **Everything else reuses Tier 1** — same `derived.py` calculators, same generalised
   `market_map` (new series + the structured strikes), same 2-outcome de-vig, same settlement
   keyed to the half's score at the half-whistle.

**Data note:** the half score must be reconstructable in-play. API-Football gives the running
score + `score.halftime`; StatsBomb gives exact event minutes (used to fit `s1` and to settle
1H markets in backtests).

**Effort:** ~2–3 days on top of Tier 1 (the split fit + half-window logic + settlement);
the market math is entirely reused.

---

## Tier 3 — goal-timing markets (needs a "which team scores next / when" model)

**Enabler:** a goal-timing layer — model the next-goal hazard per team (competing Poisson
processes from the per-minute rates the in-play model already has). Gives "which team scores
next" and goal-time distributions.

| Series | Resolves on |
|--------|-------------|
| `KXWCFTTS` / `KXWCTTSF` / `KXWCTEAMFIRSTGOAL` / `KXWCTEAM1STGOAL` | First/next team to score (home/away/none) |
| `KXWCFIRSTGOAL` | First goal timing / scorer-side | confirm exact resolution |

**Effort:** ~2–3 days. The competing-rates math is small; calibration against StatsBomb
shot timings is the work.

---

## Tier 4 — live non-goal stat markets (needs stat-rate models + the live stat feed)

**Data:** API-Football already returns live **shots / shots-on-goal / corners / saves** in
`/fixtures/statistics` (we parse some into `TeamStats` today). Model each as an in-play rate
(observed-so-far + prior projected over remaining minutes), same blend pattern as live-xG.

| Series | Resolves on | Data field |
|--------|-------------|-----------|
| `KXWCCORNERS` / `KXWCTCORNERS` | (Team) total corners over N.5 | corners |
| `KXWCSHOT` / `KXWCTEAMSHOT` | (Team) total shots over N.5 | total shots |
| `KXWCSOG` / `KXWCTEAMSOG` | (Team) shots on goal over N.5 | shots on goal |
| `KXWCSAVE` | Goalkeeper saves over N.5 | goalkeeper saves |

**Effort:** ~3–4 days (one generic "count-stat over/under" model fit per stat on real data;
need to confirm StatsBomb/API-Football give enough history to fit corner/shot rates).

---

## Tier 5 — player props (HARD — different granularity; separate track)

Needs **player-level** models + lineup/player-event data (who's on the pitch, per-player
shot/xG, historical scoring rate). Not derivable from team rates.

`KXWCPLAYERGOALS`, `KXWCGOALLEADER`, `KXWCTEAMLEADGOAL`, `KXWCAST`, `KXWCSOA`,
`KXWCHATTRICK`, `KXWCGOALCOMBO`, `KXWCMESSIMBAPPE`, `KXWCMESSIRONALDO`,
`KXWCMBAPPEGOALLEADER`, `KXWCGOLDENBOOTCLEAT`, `KXSOCCERPLAYMESSI`, `KXSOCCERPLAYCRON`,
`KXPLAYWC`.

**Effort:** large. Prereq: a player-data source (StatsBomb lineups + per-player shot history;
API-Football player stats). Defer.

---

## Tier 6 — tournament futures (different infra — Monte-Carlo bracket sim; pre-match, not in-play)

Needs a **tournament simulator**: run the 1X2 model over all remaining fixtures + knockout
bracket (with ET/penalty handling) thousands of times to get advancement/winner odds. This
is a *seasonal* model, not the in-play loop.

Winner: `KXMWORLDCUP`/`KXMENWORLDCUP`, `KXWC1STTIMEWIN`, `KXWCNOEURSA`, `KXWCCONTINENT`,
`KXWCFURTHESTADVANCING`, `KXWCREGIONKO`. Group: `KXWCGROUPWIN`/`KXWCGROUPWINNER`/
`KXWCWINGROUP`, `KXWCGROUPQUAL`, `KXWCGROUPORDER`, `KXWCGROUPPTS`, `KXWCGROUPBOTTOM`,
`KXWCGROUPWINELIM`. Stage: `KXWCROUND`, `KXWCSTAGE`, `KXWCSTAGEOFELIM`, `KXWC3RDPLACE(QUAL)`.
Host: `KXWCHOST*`, `KXWCBESTHOST`. Group-stage form: `KXWCGS3WINS`/`KXWCGSUNDEFEATED`/
`KXWCGSGOALS`/`KXWCGROUPGOALS`/`KXWCEVERYTEAMGOAL`/`KXWCGOALEVERYGAME`. Advancement /
clean-sheet (moved from Tier 1 after resolution check): `KXWCTEAMH2H` ("who advances
further"), `KXWCGOALSALLOWED` ("group-stage teams to not concede").

**Effort:** large but self-contained (Monte-Carlo over the bracket). Reuses the 1X2 model +
Elo. Highest *futures* value (the winner market has huge volume) but a distinct codebase.

---

## Tier 7 — tournament aggregate stats (cross-match accumulation)

Need a running tally across matches + a simulator for the remainder.
`KXWCTOTALGOAL`/`KXWCTEAMTOTALGOALS` (tournament goals), `KXWCTEAMGOALS` ("<team>: group-stage
goals", moved from Tier 1), `KXWCGOALCOUNT`, `KXWCGOALSTREAK`, `KXWCGAMEGOALS` (highest-scoring
game), `KXWCKOPENALTIES` (matches to penalties), `KXWCLONGESTPEN`. **Effort:** medium, depends
on Tier 6's simulator.

---

## Tier 8 — novelty / not cleanly modelable (skip or manual)

`KXWCFIRSTSONG`, `KXWCOLIMPICO` (olimpico goal), `KXWCGOALIEGOAL`/`KXWCGOALIEPEN`,
`KXWCFREEKICKGOAL`, `KXWCFASTESTGOAL`, `KXWCDELAY`, `KXWCLOCATION`, `KXWCAWARD`,
`KXWCFIFATOP10`, `KXWCSQUAD`, `KXWCCONGO`/`KXWCIRAN` (single-team specials). No edge to
model; ignore.

---

## Recommended sequencing
1. **Tier 1** (scoreline-derived) — highest value/effort ratio; the model already supports it.
2. **Tier 2** (halves) — reuses Tier 1 on a half-split.
3. **Tier 4** (corners/shots/SOG) — independent; data already arriving live.
4. **Tier 3** (goal timing).
5. **Tier 6** (tournament sim) — big but high futures volume; parallel track.
6. Tiers 5/7/8 last (player data dependency / niche / skip).

## Cross-cutting prerequisites (do alongside Tier 1)
- **Honesty first:** each new market must be CLV-validated on recorded real data before any
  real-money use — same bar as 1X2 (which currently shows *no* edge). More markets = more
  ways to lose into an efficient book, not automatically more edge.
- **Market-map generalization:** `market_map.py` currently classifies only home/draw/away;
  generalize it to parse Over/Under/Spread/score labels per series.
- **Multi-market resolution/settlement** in the recorder + replay so each series settles on
  its own rule (the settlement-capture work already handles match completion).
