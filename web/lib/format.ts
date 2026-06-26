// Plain-language helpers — used heavily in Basic mode to swap jargon for English.

import type { Bundle, DecisionCategory, OutcomeKey } from "./sim/types";

export function outcomeName(b: Bundle, o: OutcomeKey): string {
  return o === "home" ? b.home_team : o === "away" ? b.away_team : "Draw";
}

/** "back" = buy the outcome, "fade" = sell/bet against it. */
export const actionVerb = (action: string) => (action === "buy" ? "back" : "fade");

export const OUT_HEX: Record<OutcomeKey, string> = { home: "#5cb8ff", draw: "#c08fe0", away: "#ff9d57" };

export interface CategoryMeta { icon: string; title: string; desc: string; taken: boolean }

export const CATEGORY: Record<DecisionCategory, CategoryMeta> = {
  taken: { icon: "✅", title: "Taken", desc: "bets the bot actually placed", taken: true },
  cooldown: { icon: "⏳", title: "On cooldown", desc: "skipped — just bet this market, waiting before re-betting", taken: false },
  too_small: { icon: "⚖️", title: "Too small", desc: "edge there, but the sized bet rounded below 1 contract / hit a cap", taken: false },
  filtered: { icon: "🚫", title: "Filtered out", desc: "blocked by your active strategy filters", taken: false },
  passed_over: { icon: "↪️", title: "Passed over", desc: "actionable, but a stronger edge that moment won the one-bet-per-tick slot", taken: false },
};

export const CONSIDERED_ORDER: DecisionCategory[] = ["passed_over", "cooldown", "too_small", "filtered"];

/** Won/lost label for a settled taken bet. */
export function wonLabel(won: boolean | null): { text: string; cls: string } {
  if (won == null) return { text: "—", cls: "flat" };
  return won ? { text: "WON", cls: "pos" } : { text: "LOST", cls: "neg" };
}
