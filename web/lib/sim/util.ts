import type { Bundle, OutcomeKey } from "./types";

/** Minutes at which a goal was scored (detected from score changes across ticks). */
export function goalMinutes(b: Bundle): { minute: number; team: "home" | "away" }[] {
  const out: { minute: number; team: "home" | "away" }[] = [];
  let ph = 0;
  let pa = 0;
  for (const t of b.ticks) {
    if (t.score[0] > ph) out.push({ minute: t.minute, team: "home" });
    if (t.score[1] > pa) out.push({ minute: t.minute, team: "away" });
    ph = t.score[0];
    pa = t.score[1];
  }
  return out;
}

export function outcomeWon(o: OutcomeKey, final: string): boolean {
  return (o === "home" && final === "H") || (o === "draw" && final === "D") || (o === "away" && final === "A");
}

export const FINAL_LABEL: Record<string, string> = { H: "Home win", D: "Draw", A: "Away win" };
