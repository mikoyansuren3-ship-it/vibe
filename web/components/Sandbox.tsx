"use client";

import { useMemo } from "react";
import { runMany } from "../lib/sim/engine";
import type { Bundle, Fill, Filters } from "../lib/sim/types";
import { money, signed } from "./bits";

function goalMinutes(b: Bundle): number[] {
  const gm: number[] = [];
  let ph = 0;
  let pa = 0;
  for (const t of b.ticks) {
    if (t.score[0] > ph || t.score[1] > pa) gm.push(t.minute);
    ph = t.score[0];
    pa = t.score[1];
  }
  return gm;
}

function bucketOf(f: Fill, goals: number[]): string {
  if (goals.some((g) => f.minute - g >= 0 && f.minute - g <= 7)) return "goal/card reaction";
  if (f.minute <= 15) return "early (≤15′)";
  if (f.minute >= 70) return "late (≥70′)";
  return "mid (16–69′)";
}

interface Bucket { clv: number; n: number; }

function emptyBuckets() {
  const order = ["early (≤15′)", "mid (16–69′)", "goal/card reaction", "late (≥70′)", "BUY", "SELL"];
  const m: Record<string, Bucket> = {};
  for (const k of order) m[k] = { clv: 0, n: 0 };
  return { order, m };
}

export function Sandbox({
  bundles,
  bankroll,
  setBankroll,
  kellyFraction,
  setKelly,
  filters,
  setFilters,
}: {
  bundles: Bundle[];
  bankroll: number;
  setBankroll: (n: number) => void;
  kellyFraction: number;
  setKelly: (n: number) => void;
  filters: Filters;
  setFilters: (f: Filters) => void;
}) {
  const baseline = useMemo(() => runMany(bundles, { bankroll, kellyFraction }), [bundles, bankroll, kellyFraction]);
  const current = useMemo(
    () => runMany(bundles, { bankroll, kellyFraction, filters }),
    [bundles, bankroll, kellyFraction, filters]
  );

  const slice = useMemo(() => {
    const { order, m } = emptyBuckets();
    bundles.forEach((b, i) => {
      const goals = goalMinutes(b);
      for (const f of current.perMatch[i].fills) {
        if (f.clvPreoff == null) continue;
        const phase = bucketOf(f, goals);
        m[phase].clv += f.clvPreoff;
        m[phase].n += 1;
        const dir = f.action === "buy" ? "BUY" : "SELL";
        m[dir].clv += f.clvPreoff;
        m[dir].n += 1;
      }
    });
    return { order, m };
  }, [bundles, current]);

  const clvCls = (x: number | null) => (x == null ? "" : x >= 0 ? "pos" : "neg");

  return (
    <div className="panel">
      <h2>Strategy sandbox</h2>
      <div className="note" style={{ marginTop: 0 }}>
        Re-runs the algo over all {bundles.length} recorded games in the browser. The levers are
        the ones the game-review flagged: fading reads ~0 CLV, backs and late entries bleed.
      </div>

      <div className="knob">
        <label><span>Bankroll / game</span><span className="mono">{money(bankroll)}</span></label>
        <input type="range" min={50} max={1000} step={50} value={bankroll} onChange={(e) => setBankroll(Number(e.target.value))} />
      </div>
      <div className="knob">
        <label><span>Kelly fraction</span><span className="mono">{kellyFraction.toFixed(2)}</span></label>
        <input type="range" min={0.05} max={0.5} step={0.05} value={kellyFraction} onChange={(e) => setKelly(Number(e.target.value))} />
      </div>

      <label className="toggle">
        <input type="checkbox" checked={filters.sellOnly} onChange={(e) => setFilters({ ...filters, sellOnly: e.target.checked, disableBuys: e.target.checked })} />
        Sell / fade only (disable backs)
      </label>
      <label className="toggle">
        <input
          type="checkbox"
          checked={filters.maxEntryMinute != null}
          onChange={(e) => setFilters({ ...filters, maxEntryMinute: e.target.checked ? 70 : null })}
        />
        Cap entry minute{filters.maxEntryMinute != null ? `: ${filters.maxEntryMinute}′` : ""}
      </label>
      {filters.maxEntryMinute != null && (
        <input type="range" min={20} max={89} step={1} value={filters.maxEntryMinute} onChange={(e) => setFilters({ ...filters, maxEntryMinute: Number(e.target.value) })} />
      )}

      <div className="stats-row" style={{ marginTop: 16, gridTemplateColumns: "1fr 1fr 1fr" }}>
        <div className="stat"><span className="label">Pooled CLV</span><span className={`val ${clvCls(current.clvPreoff)}`}>{current.clvPreoff == null ? "—" : signed(current.clvPreoff, 4)}</span></div>
        <div className="stat"><span className="label">Total P&L</span><span className={`val ${current.pnl >= 0 ? "pos" : "neg"}`}>{money(current.pnl)}</span></div>
        <div className="stat"><span className="label">Fills</span><span className="val">{current.nFills}</span></div>
      </div>
      <div className="note">
        baseline (all trades): CLV {signed(baseline.clvPreoff ?? 0, 4)} · {money(baseline.pnl)} · {baseline.nFills} fills
      </div>

      <h2 style={{ marginTop: 18 }}>CLV by trade type</h2>
      <table className="slice">
        <thead><tr><th>bucket</th><th className="num">CLV</th><th className="num">fills</th></tr></thead>
        <tbody>
          {slice.order.map((k) => {
            const b = slice.m[k];
            const clv = b.n ? b.clv / b.n : null;
            return (
              <tr key={k}>
                <td>{k}</td>
                <td className={`num ${clvCls(clv)}`}>{clv == null ? "—" : signed(clv, 4)}</td>
                <td className="num">{b.n}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="note">
        Pre-off CLV in probability units (positive = we beat the opening line). Negative = paying up.
        The honest result: filters reduce the bleed toward ~0 but don&apos;t manufacture a positive edge.
      </div>
    </div>
  );
}
