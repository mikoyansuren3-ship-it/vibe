// Run one match bundle through the betting policy and settle it. Mirrors the
// autonomous path of engine/match_loop.py (one trade per tick = the max |net edge|
// actionable signal) + portfolio settlement, per-match in isolation from a chosen
// bankroll — the natural semantics for "watch the algo bet this game".
//
// Sizing is off a ~constant bankroll (the portfolio equity model: nothing realizes
// until settlement), so fills are limited by the per-market position cap and the
// per-match exposure cap, exactly like risk/sizing.py — NOT by cash depletion.
//
// CLV is policy-determined and size-independent, so it reproduces the Python
// numbers exactly; P&L is this sim's own consistent isolated-bankroll model.

import { evaluateTick, type EdgeSig, kellyForTrade, signedClv, sizeContracts } from "./policy";
import type { Bundle, Decision, DecisionCategory, Fill, Filters, OutcomeKey, SimOptions, SimResult } from "./types";
import { NO_FILTERS } from "./types";

function outcomeWon(o: OutcomeKey, finalOutcome: string): boolean {
  return (
    (o === "home" && finalOutcome === "H") ||
    (o === "draw" && finalOutcome === "D") ||
    (o === "away" && finalOutcome === "A")
  );
}

function passesFilters(action: string, minute: number, f: Filters): boolean {
  if (f.sellOnly && action !== "sell") return false;
  if (f.disableBuys && action === "buy") return false;
  if (f.maxEntryMinute != null && minute > f.maxEntryMinute) return false;
  return true;
}

interface OpenPos {
  outcome: OutcomeKey;
  action: "buy" | "sell";
  contracts: number;
  entry: number; // prob units
}

export function runBundle(bundle: Bundle, opts: SimOptions = {}): SimResult {
  const cfg = { ...bundle.config };
  if (opts.bankroll != null) cfg.bankroll = opts.bankroll;
  if (opts.kellyFraction != null) cfg.kelly_fraction = opts.kellyFraction;
  const filters = opts.filters ?? NO_FILTERS;

  const bankroll = cfg.bankroll; // sizing reference (equity model, ~constant intra-match)
  let matchExposure = 0;
  const positionByTicker: Record<string, number> = {};
  const lastTradeMinute: Record<string, number> = {};
  const open: OpenPos[] = [];
  const lastMid: Partial<Record<OutcomeKey, number>> = {};
  const fills: Fill[] = [];
  const decisions: Decision[] = [];
  const lastKeyByTicker: Record<string, string> = {};
  const equityCurve: { minute: number; equity: number }[] = [];

  // Record a decision, collapsing consecutive identical skips per market into one
  // "opportunity" (a persistent 8-min cooldown = one entry, not ~80 ticks).
  const record = (s: EdgeSig, category: DecisionCategory, contracts: number, ti: number, minute: number) => {
    const ticker = bundle.tickers[s.outcome] ?? s.outcome;
    const key = `${s.outcome}|${s.action}|${category}`;
    if (category !== "taken" && lastKeyByTicker[ticker] === key) return;
    lastKeyByTicker[ticker] = key;
    const ref = bundle.preoff[s.outcome];
    decisions.push({
      tickIndex: ti, minute, outcome: s.outcome, action: s.action as "buy" | "sell",
      modelP: s.modelP, marketImplied: s.implied, netEdge: s.netEdge,
      execCents: Math.round((s.execPrice ?? 0) * 100),
      category, contracts,
      clvPreoff: ref != null && s.execPrice != null && s.action != null ? signedClv(s.action, s.execPrice, ref) : null,
      won: null,
    });
  };

  for (let ti = 0; ti < bundle.ticks.length; ti++) {
    const tick = bundle.ticks[ti];
    for (const o of ["home", "draw", "away"] as OutcomeKey[]) {
      const q = tick.markets[o];
      if (q && q[0] != null && q[1] != null) lastMid[o] = (q[0] + q[1]) / 200;
    }

    const actionableAll = evaluateTick(tick, cfg).filter((s) => s.actionable);
    const passed = actionableAll.filter((s) => passesFilters(s.action as string, tick.minute, filters));
    for (const s of actionableAll) if (!passed.includes(s)) record(s, "filtered", 0, ti, tick.minute);

    if (passed.length > 0) {
      // one trade per tick: the strongest net edge (match_loop.py:122); the rest are "passed over".
      const sig = passed.reduce((a, b) => (Math.abs(b.netEdge) > Math.abs(a.netEdge) ? b : a));
      for (const s of passed) if (s !== sig) record(s, "passed_over", 0, ti, tick.minute);

      const ticker = bundle.tickers[sig.outcome] ?? sig.outcome;
      const last = lastTradeMinute[ticker];
      const onCooldown = last != null && tick.minute - last < cfg.min_retrade_minutes;
      const existing = positionByTicker[ticker] ?? 0;
      const taper = lateTaper(tick.minute, cfg.late_taper_minutes, cfg.late_taper_floor);
      const contracts = onCooldown
        ? 0
        : sizeContracts(sig, bankroll, { ...cfg, kelly_factor: cfg.kelly_factor * taper }, existing, matchExposure);

      if (contracts > 0 && sig.execPrice != null && sig.action != null) {
        const { cost } = kellyForTrade(sig.modelP, sig.execPrice, sig.action);
        const ref = bundle.preoff[sig.outcome];
        fills.push({
          tickIndex: ti, minute: tick.minute, outcome: sig.outcome, action: sig.action,
          contracts, entryCents: Math.round(sig.execPrice * 100), cost,
          clvPreoff: ref != null ? signedClv(sig.action, sig.execPrice, ref) : null,
        });
        positionByTicker[ticker] = existing + contracts;
        lastTradeMinute[ticker] = tick.minute;
        matchExposure += contracts * cost;
        open.push({ outcome: sig.outcome, action: sig.action, contracts, entry: sig.execPrice });
        record(sig, "taken", contracts, ti, tick.minute);
      } else {
        record(sig, onCooldown ? "cooldown" : "too_small", 0, ti, tick.minute);
      }
    }
    equityCurve.push({ minute: tick.minute, equity: bankroll + unrealized(open, lastMid) });
  }

  // Settle every fill on the final 90' outcome.
  let pnl = 0;
  let clvSum = 0;
  let clvN = 0;
  for (const f of fills) {
    const p = f.entryCents / 100;
    const won = outcomeWon(f.outcome, bundle.outcome) ? 1 : 0;
    pnl += f.action === "buy" ? f.contracts * (won - p) : f.contracts * (p - won);
    if (f.clvPreoff != null) {
      clvSum += f.clvPreoff;
      clvN += 1;
    }
  }
  for (const d of decisions) if (d.category === "taken") d.won = outcomeWon(d.outcome, bundle.outcome);

  return {
    fills,
    decisions,
    pnl,
    bankrollEnd: bankroll + pnl,
    clvPreoff: clvN ? clvSum / clvN : null,
    clvN,
    equityCurve,
  };
}

/** Late-game taper: shrink size toward `floor` as minutes_remaining falls below
 *  `window` (match_loop._late_game_taper). 90' regulation => remaining = 90 - minute. */
function lateTaper(minute: number, window: number, floor: number): number {
  if (!window || window <= 0) return 1.0;
  const rem = Math.max(0, 90 - minute);
  if (rem >= window) return 1.0;
  return Math.max(floor, Math.min(1.0, rem / window));
}

/** Mark-to-market unrealized P&L of open positions at the latest mid per outcome. */
function unrealized(open: OpenPos[], lastMid: Partial<Record<OutcomeKey, number>>): number {
  let u = 0;
  for (const p of open) {
    const mid = lastMid[p.outcome];
    if (mid == null) continue;
    u += p.action === "buy" ? p.contracts * (mid - p.entry) : p.contracts * (p.entry - mid);
  }
  return u;
}

/** Aggregate a set of bundles under one config: summed P&L + pooled pre-off CLV. */
export function runMany(
  bundles: Bundle[],
  opts: SimOptions = {}
): { pnl: number; clvPreoff: number | null; clvN: number; nFills: number; perMatch: SimResult[] } {
  const perMatch = bundles.map((b) => runBundle(b, opts));
  let pnl = 0;
  let clvSum = 0;
  let clvN = 0;
  let nFills = 0;
  for (const r of perMatch) {
    pnl += r.pnl;
    nFills += r.fills.length;
    if (r.clvPreoff != null) {
      clvSum += r.clvPreoff * r.clvN;
      clvN += r.clvN;
    }
  }
  return { pnl, clvPreoff: clvN ? clvSum / clvN : null, clvN, nFills, perMatch };
}
