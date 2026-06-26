// Shapes of the per-match bundle (produced by `wck export-bundles`) and the
// client-side simulation. Kept in sync with src/wc_kalshi/backtest/export.py.

export type OutcomeKey = "home" | "draw" | "away";
export type FinalOutcome = "H" | "D" | "A";
export type Action = "buy" | "sell";

/** [bid, ask] in cents, either may be null when unquoted. */
export type Quote = [number | null, number | null];

export interface Tick {
  minute: number;
  period: string;
  score: [number, number];
  /** model probabilities [home, draw, away], normalized. */
  model: [number, number, number];
  markets: Partial<Record<OutcomeKey, Quote>>;
}

export interface GoldenFill {
  minute: number;
  outcome: OutcomeKey;
  action: Action;
  contracts: number;
  entry_cents: number;
}

/** Thresholds/sizer params mirrored from config.py so the TS policy matches Python. */
export interface SimConfig {
  min_edge: number;
  min_edge_after_costs: number;
  slippage_cents: number;
  fee_coefficient: number;
  maker_fraction: number;
  min_price: number;
  max_price: number;
  kelly_fraction: number;
  max_position_per_market: number;
  max_exposure_per_match: number;
  min_order_contracts: number;
  min_retrade_minutes: number;
  late_taper_minutes: number;
  late_taper_floor: number;
  bankroll: number;
  kelly_factor: number;
}

export interface Bundle {
  match_id: string;
  home_team: string;
  away_team: string;
  home_elo: number | null;
  away_elo: number | null;
  outcome: FinalOutcome;
  final_score: [number, number];
  tickers: Partial<Record<OutcomeKey, string>>;
  preoff: Partial<Record<OutcomeKey, number>>;
  n_ticks: number;
  ticks: Tick[];
  golden: { fills: GoldenFill[]; n_fills: number; pnl: number };
  config: SimConfig;
}

/** Strategy knobs the sandbox exposes (the levers the game review surfaced). */
export interface Filters {
  sellOnly: boolean;
  disableBuys: boolean;
  maxEntryMinute: number | null;
}

export interface SimOptions {
  bankroll?: number;
  kellyFraction?: number;
  filters?: Filters;
}

export interface Fill {
  tickIndex: number;
  minute: number;
  outcome: OutcomeKey;
  action: Action;
  contracts: number;
  entryCents: number;
  cost: number; // capital at risk per contract (prob units)
  clvPreoff: number | null;
}

/** Why a considered bet did or didn't become a fill. */
export type DecisionCategory = "taken" | "cooldown" | "too_small" | "filtered" | "passed_over";

/** One betting decision the algo reached (taken or considered-but-skipped). */
export interface Decision {
  tickIndex: number;
  minute: number;
  outcome: OutcomeKey;
  action: Action;
  modelP: number;
  marketImplied: number;
  netEdge: number;
  execCents: number;
  category: DecisionCategory;
  contracts: number; // >0 only when taken
  staked: number; // dollars at risk (0 for considered-but-not-taken)
  clvPreoff: number | null;
  won: boolean | null; // settled result for taken bets
  pnl: number | null; // settled profit/loss in dollars (taken bets only)
}

export interface SimResult {
  fills: Fill[];
  decisions: Decision[];
  pnl: number;
  bankrollEnd: number;
  clvPreoff: number | null;
  clvN: number;
  equityCurve: { minute: number; equity: number }[];
}

export const NO_FILTERS: Filters = { sellOnly: false, disableBuys: false, maxEntryMinute: null };
