"use client";

import { useMemo, useState } from "react";
import type { Bundle, DerivedMarket } from "../../lib/sim/types";
import { pct } from "../bits";

const TYPE_LABEL: Record<string, string> = { total: "Total goals (over/under)", spread: "Spread / handicap", team_total: "Team totals", btts: "Both teams to score" };
const TYPE_ORDER = ["total", "btts", "spread", "team_total"];

/** model (blue) vs market (grey) probability over the match, converging to the 0/1 result. */
function MarketChart({ m }: { m: DerivedMarket }) {
  const W = 240, H = 46;
  const x = (min: number) => (Math.min(90, min) / 90) * W;
  const y = (p: number) => (1 - p) * H;
  const model = m.ticks.map((t) => `${x(t[0])},${y(t[1])}`).join(" ");
  const market = m.ticks.map((t) => `${x(t[0])},${y(t[2])}`).join(" ");
  const settleY = y(m.settled_yes ? 1 : 0);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" style={{ width: "100%", height: 46 }}>
      <line x1="0" y1={settleY} x2={W} y2={settleY} stroke={m.settled_yes ? "var(--green)" : "var(--red)"} strokeWidth="0.6" strokeDasharray="2 3" opacity="0.5" />
      <polyline points={market} fill="none" stroke="var(--muted)" strokeWidth="1.1" vectorEffect="non-scaling-stroke" opacity="0.8" />
      <polyline points={model} fill="none" stroke="var(--home)" strokeWidth="1.4" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

function MarketRow({ m, adv }: { m: DerivedMarket; adv: boolean }) {
  const [, o0, m0] = m.ticks[0];
  const y = m.settled_yes ? 1 : 0;
  const modelCloser = Math.abs(o0 - y) < Math.abs(m0 - y);
  return (
    <div className="mktrow">
      <div className="mktlabel">{m.label}</div>
      <MarketChart m={m} />
      <div className="mktnums">
        <span><span style={{ color: "var(--home)" }}>model {pct(o0)}</span> · <span style={{ color: "var(--muted)" }}>mkt {pct(m0)}</span></span>
        <span>
          <span className={`tag2 ${m.settled_yes ? "back" : "fade"}`}>{m.settled_yes ? "YES" : "NO"}</span>
          {adv && <span className="note" style={{ margin: "0 0 0 8px" }}>{modelCloser ? "model closer" : "market closer"}</span>}
        </span>
      </div>
    </div>
  );
}

export function Markets({ bundles, adv }: { bundles: Bundle[]; adv: boolean }) {
  const withDerived = useMemo(() => bundles.filter((b) => b.derived && b.derived.length), [bundles]);
  const [gid, setGid] = useState<string>("");
  const game = withDerived.find((b) => b.match_id === gid) ?? withDerived[withDerived.length - 1];

  if (withDerived.length === 0) {
    return (
      <div>
        <div className="tabhead"><h1>Markets</h1></div>
        <div className="panel"><div className="note" style={{ margin: 0 }}>No scoreline-market data captured yet.</div></div>
      </div>
    );
  }

  const dv = game?.derived ?? [];
  // Honest accuracy: opening Brier, model vs market, across this game's contracts.
  const bm = dv.reduce((s, m) => s + (m.ticks[0][1] - (m.settled_yes ? 1 : 0)) ** 2, 0) / (dv.length || 1);
  const bk = dv.reduce((s, m) => s + (m.ticks[0][2] - (m.settled_yes ? 1 : 0)) ** 2, 0) / (dv.length || 1);
  const sharper = bk < bm ? "the market" : "the model";

  return (
    <div>
      <div className="tabhead">
        <h1>Markets</h1>
        <div className="sub">How the model prices Total, BTTS &amp; Spread vs Kalshi&apos;s lines — and how each settled. Read-only: the bot doesn&apos;t bet these.</div>
        <div className="gamenav" style={{ marginLeft: 0 }}>
          <select value={game?.match_id ?? ""} onChange={(e) => setGid(e.target.value)}>
            {withDerived.map((b) => (
              <option key={b.match_id} value={b.match_id}>{b.home_team} {b.final_score[0]}–{b.final_score[1]} {b.away_team}</option>
            ))}
          </select>
        </div>
      </div>

      <div className="panel" style={{ marginBottom: 14 }}>
        <div className="note" style={{ margin: 0 }}>
          On this game, <b style={{ color: "var(--text)" }}>{sharper}&apos;s opening lines were sharper</b>
          {adv && <> (opening Brier — model {bm.toFixed(3)} vs market {bk.toFixed(3)})</>}.
          Across all 8 games with this data the model does <b style={{ color: "var(--text)" }}>not beat Kalshi</b> on derived markets — the lines are sharper than on 1X2. Blue = model, grey = market; dashed line = how it settled.
        </div>
      </div>

      {TYPE_ORDER.filter((t) => dv.some((m) => m.type === t)).map((t) => (
        <div className="panel" key={t} style={{ marginBottom: 12 }}>
          <h2>{TYPE_LABEL[t]}</h2>
          {dv.filter((m) => m.type === t).sort((a, b) => (a.strike ?? 0) - (b.strike ?? 0)).map((m, i) => (
            <MarketRow key={i} m={m} adv={adv} />
          ))}
        </div>
      ))}
    </div>
  );
}
