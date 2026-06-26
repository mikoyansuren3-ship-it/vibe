"use client";

import { useMemo, useState } from "react";
import { runBundle } from "../../lib/sim/engine";
import { actionVerb, CATEGORY, CONSIDERED_ORDER, outcomeName, OUT_HEX, wonLabel } from "../../lib/format";
import type { Bundle, Decision, DecisionCategory, Filters } from "../../lib/sim/types";
import { cls, signed } from "../bits";

const CAP = 50;

interface Row { d: Decision; b: Bundle }

function BetRow({ d, b, adv, showGame }: Row & { adv: boolean; showGame: boolean }) {
  const wl = wonLabel(d.won);
  const cols = adv ? "1.4fr 0.9fr auto auto auto auto" : "1fr auto auto";
  return (
    <div className="betrow" style={{ gridTemplateColumns: cols }}>
      <span className="who">
        <span className="outdot" style={{ background: OUT_HEX[d.outcome] }} />
        <span className={`tag2 ${d.action === "buy" ? "back" : "fade"}`}>{actionVerb(d.action)}</span>
        <span>{outcomeName(b, d.outcome)}</span>
      </span>
      {showGame ? <span className="gname">{b.home_team.slice(0, 8)}–{b.away_team.slice(0, 8)}</span> : <span />}
      {adv && <span className="mono" style={{ color: "var(--muted)" }}>{d.minute}′</span>}
      {adv && <span className="mono">{d.contracts > 0 ? `×${d.contracts} @${d.execCents}¢` : `@${d.execCents}¢`}</span>}
      {adv && <span className={`mono ${cls(d.clvPreoff)}`} title="pre-off CLV">{d.clvPreoff == null ? "—" : signed(d.clvPreoff, 2)}</span>}
      <span className={`wl ${wl.cls}`} style={{ textAlign: "right" }}>{d.category === "taken" ? wl.text : ""}</span>
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
        <div>
          <div className="title">{meta.title}</div>
          <div className="desc">{meta.desc}</div>
        </div>
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
  bundles, selectedId, bankroll, kellyFraction, filters, adv,
}: {
  bundles: Bundle[]; selectedId: string;
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
  const consideredTotal = rows.filter((r) => r.d.category !== "taken").length;
  const showGame = scope === "all";
  const selBundle = bundles.find((b) => b.match_id === selectedId);

  return (
    <div>
      <div className="tabhead">
        <h1>Bets</h1>
        <div className="sub">Every bet the bot <b>took</b> — and every one it <b>considered</b> but skipped, grouped by why.</div>
      </div>

      <div className="betfilters">
        <span className={`chip ${scope === "all" ? "on" : ""}`} onClick={() => setScope("all")}>All games</span>
        <span className={`chip ${scope === "game" ? "on" : ""}`} onClick={() => setScope("game")}>
          This game{selBundle ? `: ${selBundle.home_team.slice(0, 8)}–${selBundle.away_team.slice(0, 8)}` : ""}
        </span>
        <span style={{ width: 12 }} />
        {(["all", "buy", "sell"] as const).map((t) => (
          <span key={t} className={`chip ${type === t ? "on" : ""}`} onClick={() => setType(t)}>
            {t === "all" ? "All types" : t === "buy" ? "Backs" : "Fades"}
          </span>
        ))}
      </div>

      <div style={{ display: "flex", gap: 18, marginBottom: 14, fontSize: 13 }}>
        <div><span className="pos" style={{ fontWeight: 700, fontSize: 18 }}>{taken.length}</span> <span style={{ color: "var(--muted)" }}>taken</span></div>
        <div><span style={{ fontWeight: 700, fontSize: 18 }}>{consideredTotal}</span> <span style={{ color: "var(--muted)" }}>considered</span></div>
      </div>

      <Section cat="taken" rows={taken} adv={adv} showGame={showGame} />
      {consideredCats.map((c) => <Section key={c} cat={c} rows={byCat(c)} adv={adv} showGame={showGame} />)}
      {!adv && (
        <div className="note">In Basic mode you see what the bot bet and what won. Switch to Advanced for entry prices, edges and CLV per bet.</div>
      )}
    </div>
  );
}
