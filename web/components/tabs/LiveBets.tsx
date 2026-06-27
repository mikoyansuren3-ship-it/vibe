"use client";

// Live section for the Bets tab: for every in-progress game, the bot's OPEN
// positions (marked-to-market) plus an exhaustive board of EVERY captured Kalshi
// contract — model price vs market + the engine's would-bet flag — even the ones
// the model can't price (half-markets, corners) which are shown market-only.

import { useState } from "react";
import { runBundle } from "../../lib/sim/engine";
import { evaluateTick } from "../../lib/sim/policy";
import { evalContract } from "../../lib/sim/markets";
import { outcomeName, OUT_HEX } from "../../lib/format";
import type { Bundle, Filters, LiveContract, OutcomeKey } from "../../lib/sim/types";
import { cls, pct, signed } from "../bits";

const sMoney = (x: number) => (x >= 0 ? "+" : "−") + "$" + Math.abs(x).toFixed(2);
const OUTS: OutcomeKey[] = ["home", "draw", "away"];

interface Line {
  label: string;
  color?: string;
  modelP: number | null;
  mid: number | null;
  edge: number | null;
  action: "buy" | "sell" | null;
  wouldBet: boolean;
}

function EdgeCell({ ln }: { ln: Line }) {
  if (ln.modelP == null) return <span className="note" style={{ margin: 0, fontSize: 11 }}>no model price</span>;
  const verb = ln.action === "buy" ? "back" : ln.action === "sell" ? "fade" : "—";
  return (
    <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
      <span className={`mono ${cls(ln.edge ?? 0)}`} title="model − market" style={{ minWidth: 52, textAlign: "right" }}>
        {ln.edge == null ? "" : signed(ln.edge, 2)}
      </span>
      {ln.wouldBet ? (
        <span className={`tag2 ${ln.action === "buy" ? "back" : "fade"}`}>{verb} ✓</span>
      ) : (
        <span className="note" style={{ margin: 0, fontSize: 11 }}>no bet</span>
      )}
    </span>
  );
}

function ContractRow({ ln }: { ln: Line }) {
  return (
    <div className="betrow" style={{ gap: 10 }}>
      <span className="who" style={{ minWidth: 0 }}>
        {ln.color && <span className="outdot" style={{ background: ln.color }} />}
        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{ln.label}</span>
      </span>
      <span className="spring" />
      <span className="mono" style={{ color: "var(--muted)", minWidth: 92, textAlign: "right" }}>
        mkt {ln.mid == null ? "—" : pct(ln.mid)}
      </span>
      <span className="mono" style={{ color: "var(--home)", minWidth: 92, textAlign: "right" }}>
        model {ln.modelP == null ? "—" : pct(ln.modelP)}
      </span>
      <span style={{ minWidth: 150, textAlign: "right" }}><EdgeCell ln={ln} /></span>
    </div>
  );
}

function Group({ title, sub, lines, openDefault }: { title: string; sub?: string; lines: Line[]; openDefault?: boolean }) {
  const [open, setOpen] = useState(!!openDefault);
  if (lines.length === 0) return null;
  const nBet = lines.filter((l) => l.wouldBet).length;
  return (
    <div className={`betcard ${open ? "open" : ""}`}>
      <div className="head" onClick={() => setOpen((o) => !o)}>
        <div><div className="title">{title}</div>{sub && <div className="desc">{sub}</div>}</div>
        {nBet > 0 && <span className="tag2 back" style={{ marginLeft: "auto" }}>{nBet} edge</span>}
        <span className="count" style={{ marginLeft: nBet > 0 ? 8 : "auto" }}>{lines.length}</span>
        <span className="chev">›</span>
      </div>
      {open && <div className="betlist">{lines.map((ln, i) => <ContractRow key={i} ln={ln} />)}</div>}
    </div>
  );
}

function contractToLine(c: LiveContract, cfg: Bundle["config"]): Line {
  const s = evalContract(c, cfg);
  return { label: c.label, modelP: s.modelP, mid: s.mid, edge: s.edge, action: s.action, wouldBet: s.wouldBet };
}

function LiveGame({ bundle, bankroll, kellyFraction, filters }: {
  bundle: Bundle; bankroll: number; kellyFraction: number; filters: Filters;
}) {
  const r = runBundle(bundle, { bankroll, kellyFraction, filters });
  const open = r.decisions.filter((d) => d.category === "taken");
  const last = bundle.ticks[bundle.ticks.length - 1];
  // Last KNOWN two-sided mid per outcome (carried forward, like the engine) — late in
  // a lopsided game the book often goes one-sided, so the final tick may have no mid.
  const lastMid: Partial<Record<OutcomeKey, number>> = {};
  for (const t of bundle.ticks) {
    for (const o of OUTS) {
      const q = t.markets[o];
      if (q && q[0] != null && q[1] != null) lastMid[o] = (q[0] + q[1]) / 200;
    }
  }
  const midOf = (o: OutcomeKey) => lastMid[o] ?? null;

  // 1X2 group from the latest tick that actually carries a quote (the very last tick
  // can be one-sided in a lopsided game); proper 3-way de-vig via evaluateTick.
  const lastQuoted = [...bundle.ticks].reverse().find((t) =>
    OUTS.some((o) => { const q = t.markets[o]; return q && q[0] != null && q[1] != null; })
  );
  const sigs = lastQuoted ? evaluateTick(lastQuoted, bundle.config) : [];
  const oneX2: Line[] = OUTS.filter((o) => sigs.some((s) => s.outcome === o)).map((o) => {
    const s = sigs.find((x) => x.outcome === o)!;
    return {
      label: outcomeName(bundle, o), color: OUT_HEX[o],
      modelP: s.modelP, mid: s.implied, edge: s.modelP - s.implied,
      action: s.rawEdge > 0 ? "buy" : s.rawEdge < 0 ? "sell" : null, wouldBet: s.actionable,
    };
  });

  const groups = bundle.all_markets ?? [];
  const priceable = groups.filter((g) => g.priceable);
  const marketOnly = groups.filter((g) => !g.priceable);

  return (
    <div style={{ marginBottom: 22 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, margin: "4px 0 10px" }}>
        <span className="livedot" />
        <span style={{ fontWeight: 700 }}>{bundle.home_team} {bundle.final_score[0]}–{bundle.final_score[1]} {bundle.away_team}</span>
        <span className="mono" style={{ color: "var(--muted)" }}>{bundle.minute}′</span>
      </div>

      {/* Ongoing bets: the bot's open positions, marked-to-market. */}
      <div className="betcard open" style={{ marginBottom: 12 }}>
        <div className="head" style={{ cursor: "default" }}>
          <div><div className="title">Ongoing bets</div><div className="desc">open positions, marked to the live market</div></div>
          <span className={`mono ${cls(r.pnl)}`} style={{ marginLeft: "auto", fontWeight: 700 }}>{sMoney(r.pnl)}</span>
        </div>
        <div className="betlist">
          {open.length === 0 && <div className="note" style={{ padding: "10px 15px", margin: 0 }}>No open positions — the bot sees no actionable 1X2 edge right now.</div>}
          {open.map((d, i) => {
            const cur = midOf(d.outcome);
            const entry = d.execCents / 100;
            const unreal = cur == null ? null : (d.action === "buy" ? d.contracts * (cur - entry) : d.contracts * (entry - cur));
            return (
              <div className="betrow" key={i} style={{ gap: 10 }}>
                <span className="who" style={{ minWidth: 0 }}>
                  <span className="outdot" style={{ background: OUT_HEX[d.outcome] }} />
                  <span className={`tag2 ${d.action === "buy" ? "back" : "fade"}`}>{d.action === "buy" ? "back" : "fade"}</span>
                  <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{outcomeName(bundle, d.outcome)}</span>
                </span>
                <span className="spring" />
                <span className="mono" style={{ color: "var(--muted)" }}>×{d.contracts}@{d.execCents}¢</span>
                <span className="mono" style={{ color: "var(--muted)", minWidth: 78, textAlign: "right" }}>now {cur == null ? "—" : pct(cur)}</span>
                {unreal != null && <span className={`mono ${cls(unreal)}`} style={{ minWidth: 64, textAlign: "right", fontWeight: 700 }}>{sMoney(unreal)}</span>}
              </div>
            );
          })}
        </div>
      </div>

      {/* Every possible bet. */}
      <Group title="Match result (1X2)" sub="the market the bot actually trades" lines={oneX2} openDefault />
      {priceable.map((g) => (
        <Group key={g.series} title={g.label} sub="model-priced — shown with edge & would-bet" lines={g.contracts.map((c) => contractToLine(c, bundle.config))} />
      ))}
      {marketOnly.map((g) => (
        <Group key={g.series} title={g.label} sub="market price only — the model can't price this" lines={g.contracts.map((c) => contractToLine(c, bundle.config))} />
      ))}
    </div>
  );
}

export function LiveBets({ bundles, bankroll, kellyFraction, filters }: {
  bundles: Bundle[]; bankroll: number; kellyFraction: number; filters: Filters;
}) {
  if (bundles.length === 0) return null;
  return (
    <div style={{ marginBottom: 26 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, margin: "0 0 6px" }}>
        <h2 style={{ margin: 0, fontSize: 17 }}>Live now</h2>
        <span className="note" style={{ margin: 0 }}>{bundles.length} game{bundles.length > 1 ? "s" : ""} in progress — every Kalshi contract, even the ones the bot ignores.</span>
      </div>
      {bundles.map((b) => (
        <LiveGame key={b.match_id} bundle={b} bankroll={bankroll} kellyFraction={kellyFraction} filters={filters} />
      ))}
    </div>
  );
}
