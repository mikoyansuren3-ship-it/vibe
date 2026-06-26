"use client";

import { useMemo } from "react";
import { runBundle } from "../../lib/sim/engine";
import { OUT_HEX } from "../../lib/format";
import type { Bundle } from "../../lib/sim/types";
import { cls, money, signed } from "../bits";

export function Games({
  bundles, adv, onPick,
}: {
  bundles: Bundle[]; adv: boolean; onPick: (id: string) => void;
}) {
  const stats = useMemo(() => {
    const m: Record<string, { pnl: number; clv: number | null }> = {};
    if (adv) for (const b of bundles) { const r = runBundle(b); m[b.match_id] = { pnl: r.pnl, clv: r.clvPreoff }; }
    return m;
  }, [bundles, adv]);

  return (
    <div>
      <div className="tabhead">
        <h1>Games</h1>
        <div className="sub">All {bundles.length} recorded matches. Click one to watch the bot bet it in Replay.</div>
      </div>
      <div className="gamesgrid">
        {bundles.map((b) => {
          const dotKey = b.outcome === "H" ? "home" : b.outcome === "D" ? "draw" : "away";
          const s = stats[b.match_id];
          return (
            <div key={b.match_id} className="gamecard" onClick={() => onPick(b.match_id)}>
              <div className="gteams"><span>{b.home_team}</span><span style={{ color: "var(--faint)" }}>v</span><span>{b.away_team}</span></div>
              <div className="gscore mono">{b.final_score[0]}–{b.final_score[1]}</div>
              <div className="gmeta">
                <span className="outdot" style={{ background: OUT_HEX[dotKey] }} />
                {b.outcome === "H" ? `${b.home_team} win` : b.outcome === "D" ? "Draw" : `${b.away_team} win`}
              </div>
              {adv && s && (
                <div className="gmeta" style={{ marginTop: 8, justifyContent: "space-between" }}>
                  <span>P&L <b className={cls(s.pnl)}>{money(s.pnl)}</b></span>
                  <span>CLV <b className={cls(s.clv)}>{s.clv == null ? "—" : signed(s.clv, 3)}</b></span>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
