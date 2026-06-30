"use client";

import { useMemo, useState } from "react";
import { runBundle } from "../../lib/sim/engine";
import { actionVerb, CATEGORY, CONSIDERED_ORDER, outcomeName, OUT_HEX, wonLabel } from "../../lib/format";
import type { Bundle, Decision, DecisionCategory, Filters } from "../../lib/sim/types";
import { cls, money, signed } from "../bits";
import { LiveBets } from "./LiveBets";

const CAP = 60;
const sMoney = (x: number) => (x >= 0 ? "+" : "−") + "$" + Math.abs(x).toFixed(2);

interface Row { d: Decision; b: Bundle }

function BetRow({ d, b, adv, showGame }: Row & { adv: boolean; showGame: boolean }) {
  const taken = d.category === "taken";
  const wl = wonLabel(d.won);
  return (
    <div className="betrow">
      <span className="who">
        <span className="outdot" style={{ background: OUT_HEX[d.outcome] }} />
        <span className={`tag2 ${d.action === "buy" ? "back" : "fade"}`}>{actionVerb(d.action)}</span>
        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{outcomeName(b, d.outcome)}</span>
      </span>
      {showGame && <span className="gname">{b.home_team.slice(0, 7)}–{b.away_team.slice(0, 7)}</span>}
      {adv && <span className="mono" style={{ color: "var(--faint)" }}>{d.minute}′</span>}
      {adv && taken && <span className="mono" style={{ color: "var(--muted)" }}>×{d.contracts}@{d.execCents}¢</span>}
      {adv && <span className={`mono ${cls(d.clvPreoff)}`} title="pre-off CLV" style={{ width: 44, textAlign: "right" }}>{d.clvPreoff == null ? "" : signed(d.clvPreoff, 2)}</span>}
      <span className="spring betmoney">
        {taken ? (
          <>
            <span className="stake mono">bet {money(d.staked)}</span>
            {d.pnl != null && <span className={`mono ${cls(d.pnl)}`} style={{ fontWeight: 700, minWidth: 64, textAlign: "right" }}>{sMoney(d.pnl)}</span>}
            <span className={`wl ${wl.cls}`} style={{ minWidth: 36, textAlign: "right" }}>{wl.text}</span>
          </>
        ) : (
          <span style={{ color: "var(--faint)" }}>not placed</span>
        )}
      </span>
    </div>
  );
}

function Section({ cat, rows, adv, showGame }: { cat: DecisionCategory; rows: Row[]; adv: boolean; showGame: boolean }) {
  const meta = CATEGORY[cat];
  const [open, setOpen] = useState(cat === "taken");
  if (rows.length === 0) return null;
  return (
    <div className={`betcard ${open ? "open" : ""}`}>
      <div className="head" onClick={() => setOpen((o) => !o)}>
        <span className="ic">{meta.icon}</span>
        <div><div className="title">{meta.title}</div><div className="desc">{meta.desc}</div></div>
        <span className="count">{rows.length}</span>
        <span className="chev">›</span>
      </div>
      {open && (
        <div className="betlist">
          {rows.slice(0, CAP).map((r, i) => <BetRow key={i} {...r} adv={adv} showGame={showGame} />)}
          {rows.length > CAP && <div className="note" style={{ padding: "8px 15px", margin: 0 }}>+{rows.length - CAP} more…</div>}
        </div>
      )}
    </div>
  );
}

export function Bets({
  bundles, liveBundles, upcomingBundles = [], liveUpdatedAt, selectedId, bankroll, kellyFraction, filters, adv,
}: {
  bundles: Bundle[]; liveBundles: Bundle[]; upcomingBundles?: Bundle[]; liveUpdatedAt?: number | null; selectedId: string;
  bankroll: number; kellyFraction: number; filters: Filters; adv: boolean;
}) {
  const [scope, setScope] = useState<"all" | "game">("all");
  const [type, setType] = useState<"all" | "buy" | "sell">("all");

  const rows = useMemo(() => {
    const src = scope === "game" ? bundles.filter((b) => b.match_id === selectedId) : bundles;
    const out: Row[] = [];
    for (const b of src) {
      const r = runBundle(b, { bankroll, kellyFraction, filters });
      for (const d of r.decisions) {
        if (type !== "all" && d.action !== type) continue;
        out.push({ d, b });
      }
    }
    return out;
  }, [bundles, selectedId, scope, type, bankroll, kellyFraction, filters]);

  const byCat = (c: DecisionCategory) => rows.filter((r) => r.d.category === c)
    .sort((a, z) => Math.abs(z.d.netEdge) - Math.abs(a.d.netEdge));
  const taken = byCat("taken");
  const consideredCats = CONSIDERED_ORDER.filter((c) => rows.some((r) => r.d.category === c));
  const consideredTotal = rows.length - taken.length;
  const showGame = scope === "all";
  const selBundle = bundles.find((b) => b.match_id === selectedId);

  // Overall money summary across taken bets in the current filter.
  const staked = taken.reduce((s, r) => s + r.d.staked, 0);
  const netPnl = taken.reduce((s, r) => s + (r.d.pnl ?? 0), 0);
  const roi = staked > 0 ? netPnl / staked : 0;

  return (
    <div>
      <div className="tabhead">
        <h1>Bets</h1>
        <div className="sub">Every bet the bot <b>took</b> (with stake &amp; return) — and every one it <b>considered</b> but skipped, grouped by why.</div>
      </div>

      <LiveBets bundles={liveBundles} upcoming={upcomingBundles} bankroll={bankroll} kellyFraction={kellyFraction} filters={filters} updatedAt={liveUpdatedAt} />

      <div className="betfilters">
        <span className={`chip ${scope === "all" ? "on" : ""}`} onClick={() => setScope("all")}>All games</span>
        <span className={`chip ${scope === "game" ? "on" : ""}`} onClick={() => setScope("game")}>
          This game{selBundle ? `: ${selBundle.home_team.slice(0, 7)}–${selBundle.away_team.slice(0, 7)}` : ""}
        </span>
        <span style={{ width: 12 }} />
        {(["all", "buy", "sell"] as const).map((t) => (
          <span key={t} className={`chip ${type === t ? "on" : ""}`} onClick={() => setType(t)}>
            {t === "all" ? "All types" : t === "buy" ? "Backs" : "Fades"}
          </span>
        ))}
      </div>

      {/* overall money summary (taken bets) */}
      <div className="betsum">
        <div className="cell"><div className="k">Total staked</div><div className="v mono">{money(staked)}</div></div>
        <div className="cell"><div className="k">Total returned</div><div className="v mono">{money(staked + netPnl)}</div></div>
        <div className="cell"><div className="k">Net P&L</div><div className={`v mono ${cls(netPnl)}`}>{sMoney(netPnl)}</div></div>
        <div className="cell"><div className="k">Return on stake</div><div className={`v mono ${cls(roi)}`}>{(roi * 100 >= 0 ? "+" : "") + (roi * 100).toFixed(1)}%</div></div>
      </div>
      <div style={{ display: "flex", gap: 18, marginBottom: 14, fontSize: 13 }}>
        <div><span className="pos" style={{ fontWeight: 700, fontSize: 16 }}>{taken.length}</span> <span style={{ color: "var(--muted)" }}>taken</span></div>
        <div><span style={{ fontWeight: 700, fontSize: 16 }}>{consideredTotal}</span> <span style={{ color: "var(--muted)" }}>considered</span></div>
      </div>

      <Section cat="taken" rows={taken} adv={adv} showGame={showGame} />
      {consideredCats.map((c) => <Section key={c} cat={c} rows={byCat(c)} adv={adv} showGame={showGame} />)}
      <div className="note">
        Stakes use the current ${bankroll}/game bankroll. {adv ? "Net P&L sums each bet's settled profit/loss; ROI = net ÷ staked." : "Switch to Advanced for entry prices, edges and CLV per bet."}
      </div>
    </div>
  );
}
