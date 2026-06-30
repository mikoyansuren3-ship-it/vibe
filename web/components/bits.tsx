"use client";

import type { OutcomeKey } from "../lib/sim/types";

export const pct = (x: number | null | undefined) => (x == null ? "—" : `${(x * 100).toFixed(0)}%`);
export const money = (x: number) => `${x < 0 ? "−" : ""}$${Math.abs(x).toFixed(2)}`;
export const signed = (x: number, d = 4) => `${x >= 0 ? "+" : "−"}${Math.abs(x).toFixed(d)}`;
export const cls = (x: number | null) => (x == null ? "flat" : x > 0 ? "pos" : x < 0 ? "neg" : "flat");

export const OUT_COLOR: Record<OutcomeKey, string> = { home: "var(--home)", draw: "var(--draw)", away: "var(--away)" };

export function Tile({ k, v, c }: { k: string; v: string; c?: string }) {
  return (
    <div className="tile">
      <div className="k">{k}</div>
      <div className={`v ${c ?? ""}`}>{v}</div>
    </div>
  );
}

/** Stacked model (top) / market (bottom) probability bars + edge readout. N-way: the 1X2
 *  callers pass length-3 arrays; the knockout "to advance" headline passes length-2. */
export function DualBars({
  labels,
  model,
  market,
  colors,
  showEdge = true,
}: {
  labels: string[];
  model: number[];
  market: (number | null)[];
  colors?: string[];
  showEdge?: boolean;
}) {
  const grid = showEdge ? "64px 1fr 60px" : "64px 1fr";
  const palette = colors ?? [OUT_COLOR.home, OUT_COLOR.draw, OUT_COLOR.away];
  return (
    <div className="probs">
      <div className="probhead" style={{ gridTemplateColumns: grid }}>
        <span /><span>{showEdge ? "model ▸ market" : "the bot ▸ the market"}</span>{showEdge && <span style={{ textAlign: "right" }}>edge</span>}
      </div>
      {labels.map((label, i) => {
        const m = model[i];
        const mk = market[i];
        const edge = mk == null ? null : m - mk;
        const color = palette[i] ?? "var(--accent)";
        return (
          <div className="probrow" key={i} style={{ gridTemplateColumns: grid }}>
            <span className="tag" style={{ color }}>{label}</span>
            <div className="dualbar">
              <div className="model-fill" style={{ width: `${m * 100}%`, background: color }} />
              <div className="market-fill" style={{ width: `${(mk ?? 0) * 100}%` }} />
              <span className="rowlbl top">{pct(m)}</span>
              <span className="rowlbl bot">{pct(mk)}</span>
            </div>
            {showEdge && <span className={`edge ${cls(edge)}`}>{edge == null ? "—" : signed(edge, 2)}</span>}
          </div>
        );
      })}
    </div>
  );
}

/** Equity curve with baseline, goal markers, and a "now" dot. */
export function EquityChart({
  points,
  baseline,
  goals,
  now,
  totalTicks,
}: {
  points: number[];
  baseline: number;
  goals: { minute: number; team: string }[];
  now: number;
  totalTicks: number;
}) {
  const W = 320;
  const H = 130;
  const padY = 10;
  if (points.length < 2) return <svg className="chart" viewBox={`0 0 ${W} ${H}`} />;
  const allY = [baseline, ...points];
  const min = Math.min(...allY);
  const max = Math.max(...allY);
  const range = max - min || 1;
  const x = (i: number) => (i / (totalTicks - 1)) * W;
  const y = (v: number) => padY + (1 - (v - min) / range) * (H - 2 * padY);
  const path = points.map((p, i) => `${x(i)},${y(p)}`).join(" ");
  const last = points[points.length - 1];
  const up = last >= baseline;
  const baseY = y(baseline);
  const area = `0,${baseY} ${path} ${x(points.length - 1)},${baseY}`;
  return (
    <svg className="chart" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
      <defs>
        <linearGradient id="eq" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={up ? "rgba(63,185,80,0.25)" : "rgba(248,81,73,0.22)"} />
          <stop offset="100%" stopColor="rgba(0,0,0,0)" />
        </linearGradient>
      </defs>
      <line x1="0" y1={baseY} x2={W} y2={baseY} stroke="var(--border2)" strokeWidth="1" strokeDasharray="3 3" />
      <polygon points={area} fill="url(#eq)" />
      <polyline points={path} fill="none" stroke={up ? "var(--green)" : "var(--red)"} strokeWidth="1.6" vectorEffect="non-scaling-stroke" />
      {goals.map((g, i) => {
        const gx = (g.minute / 90) * W;
        return <line key={i} x1={gx} y1="0" x2={gx} y2={H} stroke="var(--yellow)" strokeWidth="0.7" strokeDasharray="2 3" opacity="0.5" />;
      })}
      <circle cx={x(points.length - 1)} cy={y(last)} r="3" fill={up ? "var(--green)" : "var(--red)"} stroke="var(--bg)" strokeWidth="1.5" />
    </svg>
  );
}

/** Signed horizontal CLV bar: red left of zero, green right. Domain ±0.30. */
export function SignedBar({ value, max = 0.3 }: { value: number; max?: number }) {
  const frac = Math.max(-1, Math.min(1, value / max));
  const w = Math.abs(frac) * 50;
  const pos = value >= 0;
  return (
    <div className="clvtrack">
      <div className="zero" />
      <div
        className="seg"
        style={{
          left: pos ? "50%" : `${50 - w}%`,
          width: `${w}%`,
          background: pos ? "var(--green)" : "var(--red)",
          opacity: 0.85,
        }}
      />
    </div>
  );
}
