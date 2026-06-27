#!/usr/bin/env python3
"""Does the in-play model beat Kalshi on scoreline-derived markets?

For the matches with captured raw_market_quotes, walks each tick, prices every
Total / BTTS / Spread / TeamTotal contract from the model's scoreline matrix
(modeling/derived.py), and compares to the real Kalshi mid. Reports, per market
type: model Brier vs market Brier (lower = more accurate) and pre-off CLV (does a
model-edge bet enter better than the opening line). The honest Tier-1 edge check.

Usage: python scripts/eval_derived_markets.py [--db data/wc_tournament.sqlite3]
"""
from __future__ import annotations

import argparse
import collections
import sqlite3

from wc_kalshi.config import load_config
from wc_kalshi.models.db import Database
from wc_kalshi.modeling.derived import (
    prob_btts, prob_spread, prob_team_total_over, prob_total_over,
)
from wc_kalshi.modeling.inplay import DixonColesInplayModel

EDGE_THRESH = 0.05  # min |model - mid| to count as a "bet" for CLV


def model_prob(series: str, M, strike, sub_title: str, home: str, away: str):
    """Model P(yes) for one contract, or None if we don't price this series."""
    side = "home" if home and home in (sub_title or "") else "away" if away and away in (sub_title or "") else None
    if series == "KXWCTOTAL" and strike is not None:
        return prob_total_over(M, strike)
    if series == "KXWCBTTS":
        return prob_btts(M)
    if series == "KXWCSPREAD" and strike is not None and side:
        return prob_spread(M, side, strike)
    if series == "KXWCTEAMTOTAL" and strike is not None and side:
        return prob_team_total_over(M, side, strike)
    return None


def settles_yes(series: str, strike, sub_title: str, home: str, away: str, hs: int, as_: int):
    side_goals = hs if (home and home in (sub_title or "")) else as_
    opp_goals = as_ if (home and home in (sub_title or "")) else hs
    if series == "KXWCTOTAL":
        return (hs + as_) > strike
    if series == "KXWCBTTS":
        return hs > 0 and as_ > 0
    if series == "KXWCSPREAD":
        return (side_goals - opp_goals) > strike
    if series == "KXWCTEAMTOTAL":
        return side_goals > strike
    return None


TYPES = {"KXWCTOTAL": "Total O/U", "KXWCBTTS": "BTTS", "KXWCSPREAD": "Spread", "KXWCTEAMTOTAL": "Team total"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/wc_tournament.sqlite3")
    args = ap.parse_args()
    cfg = load_config(use_local=True)
    model = DixonColesInplayModel(cfg.model)
    raw = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    db = Database(f"sqlite:///{args.db}")

    match_ids = [r[0] for r in raw.execute("SELECT DISTINCT match_id FROM raw_market_quotes").fetchall()]
    # accumulators per type: model/market squared error, n; clv list
    acc = {s: {"mse_model": 0.0, "mse_mkt": 0.0, "n": 0, "clv": []} for s in TYPES}
    # CLEAN test: at kickoff only (one point per contract) — no late-game convergence
    # or 60s-quote-lag confound. model opening prob vs market opening line vs outcome.
    opening = {s: {"mse_model": 0.0, "mse_mkt": 0.0, "n": 0} for s in TYPES}

    for mid in match_ids:
        snaps = db.iter_match_snapshots(mid)
        settled = [s for s in snaps if s.period.is_finished]
        if not settled:
            continue
        last = settled[-1]
        hs, as_ = last.home_score, last.away_score
        home, away = snaps[0].home_team, snaps[0].away_team

        # quotes for this match, grouped by ticker, ordered by ts
        rows = raw.execute(
            "SELECT series, market_ticker, ts, yes_bid, yes_ask, floor_strike, yes_sub_title "
            "FROM raw_market_quotes WHERE match_id=? AND yes_bid IS NOT NULL AND yes_ask IS NOT NULL "
            "ORDER BY ts", (mid,)).fetchall()
        by_ticker: dict[str, list] = collections.defaultdict(list)
        meta: dict[str, tuple] = {}
        for series, tk, ts, yb, ya, strike, sub in rows:
            if series not in TYPES:
                continue
            by_ticker[tk].append((ts, (yb + ya) / 200.0))
            meta[tk] = (series, strike, sub)
        preoff = {tk: q[0][1] for tk, q in by_ticker.items()}

        # --- clean opening-line test (one point per contract, at kickoff) ---
        opener = next((s for s in snaps if s.period.is_live), None)
        if opener is not None:
            M0 = model.scoreline_matrix(opener)
            for tk, quotes in by_ticker.items():
                series, strike, sub = meta[tk]
                mp = model_prob(series, M0, strike, sub, home, away)
                won = settles_yes(series, strike, sub, home, away, hs, as_)
                if mp is None or won is None:
                    continue
                y = 1.0 if won else 0.0
                o = opening[series]
                o["mse_model"] += (mp - y) ** 2
                o["mse_mkt"] += (preoff[tk] - y) ** 2
                o["n"] += 1

        for snap in snaps:
            if not snap.period.is_live:
                continue
            M = model.scoreline_matrix(snap)
            tts = snap.ts.isoformat()
            for tk, quotes in by_ticker.items():
                series, strike, sub = meta[tk]
                mid_now = None
                for qts, qmid in quotes:  # carry-forward latest quote <= now
                    if qts <= tts:
                        mid_now = qmid
                    else:
                        break
                if mid_now is None:
                    continue
                mp = model_prob(series, M, strike, sub, home, away)
                won = settles_yes(series, strike, sub, home, away, hs, as_)
                if mp is None or won is None:
                    continue
                y = 1.0 if won else 0.0
                a = acc[series]
                a["mse_model"] += (mp - y) ** 2
                a["mse_mkt"] += (mid_now - y) ** 2
                a["n"] += 1
                if abs(mp - mid_now) >= EDGE_THRESH:  # model would bet this leg
                    entry = mid_now
                    ref = preoff[tk]
                    a["clv"].append((ref - entry) if mp > mid_now else (entry - ref))

    print(f"matches: {len(match_ids)}   (edge bet threshold |model−mid| ≥ {EDGE_THRESH})\n")
    print(f"{'market':12} {'Brier model':>12} {'Brier mkt':>11} {'Δ(mkt−mdl)':>11} {'pre-off CLV':>12} {'n / nbet':>12}")
    print("-" * 74)
    for s, label in TYPES.items():
        a = acc[s]
        if a["n"] == 0:
            continue
        bm, bk = a["mse_model"] / a["n"], a["mse_mkt"] / a["n"]
        clv = sum(a["clv"]) / len(a["clv"]) if a["clv"] else float("nan")
        print(f"{label:12} {bm:12.4f} {bk:11.4f} {bk - bm:+11.4f} {clv:+12.4f} {a['n']:>7}/{len(a['clv']):<5}")
    print("\n>>> CLEAN TEST — opening line only (one point per contract at kickoff,")
    print("    no late-game convergence / 60s-quote-lag confound):")
    print(f"{'market':12} {'Brier model':>12} {'Brier mkt':>11} {'Δ(mkt−mdl)':>11} {'contracts':>10}")
    print("-" * 60)
    for s, label in TYPES.items():
        o = opening[s]
        if o["n"] == 0:
            continue
        bm, bk = o["mse_model"] / o["n"], o["mse_mkt"] / o["n"]
        print(f"{label:12} {bm:12.4f} {bk:11.4f} {bk - bm:+11.4f} {o['n']:>10}")
    print("\nΔ>0 = model's OPENING read beats the market's OPENING line (honest predictive "
          "edge). The all-tick table above is confounded; trust this one.")


if __name__ == "__main__":
    main()
