// Per-contract edge evaluation for the live "all markets" board. A Kalshi binary
// (yes/no) market's implied YES probability is just its yes mid — no cross-outcome
// de-vig (that only applies to the 3-way 1X2). Cost/threshold logic mirrors
// policy.ts evaluateTick so the board's "would-bet" flag matches the real engine.

import { kalshiFee } from "./policy";
import type { Action, LiveContract, SimConfig } from "./types";

export interface ContractSignal {
  modelP: number | null;
  mid: number | null;
  edge: number | null; // model − mid (signed; + favors YES/back)
  action: Action | null; // buy (back YES) | sell (fade YES) | null
  netEdge: number | null; // edge after fee+slippage at the executable side
  wouldBet: boolean; // passes the engine's actionable thresholds
}

/** Evaluate one live contract: model price vs market, and whether the engine would bet it. */
export function evalContract(c: LiveContract, cfg: SimConfig): ContractSignal {
  const mid = c.mid;
  if (c.model == null || mid == null || c.bid == null || c.ask == null) {
    return { modelP: c.model, mid, edge: null, action: null, netEdge: null, wouldBet: false };
  }
  const modelP = c.model;
  const edge = modelP - mid;
  const ask = c.ask / 100;
  const bid = c.bid / 100;
  const slip = cfg.slippage_cents / 100;

  let action: Action | null = null;
  let execPrice = 0;
  let netEdge = 0;
  if (edge > 0) {
    action = "buy";
    execPrice = ask;
    netEdge = modelP - ask - kalshiFee(1, ask, cfg.fee_coefficient) - slip;
  } else if (edge < 0) {
    action = "sell";
    execPrice = bid;
    netEdge = bid - modelP - kalshiFee(1, bid, cfg.fee_coefficient) - slip;
  }

  const wouldBet =
    action != null &&
    netEdge >= cfg.min_edge_after_costs &&
    Math.abs(edge) >= cfg.min_edge &&
    execPrice >= cfg.min_price &&
    execPrice <= cfg.max_price;

  return { modelP, mid, edge, action, netEdge, wouldBet };
}
