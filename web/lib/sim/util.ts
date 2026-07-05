import type { Bundle, OutcomeKey } from "./types";

/** Goals scored (detected from score changes across ticks), each carrying the TICK INDEX at
 *  which it appeared — markers are positioned by tick index to align with the tick-indexed
 *  playhead, not by minute/90 (which drifts on a mid-join recorder and pushes ET goals off). */
export function goalMinutes(
  b: Bundle,
): { minute: number; team: "home" | "away"; tickIndex: number }[] {
  const out: { minute: number; team: "home" | "away"; tickIndex: number }[] = [];
  let ph = 0;
  let pa = 0;
  b.ticks.forEach((t, ti) => {
    if (t.score[0] > ph) out.push({ minute: t.minute, team: "home", tickIndex: ti });
    if (t.score[1] > pa) out.push({ minute: t.minute, team: "away", tickIndex: ti });
    ph = t.score[0];
    pa = t.score[1];
  });
  return out;
}

export function outcomeWon(o: OutcomeKey, final: string): boolean {
  return (o === "home" && final === "H") || (o === "draw" && final === "D") || (o === "away" && final === "A");
}

export const FINAL_LABEL: Record<string, string> = { H: "Home win", D: "Draw", A: "Away win" };
