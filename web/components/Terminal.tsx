"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { runBundle } from "../lib/sim/engine";
import { devigProportional } from "../lib/sim/policy";
import type { Bundle, Filters, OutcomeKey } from "../lib/sim/types";
import { FINAL_LABEL, goalMinutes, outcomeWon } from "../lib/sim/util";
import { actionVerb, outcomeName } from "../lib/format";
import { cls, DualBars, EquityChart, money, signed, Tile } from "./bits";

const SPEEDS = [1, 4, 16, 64];

function marketImplied(tick: Bundle["ticks"][number]): [number | null, number | null, number | null] {
  const keys: OutcomeKey[] = ["home", "draw", "away"];
  const present = keys.filter((k) => {
    const q = tick.markets[k];
    return q && q[0] != null && q[1] != null;
  });
  const dev = devigProportional(present.map((k) => {
    const [b, a] = tick.markets[k] as [number, number];
    return (b + a) / 200;
  }));
  const out: Record<string, number> = {};
  present.forEach((k, i) => (out[k] = dev[i]));
  return [out.home ?? null, out.draw ?? null, out.away ?? null];
}

export function Terminal({
  bundle, bankroll, kellyFraction, filters, live, adv = true,
}: {
  bundle: Bundle; bankroll: number; kellyFraction: number; filters: Filters; live?: boolean; adv?: boolean;
}) {
  const result = useMemo(() => runBundle(bundle, { bankroll, kellyFraction, filters }), [bundle, bankroll, kellyFraction, filters]);
  const goals = useMemo(() => goalMinutes(bundle), [bundle]);
  const n = bundle.ticks.length;
  const [idx, setIdx] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(16);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => { setIdx(0); setPlaying(false); }, [bundle.match_id]);
  useEffect(() => {
    if (timer.current) clearInterval(timer.current);
    if (!playing) return;
    timer.current = setInterval(() => {
      setIdx((i) => { if (i >= n - 1) { setPlaying(false); return i; } return Math.min(n - 1, i + Math.max(1, Math.round(speed / 4))); });
    }, 1000 / Math.min(speed, 16));
    return () => { if (timer.current) clearInterval(timer.current); };
  }, [playing, speed, n]);

  const tick = bundle.ticks[Math.min(idx, n - 1)];
  const fillsSoFar = result.fills.filter((f) => f.tickIndex <= idx);
  const consideredN = result.decisions.filter((d) => d.category !== "taken").length;
  const equity = result.equityCurve[Math.min(idx, result.equityCurve.length - 1)]?.equity ?? bankroll;
  const pnlNow = equity - bankroll;
  const clvF = fillsSoFar.filter((f) => f.clvPreoff != null);
  const clvNow = clvF.length ? clvF.reduce((s, f) => s + (f.clvPreoff as number), 0) / clvF.length : null;
  const atEnd = idx >= n - 1;
  const settled = bundle.outcome != null;
  const playedFrac = (idx / (n - 1)) * 100;

  return (
    <div className="panel">
      <div className="matchup">
        <div className="teams">{bundle.home_team} <span style={{ color: "var(--faint)" }}>vs</span> {bundle.away_team}</div>
        <div className="clock">
          <div className="score mono">{tick.score[0]}–{tick.score[1]}</div>
          <div className="min">{live && <span className="livedot" />}{tick.minute}′ · {tick.period}</div>
        </div>
      </div>

      <DualBars labels={[bundle.home_team.slice(0, 9), "Draw", bundle.away_team.slice(0, 9)]} model={tick.model} market={marketImplied(tick)} showEdge={adv} />

      <div className="controls">
        <button className="primary" onClick={() => setPlaying((p) => !p)}>{playing ? "❚❚" : "▶"}</button>
        <button onClick={() => { setPlaying(false); setIdx(0); }}>↺</button>
        <div className="timeline">
          <div className="track"><div className="played" style={{ width: `${playedFrac}%` }} /></div>
          {goals.map((g, i) => (
            <span key={`g${i}`} className="marker goal" style={{ left: `${Math.min(100, (g.minute / 90) * 100)}%` }} title={`${g.team} goal ${g.minute}'`}>⚽</span>
          ))}
          {result.fills.map((f, i) => (
            <span key={`f${i}`} className={`marker fill ${f.action}`} style={{ left: `${Math.min(100, (f.minute / 90) * 100)}%` }} title={`${f.action} ${f.outcome} ${f.minute}'`} />
          ))}
          <input type="range" min={0} max={n - 1} value={idx} onChange={(e) => { setPlaying(false); setIdx(Number(e.target.value)); }} />
        </div>
        <select value={speed} onChange={(e) => setSpeed(Number(e.target.value))}>
          {SPEEDS.map((s) => <option key={s} value={s}>{s}×</option>)}
        </select>
      </div>

      <div className="tiles" style={{ gridTemplateColumns: `repeat(${adv ? 4 : 3}, 1fr)` }}>
        <Tile k="Bankroll" v={money(bankroll)} />
        <Tile k={atEnd ? "Final equity" : "Equity"} v={money(equity)} c={cls(pnlNow)} />
        <Tile k="P&L" v={money(pnlNow)} c={cls(pnlNow)} />
        {adv && <Tile k="Pre-off CLV" v={clvNow == null ? "—" : signed(clvNow, 3)} c={cls(clvNow)} />}
      </div>

      <div style={{ marginTop: 14 }}>
        <EquityChart points={result.equityCurve.slice(0, idx + 1).map((e) => e.equity)} baseline={bankroll} goals={goals} now={idx} totalTicks={n} />
      </div>

      {atEnd && !settled && (
        <div className="banner">
          <span className="pill" style={{ background: "rgba(227,179,65,0.16)", color: "var(--yellow)" }}>● IN PROGRESS</span>
          <span>Live through <b className="mono">{tick.minute}′</b> · {fillsSoFar.length} bets open · unrealized P&L <b className={cls(pnlNow)}>{money(pnlNow)}</b></span>
        </div>
      )}
      {atEnd && settled && (
        <div className="banner">
          <span className={`pill ${bundle.outcome}`}>{FINAL_LABEL[bundle.outcome as string]}</span>
          <span>
            Settled <b className="mono">{bundle.final_score[0]}–{bundle.final_score[1]}</b> · {fillsSoFar.length} bets ·
            P&L <b className={cls(result.pnl)}>{money(result.pnl)}</b>
            {adv && <> · CLV <b className={cls(result.clvPreoff)}>{result.clvPreoff == null ? "—" : signed(result.clvPreoff, 3)}</b></>}
          </span>
        </div>
      )}

      <div className="h2row" style={{ marginTop: 18 }}>
        <h2>Bets taken ({fillsSoFar.length})</h2>
        <span className="note" style={{ margin: 0 }}>{consideredN} considered → Bets tab</span>
      </div>
      <div className="fills">
        {fillsSoFar.length === 0 && <div className="note" style={{ marginTop: 0 }}>No bets yet — the bot is waiting for a worthwhile edge.</div>}
        {[...fillsSoFar].reverse().map((f, i) => {
          // A back wins if the outcome occurs; a fade wins if it does NOT. (null while live.)
          const occ = settled ? outcomeWon(f.outcome, bundle.outcome as string) : null;
          const won = atEnd && occ != null ? (f.action === "buy" ? occ : !occ) : null;
          return (
            <div className="fill" style={{ gridTemplateColumns: adv ? "40px 46px 1fr 56px" : "40px 1fr auto" }} key={i}>
              <span className="mono" style={{ color: "var(--faint)" }}>{f.minute}′</span>
              {adv && <span className={`mono ${f.action}`}>{f.action.toUpperCase()}</span>}
              <span>
                <span className={f.action}>{actionVerb(f.action)}</span> {outcomeName(bundle, f.outcome)}
                {adv && <span className="mono" style={{ color: "var(--muted)" }}> ×{f.contracts} @{f.entryCents}¢</span>}
                {won != null && <span className={won ? "pos" : "neg"}> · {won ? "WON" : "LOST"}</span>}
              </span>
              {adv && <span className={`mono ${cls(f.clvPreoff)}`} style={{ textAlign: "right" }}>{f.clvPreoff == null ? "" : signed(f.clvPreoff, 2)}</span>}
            </div>
          );
        })}
      </div>
    </div>
  );
}
