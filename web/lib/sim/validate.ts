// Cross-language validation: the TS policy must reproduce the Python numbers.
//
// 1. CLV over the GOLDEN fills (Python's own fills) using our de-vig/CLV/preoff
//    must equal the canonical avg_clv_preoff — validates devig + CLV + preoff,
//    independent of our engine's sizing.
// 2. The engine, run at default config, must reproduce the golden fill SET
//    (minute, outcome, action, entry) — validates the edge/policy port.
// 3. Engine aggregate pre-off CLV ~= canonical (size-independent => exact).
//
// Run: npx tsx lib/sim/validate.ts   (from web/)

import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { runMany } from "./engine";
import { signedClv } from "./policy";
import type { Bundle, OutcomeKey } from "./types";

const DIR = join(import.meta.dirname, "../../public/bundles");
const manifest = JSON.parse(readFileSync(join(DIR, "manifest.json"), "utf8"));
const bundles: Bundle[] = readdirSync(DIR)
  .filter((f) => f.endsWith(".json") && f !== "manifest.json")
  .map((f) => JSON.parse(readFileSync(join(DIR, f), "utf8")));

const canonicalClv = manifest.aggregate.avg_clv_preoff as number;
const canonicalFills = manifest.aggregate.n_fills as number;

// --- 1. CLV over golden fills -------------------------------------------------
let gSum = 0;
let gN = 0;
for (const b of bundles) {
  for (const f of b.golden.fills) {
    const ref = b.preoff[f.outcome as OutcomeKey];
    if (ref == null) continue;
    gSum += signedClv(f.action, f.entry_cents / 100, ref);
    gN += 1;
  }
}
const goldenClv = gN ? gSum / gN : NaN;

// --- 2 & 3. engine reproduction ----------------------------------------------
const agg = runMany(bundles); // default config from each bundle

let matched = 0;
let goldenTotal = 0;
let engineExtra = 0;
for (const b of bundles) {
  const eng = runBundleKey(b);
  const gold = new Set(b.golden.fills.map((f) => `${f.minute}|${f.outcome}|${f.action}|${f.entry_cents}`));
  goldenTotal += gold.size;
  const engKeys = new Set(eng);
  for (const k of gold) if (engKeys.has(k)) matched++;
  for (const k of engKeys) if (!gold.has(k)) engineExtra++;
}

function runBundleKey(b: Bundle): string[] {
  // re-run a single bundle and key its fills like the golden set
  const r = runMany([b]);
  return r.perMatch[0].fills.map((f) => `${f.minute}|${f.outcome}|${f.action}|${f.entryCents}`);
}

const fmt = (x: number) => x.toFixed(4);
const ok = (cond: boolean) => (cond ? "PASS" : "FAIL");

// Pass bar (honest): CLV is policy-determined and must match EXACTLY; fill
// reproduction is ~exact but the engine uses the FROZEN production calibration
// factor while the original replay's factor evolved as matches settled, which
// flips a few marginal 1-contract fills — so we allow a small (<=5%) fill delta
// and a small aggregate-CLV tolerance. CLV exactness is the rigorous check.
const clvExact = Math.abs(goldenClv - canonicalClv) < 1e-3;
const fillRepro = matched / goldenTotal;
const aggClvClose = Math.abs((agg.clvPreoff ?? NaN) - canonicalClv) < 0.01;

console.log(`bundles: ${bundles.length}   canonical CLV ${fmt(canonicalClv)} over ${canonicalFills} fills\n`);
console.log(`[1] CLV over golden fills:  ${fmt(goldenClv)}  (n=${gN})   ${ok(clvExact)}  (must be exact)`);
console.log(`[2] engine reproduces golden fills: ${matched}/${goldenTotal} (${(100 * fillRepro).toFixed(1)}%), ${engineExtra} extra   ${ok(fillRepro >= 0.95)}  (>=95%; calibration-margin diff)`);
console.log(`[3] engine aggregate CLV:   ${fmt(agg.clvPreoff ?? NaN)}  (n=${agg.clvN})   ${ok(aggClvClose)}  (within 0.01 of canonical)`);
console.log(`    engine summed P&L (isolated $100/game): ${agg.pnl.toFixed(2)}`);

// --- 4. sandbox levers (the review's finding, as an interactive feature) ------
console.log("\nSandbox filters (pooled pre-off CLV across all games):");
const scenarios: { label: string; filters: { sellOnly: boolean; disableBuys: boolean; maxEntryMinute: number | null } }[] = [
  { label: "baseline (all trades)", filters: { sellOnly: false, disableBuys: false, maxEntryMinute: null } },
  { label: "disable buys", filters: { sellOnly: true, disableBuys: true, maxEntryMinute: null } },
  { label: "no late entries (>70')", filters: { sellOnly: false, disableBuys: false, maxEntryMinute: 70 } },
  { label: "sell-only + no late", filters: { sellOnly: true, disableBuys: true, maxEntryMinute: 70 } },
];
for (const s of scenarios) {
  const r = runMany(bundles, { filters: s.filters });
  console.log(
    `  ${s.label.padEnd(26)} CLV ${fmt(r.clvPreoff ?? NaN).padStart(8)}  (${r.clvN} fills)  P&L ${r.pnl.toFixed(2)}`
  );
}

const pass = clvExact && fillRepro >= 0.95 && aggClvClose;
console.log(`\n${pass ? "VALIDATION PASSED" : "VALIDATION FAILED"}`);
process.exit(pass ? 0 : 1);
