"use client";

import { useMemo } from "react";
import type { TabId } from "../Sidebar";
import { cls, money, signed, StageBadge } from "../bits";
import { runMany } from "../../lib/sim/engine";
import { OUT_HEX } from "../../lib/format";
import type { Bundle } from "../../lib/sim/types";
import type { Manifest } from "../../lib/data";

export function Overview({
  manifest, bundles, adv, onPick,
}: {
  manifest: Manifest; bundles: Bundle[]; adv: boolean;
  onPick: (id: string, tab: TabId) => void;
}) {
  const base = useMemo(() => runMany(bundles), [bundles]);
  const brier = (manifest.aggregate as { calibration?: Record<string, number> }).calibration?.brier ?? 0;
  const rec = manifest.matches.reduce((a, x) => { a[x.outcome]++; return a; }, { H: 0, D: 0, A: 0 } as Record<string, number>);

  // The bot's settled paper-bet record. Win rate is NOT edge (see caption) — shown big
  // because it's the first thing people ask, then immediately contextualized.
  const settledBets = base.wins + base.losses;
  const winRate = settledBets ? base.wins / settledBets : 0;

  const cells = [
    { k: "Games", v: String(manifest.matches.length), sub: `${base.nFills} paper bets`, show: true },
    { k: "Paper P&L", v: money(base.pnl), c: cls(base.pnl), sub: "$100 / game, summed", show: true },
    { k: "Pre-off CLV", v: signed(base.clvPreoff ?? 0, 4), c: cls(base.clvPreoff ?? 0), sub: "vs opening line", show: adv },
    { k: "Brier", v: brier.toFixed(4), sub: "calibration · lower better", show: adv },
    { k: "Results", v: `${rec.H}–${rec.D}–${rec.A}`, sub: "home–draw–away", show: true },
  ].filter((c) => c.show);

  const byId = useMemo(() => new Map(bundles.map((b) => [b.match_id, b])), [bundles]);
  const recent = manifest.matches.slice(-6).reverse();

  return (
    <div>
      <div className="tabhead">
        <h1>Overview</h1>
        <div className="sub">A bot that paper-bets the in-play model on real World Cup matches — and the honest scoreboard.</div>
      </div>

      <div className="wlhero">
        <div className="wlcol">
          <div className="wlk">Paper bet record</div>
          <div className="wlbig">
            <span className="pos mono">{base.wins}</span>
            <span className="wldash">–</span>
            <span className="neg mono">{base.losses}</span>
          </div>
          <div className="wlsub">{base.wins} won · {base.losses} lost · {settledBets} settled bets</div>
        </div>
        <div className="wlcol">
          <div className="wlk">Win rate</div>
          <div className="wlbig mono">{(winRate * 100).toFixed(1)}%</div>
          <div className="wlbar" title={`${base.wins} of ${settledBets} settled bets won`}>
            <div className="wlbar-fill" style={{ width: `${winRate * 100}%` }} />
          </div>
        </div>
      </div>
      <div className="legend" style={{ margin: "0 0 16px" }}>
        <b>Win rate isn’t edge.</b> A bot can win most of its bets and still lose money by overpaying on the winners — the honest verdict is the P&amp;L and CLV below, not this number.
      </div>

      <div className="scoreboard" style={{ gridTemplateColumns: `repeat(${cells.length}, 1fr)` }}>
        {cells.map((c) => (
          <div className="cell" key={c.k}>
            <span className="k">{c.k}</span>
            <span className={`v ${c.c ?? ""}`}>{c.v}</span>
            <span className="sub2">{c.sub}</span>
          </div>
        ))}
      </div>

      <div className="panel" style={{ marginTop: 16 }}>
        <h2>The honest verdict</h2>
        <p style={{ color: "var(--muted)", lineHeight: 1.6, margin: "0 0 8px" }}>
          The bot watched {manifest.matches.length} matches and placed {base.nFills} fake-money bets ($100 per game).
          It reads each game well — but it does <span style={{ color: "var(--text)", fontWeight: 600 }}>not beat the market</span>.
          {adv
            ? ` Its bets enter ~${signed(base.clvPreoff ?? 0, 3)} vs the opening line (negative = paying up), and its calibration (Brier ${brier.toFixed(3)}) is solid. A well-calibrated model with no demonstrated edge.`
            : " Think of it as a lab to watch and learn from — not a tipster to follow."}
        </p>
      </div>

      <div className="panel" style={{ marginTop: 14 }}>
        <h2>Recent games</h2>
        <div className="gamesgrid">
          {recent.map((g) => {
            const b = byId.get(g.match_id);
            return (
            <div key={g.match_id} className="gamecard" onClick={() => onPick(g.match_id, "replay")}>
              {b?.round && <div style={{ marginBottom: 8 }}><StageBadge round={b.round} knockout={b.is_knockout} /></div>}
              <div className="gteams"><span>{g.home_team}</span><span style={{ color: "var(--faint)" }}>v</span><span>{g.away_team}</span></div>
              <div className="gscore mono">{g.final_score[0]}–{g.final_score[1]}</div>
              <div className="gmeta">
                <span className="outdot" style={{ background: OUT_HEX[g.outcome === "H" ? "home" : g.outcome === "D" ? "draw" : "away"] }} />
                {g.outcome === "H" ? `${g.home_team} win` : g.outcome === "D" ? "Draw" : `${g.away_team} win`}
              </div>
            </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
