"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { runBundle } from "../lib/sim/engine";
import { devigProportional } from "../lib/sim/policy";
import type { Bundle, Filters, OutcomeKey } from "../lib/sim/types";
import { money, ProbBars, signed, Sparkline, Stat } from "./bits";

const SPEEDS = [1, 4, 16, 64];

function marketImplied(tick: Bundle["ticks"][number]): [number | null, number | null, number | null] {
  const keys: OutcomeKey[] = ["home", "draw", "away"];
  const present = keys.filter((k) => {
    const q = tick.markets[k];
    return q && q[0] != null && q[1] != null;
  });
  const mids = present.map((k) => {
    const [b, a] = tick.markets[k] as [number, number];
    return (b + a) / 200;
  });
  const dev = devigProportional(mids);
  const out: Record<string, number> = {};
  present.forEach((k, i) => (out[k] = dev[i]));
  return [out.home ?? null, out.draw ?? null, out.away ?? null];
}

export function Terminal({
  bundle,
  bankroll,
  kellyFraction,
  filters,
  live,
}: {
  bundle: Bundle;
  bankroll: number;
  kellyFraction: number;
  filters: Filters;
  live?: boolean;
}) {
  const result = useMemo(
    () => runBundle(bundle, { bankroll, kellyFraction, filters }),
    [bundle, bankroll, kellyFraction, filters]
  );
  const n = bundle.ticks.length;
  const [idx, setIdx] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(16);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  // Reset playback when the game or strategy changes.
  useEffect(() => {
    setIdx(0);
    setPlaying(false);
  }, [bundle.match_id]);

  useEffect(() => {
    if (timer.current) clearInterval(timer.current);
    if (!playing) return;
    timer.current = setInterval(() => {
      setIdx((i) => {
        if (i >= n - 1) {
          setPlaying(false);
          return i;
        }
        return Math.min(n - 1, i + Math.max(1, Math.round(speed / 4)));
      });
    }, 1000 / Math.min(speed, 16));
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, [playing, speed, n]);

  const tick = bundle.ticks[Math.min(idx, n - 1)];
  const fillsSoFar = result.fills.filter((f) => f.tickIndex <= idx);
  const equity = result.equityCurve[Math.min(idx, result.equityCurve.length - 1)]?.equity ?? bankroll;
  const pnlNow = equity - bankroll;
  const clvFills = fillsSoFar.filter((f) => f.clvPreoff != null);
  const clvNow = clvFills.length
    ? clvFills.reduce((s, f) => s + (f.clvPreoff as number), 0) / clvFills.length
    : null;
  const atEnd = idx >= n - 1;
  const finalLabel = { H: bundle.home_team, D: "Draw", A: bundle.away_team }[bundle.outcome];

  return (
    <div className="panel">
      <div className="row spread">
        <h2 style={{ margin: 0 }}>
          {bundle.home_team} vs {bundle.away_team}
          {live && <span className="badge" style={{ marginLeft: 8, color: "var(--yellow)", borderColor: "var(--yellow)" }}>● near-live</span>}
        </h2>
        <div className="mono" style={{ fontSize: 18, fontWeight: 650 }}>
          {tick.score[0]}–{tick.score[1]}
          <span style={{ color: "var(--muted)", fontWeight: 400, fontSize: 13 }}> · {tick.minute}&apos; {tick.period}</span>
        </div>
      </div>

      <div style={{ margin: "14px 0" }}>
        <ProbBars labels={[bundle.home_team.slice(0, 8), "Draw", bundle.away_team.slice(0, 8)]} model={tick.model} market={marketImplied(tick)} />
      </div>

      <div className="controls">
        <button className="primary" onClick={() => setPlaying((p) => !p)}>{playing ? "⏸" : "▶"}</button>
        <button onClick={() => { setPlaying(false); setIdx(0); }}>⏮</button>
        <input
          className="scrub"
          type="range"
          min={0}
          max={n - 1}
          value={idx}
          onChange={(e) => { setPlaying(false); setIdx(Number(e.target.value)); }}
        />
        <select value={speed} onChange={(e) => setSpeed(Number(e.target.value))}>
          {SPEEDS.map((s) => <option key={s} value={s}>{s}×</option>)}
        </select>
      </div>

      <div className="stats-row" style={{ marginTop: 14 }}>
        <Stat label="Bankroll" value={money(bankroll)} />
        <Stat label={atEnd ? "Final equity" : "Equity (live)"} value={money(equity)} cls={pnlNow >= 0 ? "pos" : "neg"} />
        <Stat label="P&L" value={money(pnlNow)} cls={pnlNow >= 0 ? "pos" : "neg"} />
        <Stat label="Pre-off CLV" value={clvNow == null ? "—" : signed(clvNow, 3)} cls={clvNow == null ? "" : clvNow >= 0 ? "pos" : "neg"} />
      </div>

      <div style={{ marginTop: 12 }}>
        <Sparkline points={result.equityCurve.slice(0, idx + 1).map((e) => e.equity)} baseline={bankroll} />
      </div>

      {atEnd && (
        <div className="note">
          Settled: <b>{finalLabel}</b> ({bundle.final_score[0]}–{bundle.final_score[1]}). {fillsSoFar.length} fills,
          final P&amp;L {money(result.pnl)}, pre-off CLV {result.clvPreoff == null ? "—" : signed(result.clvPreoff, 3)}.
        </div>
      )}

      <h2 style={{ marginTop: 18 }}>Fills ({fillsSoFar.length})</h2>
      <div className="fills">
        {fillsSoFar.length === 0 && <div className="note">No bets yet.</div>}
        {[...fillsSoFar].reverse().map((f, i) => (
          <div className="fill mono" key={i}>
            <span style={{ color: "var(--muted)" }}>{f.minute}&apos;</span>
            <span className={f.action}>{f.action.toUpperCase()}</span>
            <span>{f.outcome} ×{f.contracts} @ {f.entryCents}¢</span>
            <span className={f.clvPreoff == null ? "" : (f.clvPreoff >= 0 ? "pos" : "neg")} style={{ textAlign: "right" }}>
              {f.clvPreoff == null ? "" : signed(f.clvPreoff, 2)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
