"use client";

// Live section for the Bets tab: for every in-progress game, the bot's OPEN
// positions (marked-to-market) plus an exhaustive board of EVERY captured Kalshi
// contract — model price vs market + the engine's would-bet flag — even the ones
// the model can't price (half-markets, corners) which are shown market-only.

import { useMemo, useState } from "react";
import { runBundle } from "../../lib/sim/engine";
import { evaluateTick } from "../../lib/sim/policy";
import { evalContract } from "../../lib/sim/markets";
import { outcomeName, OUT_HEX } from "../../lib/format";
import type { Bundle, Filters, LiveContract, OutcomeKey } from "../../lib/sim/types";
import { cls, DualBars, pct, signed } from "../bits";

const sMoney = (x: number) => (x >= 0 ? "+" : "−") + "$" + Math.abs(x).toFixed(2);
const OUTS: OutcomeKey[] = ["home", "draw", "away"];

interface Line {
  label: string;
  color?: string;
  modelP: number | null;
  mid: number | null;
  edge: number | null;
  action: "buy" | "sell" | null;
  meetsBar: boolean; // clears the edge bar in isolation
  taken: boolean;    // the engine would actually take this (1X2 strongest leg per tick only)
}

function EdgeCell({ ln }: { ln: Line }) {
  if (ln.modelP == null) return <span className="note" style={{ margin: 0, fontSize: 11 }}>no model price</span>;
  const verb = ln.action === "buy" ? "back" : ln.action === "sell" ? "fade" : "—";
  return (
    <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
      <span className={`mono ${cls(ln.edge ?? 0)}`} title="model − market" style={{ minWidth: 52, textAlign: "right" }}>
        {ln.edge == null ? "" : signed(ln.edge, 2)}
      </span>
      {ln.taken ? (
        <span className={`tag2 ${ln.action === "buy" ? "back" : "fade"}`} title="the engine takes this leg (strongest 1X2 leg this tick)">{verb} ✓</span>
      ) : ln.meetsBar ? (
        <span className="mono" style={{ fontSize: 11, color: "var(--muted)" }} title="clears the edge bar in isolation — but the engine trades only 1X2 and only the strongest leg per tick">meets bar</span>
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
  const nTaken = lines.filter((l) => l.taken).length;
  const nBar = lines.filter((l) => l.meetsBar).length;
  return (
    <div className={`betcard ${open ? "open" : ""}`}>
      <div className="head" onClick={() => setOpen((o) => !o)}>
        <div><div className="title">{title}</div>{sub && <div className="desc">{sub}</div>}</div>
        {nTaken > 0 && <span className="tag2 back" style={{ marginLeft: "auto" }}>{nTaken} taken</span>}
        {nBar > 0 && <span className="count" style={{ marginLeft: nTaken > 0 ? 8 : "auto", color: "var(--muted)" }} title="contracts that clear the edge bar but the bot doesn't trade">{nBar} meets bar</span>}
        <span className="count" style={{ marginLeft: nTaken > 0 || nBar > 0 ? 8 : "auto" }}>{lines.length}</span>
        <span className="chev">›</span>
      </div>
      {open && <div className="betlist">{lines.map((ln, i) => <ContractRow key={i} ln={ln} />)}</div>}
    </div>
  );
}

function contractToLine(c: LiveContract, cfg: Bundle["config"]): Line {
  const s = evalContract(c, cfg);
  // The engine trades ONLY 1X2 — the derived board is research-only, so taken is always
  // false here regardless of edge. meetsBar flags contracts that merely clear the bar.
  return { label: c.label, modelP: s.modelP, mid: s.mid, edge: s.edge, action: s.action, meetsBar: s.meetsBar, taken: false };
}

function LiveGame({ bundle, bankroll, kellyFraction, filters }: {
  bundle: Bundle; bankroll: number; kellyFraction: number; filters: Filters;
}) {
  // The full sim + the two O(n) tick scans only change with these four inputs, so memoize
  // them — a re-render (e.g. another live game refreshing) otherwise re-runs the whole sim.
  const { r, open, midOf, oneX2, priceable, marketOnly } = useMemo(() => {
    const r = runBundle(bundle, { bankroll, kellyFraction, filters });
    const open = r.decisions.filter((d) => d.category === "taken");
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
    // The engine takes only the single strongest actionable leg per tick
    // (one_trade_per_match_tick), so exactly one 1X2 line can be "taken" — not every
    // actionable one. The rest, if actionable, merely "meet the bar".
    const strongest = sigs
      .filter((s) => s.actionable)
      .reduce<(typeof sigs)[number] | null>((best, s) => (best == null || s.netEdge > best.netEdge ? s : best), null);
    const oneX2: Line[] = OUTS.filter((o) => sigs.some((s) => s.outcome === o)).map((o) => {
      const s = sigs.find((x) => x.outcome === o)!;
      const taken = strongest != null && strongest.outcome === o;
      return {
        label: outcomeName(bundle, o), color: OUT_HEX[o],
        modelP: s.modelP, mid: s.implied, edge: s.modelP - s.implied,
        action: s.rawEdge > 0 ? "buy" : s.rawEdge < 0 ? "sell" : null,
        meetsBar: s.actionable && !taken, taken,
      };
    });

    const groups = bundle.all_markets ?? [];
    return { r, open, midOf, oneX2, priceable: groups.filter((g) => g.priceable), marketOnly: groups.filter((g) => !g.priceable) };
  }, [bundle, bankroll, kellyFraction, filters]);

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

      {/* Every possible bet. Only the 1X2 group is tradeable (≤1 leg/tick); the rest is research. */}
      <Group title="Match result (1X2)" sub="the only market the bot trades — at most one leg per tick" lines={oneX2} openDefault />
      {priceable.map((g) => (
        <Group key={g.series} title={g.label} sub="model-priced — research only; the bot doesn't trade this" lines={g.contracts.map((c) => contractToLine(c, bundle.config))} />
      ))}
      {marketOnly.map((g) => (
        <Group key={g.series} title={g.label} sub="market price only — the model can't price this" lines={g.contracts.map((c) => contractToLine(c, bundle.config))} />
      ))}
    </div>
  );
}

function freshness(updatedAt?: number | null): { text: string; stale: boolean } {
  if (!updatedAt) return { text: "live feed offline", stale: true };
  const mins = Math.floor((Date.now() - updatedAt) / 60000);
  if (mins > 5) return { text: `feed stale — updated ${mins}m ago`, stale: true };
  return { text: mins <= 0 ? "updated just now" : `updated ${mins}m ago`, stale: false };
}

function kickoffLabel(iso?: string | null): string {
  if (!iso) return "kickoff TBD";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "kickoff TBD";
  const mins = Math.round((t - Date.now()) / 60000);
  if (mins <= 0) return "kicking off";
  if (mins < 60) return `kickoff in ${mins}m`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `kickoff in ${hrs}h ${mins % 60}m`;
  return `kickoff in ${Math.floor(hrs / 24)}d ${hrs % 24}h`;
}

// Knockout projection series Kalshi doesn't list a market for (model-only).
const KO_PROJECTION_SERIES = new Set(["KXWCMOV", "KXWCTOET", "KXWCTOPENS", "KXWCETSCORE"]);

// A future game projected as a big rectangle. Group stage: a 1X2 headline + every market's
// probability. Knockout: a 2-way "to advance" headline (includes ET + penalties) + the
// method-of-victory / extra-time projections, with the regulation result kept below.
// Read-only — the bot trades nothing before kickoff.
function UpcomingGame({ bundle }: { bundle: Bundle }) {
  const groups = bundle.all_markets ?? [];
  const isKO = !!bundle.is_knockout;
  const headSeries = isKO ? "KXWCADVANCE" : "KXWCGAME";
  const head = groups.find((g) => g.series === headSeries);
  const rest = groups.filter((g) => g.series !== headSeries);

  const headLabels = isKO ? [bundle.home_team, bundle.away_team] : [bundle.home_team, "Draw", bundle.away_team];
  const headColors = isKO ? ["var(--home)", "var(--away)"] : ["var(--home)", "var(--draw)", "var(--away)"];
  const fallback = (head?.contracts ?? []).map((c) => c.model ?? 0);
  const headModel: number[] = isKO ? (bundle.advance ?? fallback) : (bundle.model ?? fallback);
  const headMarket: (number | null)[] = (head?.contracts ?? []).map((c) => c.mid ?? null);
  const hasMarket = headMarket.some((m) => m != null);

  return (
    <div className="panel bigrect">
      <div className="rect-head">
        <span className="livedot" style={{ background: "var(--accent)" }} />
        <span className="rect-teams">
          {bundle.home_team} <span style={{ color: "var(--faint)", fontWeight: 400 }}>vs</span> {bundle.away_team}
        </span>
        {bundle.round && <span className="rect-elo mono">{bundle.round}</span>}
        {bundle.home_elo != null && bundle.away_elo != null && (
          <span className="rect-elo mono">Elo {Math.round(bundle.home_elo)}–{Math.round(bundle.away_elo)}</span>
        )}
        <span className="rect-kick mono">{kickoffLabel(bundle.kickoff)}</span>
      </div>
      {isKO && <div className="note" style={{ margin: "0 0 2px" }}>To advance — includes extra time &amp; penalties</div>}
      <DualBars labels={headLabels} model={headModel} market={headMarket} colors={headColors} showEdge={hasMarket} />
      {rest.map((g) => {
        const title = isKO && g.series === "KXWCGAME" ? "Match result (regulation)" : g.label;
        const sub = KO_PROJECTION_SERIES.has(g.series)
          ? "model projection — Kalshi has no market for this"
          : hasMarket ? "model probability vs market — projection only" : "model probability — no market open yet";
        return (
          <Group key={g.series} title={title} sub={sub}
            lines={g.contracts.map((c) => {
              const ln = contractToLine(c, bundle.config);
              if (isKO && g.series === "KXWCGAME" && /draw|tie/i.test(c.label)) ln.label = "Draw → extra time";
              return ln;
            })} />
        );
      })}
    </div>
  );
}

export function LiveBets({ bundles, upcoming = [], bankroll, kellyFraction, filters, updatedAt }: {
  bundles: Bundle[]; upcoming?: Bundle[]; bankroll: number; kellyFraction: number; filters: Filters; updatedAt?: number | null;
}) {
  const fresh = freshness(updatedAt);
  const hasLive = bundles.length > 0;
  // No live games is normal; only flag the feed as offline when we couldn't even reach
  // the publisher (updatedAt === null) — never pretend a stale/suspended feed is live.
  if (!hasLive && upcoming.length === 0) {
    if (updatedAt == null) {
      return (
        <div className="note" style={{ marginBottom: 18 }}>
          <span className="livedot" style={{ background: "var(--muted)" }} /> Live feed offline — couldn’t reach the publisher. Showing settled games only.
        </div>
      );
    }
    return null;
  }
  return (
    <>
      {hasLive && (
        <div style={{ marginBottom: 26 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, margin: "0 0 6px" }}>
            <h2 style={{ margin: 0, fontSize: 17 }}>Live now</h2>
            <span className="note" style={{ margin: 0 }}>{bundles.length} game{bundles.length > 1 ? "s" : ""} in progress — every Kalshi contract, even the ones the bot ignores.</span>
            <span className="mono" title="how fresh the live feed is (published on a ~60s timer)"
              style={{ marginLeft: "auto", fontSize: 11, color: fresh.stale ? "var(--red)" : "var(--muted)" }}>
              {fresh.text}
            </span>
          </div>
          {bundles.map((b) => (
            <LiveGame key={b.match_id} bundle={b} bankroll={bankroll} kellyFraction={kellyFraction} filters={filters} />
          ))}
        </div>
      )}
      {upcoming.length > 0 && (
        <div style={{ marginBottom: 26 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, margin: "0 0 10px" }}>
            <h2 style={{ margin: 0, fontSize: 17 }}>Upcoming</h2>
            <span className="note" style={{ margin: 0 }}>{upcoming.length} game{upcoming.length > 1 ? "s" : ""} — projected probability of every bet, before kickoff.</span>
          </div>
          {upcoming.map((b) => <UpcomingGame key={b.match_id} bundle={b} />)}
        </div>
      )}
    </>
  );
}
