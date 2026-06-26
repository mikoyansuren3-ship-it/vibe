"use client";

import { useMemo } from "react";
import { runMany } from "../lib/sim/engine";
import type { Bundle, Fill, Filters } from "../lib/sim/types";
import { goalMinutes } from "../lib/sim/util";
import { cls, money, SignedBar, signed } from "./bits";

const BUCKETS = ["early (≤15′)", "mid (16–69′)", "goal/card reaction", "late (≥70′)", "BUY / back", "SELL / fade"];

function bucketFills(bundles: Bundle[], perMatch: { fills: Fill[] }[]) {
  const acc: Record<string, { clv: number; n: number }> = {};
  for (const b of BUCKETS) acc[b] = { clv: 0, n: 0 };
  bundles.forEach((b, i) => {
    const goals = goalMinutes(b).map((g) => g.minute);
    for (const f of perMatch[i].fills) {
      if (f.clvPreoff == null) continue;
      const m = f.minute;
      const phase = goals.some((g) => m - g >= 0 && m - g <= 7)
        ? "goal/card reaction" : m <= 15 ? "early (≤15′)" : m >= 70 ? "late (≥70′)" : "mid (16–69′)";
      acc[phase].clv += f.clvPreoff; acc[phase].n += 1;
      const dir = f.action === "buy" ? "BUY / back" : "SELL / fade";
      acc[dir].clv += f.clvPreoff; acc[dir].n += 1;
    }
  });
  return acc;
}

function Delta({ now, base, d = 4, invertGood = false }: { now: number; base: number; d?: number; invertGood?: boolean }) {
  const diff = now - base;
  const good = invertGood ? diff < 0 : diff > 0;
  if (Math.abs(diff) < (d === 2 ? 0.005 : 0.0001)) return <span className="deltabadge" style={{ color: "var(--faint)", background: "var(--panel2)" }}>±0</span>;
  return (
    <span className="deltabadge" style={{ color: good ? "var(--green)" : "var(--red)", background: good ? "var(--green-dim)" : "var(--red-dim)" }}>
      {signed(diff, d)}
    </span>
  );
}

export function Sandbox({
  bundles, bankroll, setBankroll, kellyFraction, setKelly, filters, setFilters,
}: {
  bundles: Bundle[]; bankroll: number; setBankroll: (n: number) => void;
  kellyFraction: number; setKelly: (n: number) => void;
  filters: Filters; setFilters: (f: Filters) => void;
}) {
  const baseline = useMemo(() => runMany(bundles, { bankroll, kellyFraction }), [bundles, bankroll, kellyFraction]);
  const current = useMemo(() => runMany(bundles, { bankroll, kellyFraction, filters }), [bundles, bankroll, kellyFraction, filters]);
  const slice = useMemo(() => bucketFills(bundles, current.perMatch), [bundles, current]);
  const dirty = filters.sellOnly || filters.disableBuys || filters.maxEntryMinute != null;

  return (
    <div className="panel">
      <h2>Strategy sandbox</h2>
      <div className="note" style={{ marginTop: -4 }}>
        Re-runs the algo over all {bundles.length} games in your browser. Levers the game-review flagged: fades enter fair, backs &amp; late entries bleed.
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
        Sell / fade only <span style={{ color: "var(--faint)" }}>(disable backs)</span>
      </label>
      <label className="toggle">
        <input type="checkbox" checked={filters.maxEntryMinute != null} onChange={(e) => setFilters({ ...filters, maxEntryMinute: e.target.checked ? 70 : null })} />
        Cap entry minute{filters.maxEntryMinute != null ? <span className="mono" style={{ color: "var(--text)" }}>&nbsp;{filters.maxEntryMinute}′</span> : ""}
      </label>
      {filters.maxEntryMinute != null && (
        <input type="range" min={20} max={89} step={1} value={filters.maxEntryMinute} onChange={(e) => setFilters({ ...filters, maxEntryMinute: Number(e.target.value) })} />
      )}

      <div className="tiles" style={{ gridTemplateColumns: "1fr 1fr 1fr", marginTop: 16 }}>
        <div className="tile">
          <div className="k">Pooled CLV</div>
          <div className={`v ${cls(current.clvPreoff)}`}>{current.clvPreoff == null ? "—" : signed(current.clvPreoff, 4)}</div>
          {dirty && <div style={{ marginTop: 4 }}><Delta now={current.clvPreoff ?? 0} base={baseline.clvPreoff ?? 0} /></div>}
        </div>
        <div className="tile">
          <div className="k">Total P&L</div>
          <div className={`v ${cls(current.pnl)}`}>{money(current.pnl)}</div>
          {dirty && <div style={{ marginTop: 4 }}><Delta now={current.pnl} base={baseline.pnl} d={2} /></div>}
        </div>
        <div className="tile">
          <div className="k">Fills</div>
          <div className="v">{current.nFills}</div>
          {dirty && <div className="note" style={{ margin: "4px 0 0" }}>of {baseline.nFills}</div>}
        </div>
      </div>

      <h2 style={{ marginTop: 20 }}>CLV by trade type</h2>
      <div className="clvbars">
        {BUCKETS.map((k) => {
          const b = slice[k];
          const clv = b.n ? b.clv / b.n : null;
          return (
            <div className="clvbar" key={k}>
              <span className="lbl">{k} <span className="n">· {b.n}</span></span>
              {clv == null ? <div className="clvtrack"><div className="zero" /></div> : <SignedBar value={clv} />}
              <span className={`val ${cls(clv)}`}>{clv == null ? "—" : signed(clv, 3)}</span>
            </div>
          );
        })}
      </div>
      <div className="note">
        Pre-off CLV in probability units (right/green = beat the opening line; left/red = paid up). The honest takeaway: filters shrink the bleed toward ~0 but never manufacture a positive edge.
      </div>
    </div>
  );
}
