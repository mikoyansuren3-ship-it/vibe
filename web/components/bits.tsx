"use client";

import type { OutcomeKey } from "../lib/sim/types";

export const pct = (x: number | null | undefined) => (x == null ? "—" : `${(x * 100).toFixed(0)}%`);
export const money = (x: number) => `${x < 0 ? "-" : ""}$${Math.abs(x).toFixed(2)}`;
export const signed = (x: number, d = 4) => `${x >= 0 ? "+" : ""}${x.toFixed(d)}`;

const COLORS: Record<OutcomeKey, string> = { home: "var(--home)", draw: "var(--draw)", away: "var(--away)" };

export function Stat({ label, value, cls }: { label: string; value: string; cls?: string }) {
  return (
    <div className="stat">
      <span className="label">{label}</span>
      <span className={`val ${cls ?? ""}`}>{value}</span>
    </div>
  );
}

export function ProbBars({
  labels,
  model,
  market,
}: {
  labels: [string, string, string];
  model: [number, number, number];
  market: [number | null, number | null, number | null];
}) {
  const keys: OutcomeKey[] = ["home", "draw", "away"];
  return (
    <div>
      <div className="probrow" style={{ color: "var(--muted)", fontSize: 11 }}>
        <span />
        <span>model</span>
        <span>market (de-vig)</span>
      </div>
      {keys.map((k, i) => (
        <div className="probrow" key={k}>
          <span className="tag" style={{ color: COLORS[k] }}>{labels[i]}</span>
          <div className="bar model">
            <span style={{ width: `${model[i] * 100}%`, background: COLORS[k], opacity: 0.85 }} />
            <span className="lbl">{pct(model[i])}</span>
          </div>
          <div className="bar market">
            <span style={{ width: `${(market[i] ?? 0) * 100}%` }} />
            <span className="lbl">{pct(market[i])}</span>
          </div>
        </div>
      ))}
    </div>
  );
}

export function Sparkline({ points, baseline }: { points: number[]; baseline: number }) {
  if (points.length < 2) return <svg className="spark" />;
  const min = Math.min(baseline, ...points);
  const max = Math.max(baseline, ...points);
  const range = max - min || 1;
  const W = 100;
  const H = 48;
  const d = points
    .map((p, i) => `${(i / (points.length - 1)) * W},${H - ((p - min) / range) * H}`)
    .join(" ");
  const baseY = H - ((baseline - min) / range) * H;
  const last = points[points.length - 1];
  const color = last >= baseline ? "var(--green)" : "var(--red)";
  return (
    <svg className="spark" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
      <line x1="0" y1={baseY} x2={W} y2={baseY} stroke="var(--border)" strokeWidth="0.5" strokeDasharray="2 2" />
      <polyline points={d} fill="none" stroke={color} strokeWidth="1.2" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}
