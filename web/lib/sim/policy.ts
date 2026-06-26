// Faithful TypeScript port of the Python betting policy — the parts that decide
// WHETHER and HOW to bet. Cross-checked against:
//   - edge/detector.py  (EdgeDetector._evaluate_one)
//   - risk/sizing.py    (kelly_fraction_for_trade, PositionSizer.size)
//   - market/implied.py (_devig_proportional)
//   - fees.py           (kalshi_fee)
// The heavy Dixon-Coles model output is precomputed in the bundle, so this module
// is pure arithmetic — the whole reason the simulator can run client-side.

import type { Action, OutcomeKey, SimConfig, Tick } from "./types";

const MODEL_IDX: Record<OutcomeKey, number> = { home: 0, draw: 1, away: 2 };
const OUTCOMES: OutcomeKey[] = ["home", "draw", "away"];

function clamp(x: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, x));
}

/** Kalshi fee in dollars for one order (ceil to the cent, matching fees.py). */
export function kalshiFee(contracts: number, price: number, coefficient: number): number {
  if (contracts <= 0) return 0;
  const p = clamp(price, 0, 1);
  const raw = coefficient * contracts * p * (1 - p);
  // round before ceil to kill float noise, exactly like the Python.
  return Math.ceil(Math.round(raw * 100 * 1e9) / 1e9) / 100;
}

/** Proportional de-vig: each mid divided by their sum (market/implied.py default). */
export function devigProportional(mids: number[]): number[] {
  const total = mids.reduce((a, b) => a + b, 0);
  return total > 0 ? mids.map((m) => m / total) : mids;
}

export interface EdgeSig {
  outcome: OutcomeKey;
  modelP: number;
  implied: number;
  rawEdge: number;
  action: Action | null;
  execPrice: number | null; // prob units (0..1)
  netEdge: number;
  actionable: boolean;
}

/** Evaluate every quoted outcome at one tick into edge signals (mirrors detector.py). */
export function evaluateTick(tick: Tick, cfg: SimConfig): EdgeSig[] {
  const present = OUTCOMES.filter((o) => {
    const q = tick.markets[o];
    return q != null && q[0] != null && q[1] != null;
  });
  const mids = present.map((o) => {
    const [b, a] = tick.markets[o] as [number, number];
    return (b + a) / 200;
  });
  const implied = devigProportional(mids);

  return present.map((o, i) => {
    const modelP = tick.model[MODEL_IDX[o]];
    const imp = implied[i];
    const [bidC, askC] = tick.markets[o] as [number, number];
    const ask = askC / 100;
    const bid = bidC / 100;
    const rawEdge = modelP - imp;
    const slip = cfg.slippage_cents / 100;

    let action: Action | null = null;
    let execPrice: number | null = null;
    let netEdge = 0;
    if (rawEdge > 0) {
      const fee = kalshiFee(1, ask, cfg.fee_coefficient);
      netEdge = modelP - ask - fee - slip;
      action = "buy";
      execPrice = ask;
    } else if (rawEdge < 0) {
      const fee = kalshiFee(1, bid, cfg.fee_coefficient);
      netEdge = bid - modelP - fee - slip;
      action = "sell";
      execPrice = bid;
    }

    const gross = Math.abs(rawEdge);
    const actionable =
      action != null &&
      execPrice != null &&
      netEdge >= cfg.min_edge_after_costs &&
      gross >= cfg.min_edge &&
      execPrice >= cfg.min_price &&
      execPrice <= cfg.max_price;

    return { outcome: o, modelP, implied: imp, rawEdge, action, execPrice, netEdge, actionable };
  });
}

/** Full-Kelly fraction + capital-at-risk per contract (risk/sizing.py). */
export function kellyForTrade(
  modelPYes: number,
  execPrice: number,
  action: Action
): { kelly: number; cost: number } {
  const p = clamp(execPrice, 1e-4, 1 - 1e-4);
  if (action === "buy") return { kelly: clamp((modelPYes - p) / (1 - p), 0, 1), cost: p };
  return { kelly: clamp((p - modelPYes) / p, 0, 1), cost: 1 - p }; // sell yes == buy no
}

/** Contracts to trade after fractional-Kelly sizing + per-market/per-match caps. */
export function sizeContracts(
  sig: EdgeSig,
  bankroll: number,
  cfg: SimConfig,
  existingContracts: number,
  matchExposure: number
): number {
  if (sig.action == null || sig.execPrice == null) return 0;
  const { kelly, cost } = kellyForTrade(sig.modelP, sig.execPrice, sig.action);
  const sized = kelly * cfg.kelly_fraction * clamp(cfg.kelly_factor, 0, 1);
  const dollars = Math.max(0, sized * bankroll);
  if (cost <= 0) return 0;
  let contracts = Math.floor(dollars / cost);
  const roomMarket = cfg.max_position_per_market - Math.abs(existingContracts);
  contracts = Math.min(contracts, Math.max(0, roomMarket));
  const roomExposure = cfg.max_exposure_per_match - matchExposure;
  contracts = Math.min(contracts, Math.floor(Math.max(0, roomExposure) / cost));
  return contracts < cfg.min_order_contracts ? 0 : contracts;
}

/** Signed closing-line value vs a reference mid (replay.py `_signed_clv`). */
export function signedClv(action: Action, entry: number, reference: number): number {
  return action === "buy" ? reference - entry : entry - reference;
}
