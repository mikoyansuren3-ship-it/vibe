"use client";

import { useEffect, useMemo, useState } from "react";
import { Sandbox } from "../components/Sandbox";
import { Terminal } from "../components/Terminal";
import { cls, money, signed } from "../components/bits";
import { runMany } from "../lib/sim/engine";
import { loadAllBundles, loadManifest, type Manifest } from "../lib/data";
import type { Bundle, Filters } from "../lib/sim/types";

const NO_FILTERS: Filters = { sellOnly: false, disableBuys: false, maxEntryMinute: null };

function Scoreboard({ m, bundles }: { m: Manifest; bundles: Bundle[] }) {
  // Use the SAME client-side engine the sandbox/terminal use, so every number on
  // the page is consistent. Brier comes from the Python calibration (sizing-independent).
  const base = useMemo(() => runMany(bundles), [bundles]);
  const brier = (m.aggregate as { calibration?: Record<string, number> }).calibration?.brier ?? 0;
  const rec = m.matches.reduce((acc, x) => { acc[x.outcome]++; return acc; }, { H: 0, D: 0, A: 0 } as Record<string, number>);
  const cells: { k: string; v: string; c?: string; sub?: string }[] = [
    { k: "Games", v: String(m.matches.length), sub: `${base.nFills} paper fills` },
    { k: "Paper P&L", v: money(base.pnl), c: cls(base.pnl), sub: `$100 / game, summed` },
    { k: "Pre-off CLV", v: signed(base.clvPreoff ?? 0, 4), c: cls(base.clvPreoff ?? 0), sub: "vs opening line" },
    { k: "Brier", v: brier.toFixed(4), sub: "uniform ≈ 0.667" },
    { k: "Results", v: `${rec.H}–${rec.D}–${rec.A}`, sub: "home–draw–away" },
  ];
  return (
    <div className="scoreboard">
      {cells.map((c) => (
        <div className="cell" key={c.k}>
          <span className="k">{c.k}</span>
          <span className={`v ${c.c ?? ""}`}>{c.v}</span>
          {c.sub && <span className="sub2">{c.sub}</span>}
        </div>
      ))}
    </div>
  );
}

export default function Page() {
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [bundles, setBundles] = useState<Bundle[]>([]);
  const [selectedId, setSelectedId] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [bankroll, setBankroll] = useState(100);
  const [kellyFraction, setKelly] = useState(0.25);
  const [filters, setFilters] = useState<Filters>(NO_FILTERS);

  useEffect(() => {
    (async () => {
      try {
        const m = await loadManifest();
        setManifest(m);
        const ids = m.matches.map((x) => x.match_id);
        setBundles(await loadAllBundles(ids));
        setSelectedId(m.live_match_id || ids[ids.length - 1] || "");
      } catch (e) { setError(String(e)); }
    })();
  }, []);

  const ids = manifest?.matches.map((x) => x.match_id) ?? [];
  const pos = ids.indexOf(selectedId);
  const selected = useMemo(() => bundles.find((b) => b.match_id === selectedId), [bundles, selectedId]);
  const isLive = manifest?.live_match_id === selectedId && !!selectedId;

  return (
    <div className="wrap">
      <header className="top">
        <div>
          <div className="brandrow">
            <span className="logo">K</span>
            <h1>World Cup × Kalshi — Paper Gambling Simulator</h1>
          </div>
          <div className="sub">Watch the in-play model paper-bet real recorded matches, then tune the strategy and see the edge move — live, in your browser.</div>
        </div>
        {manifest && (
          <div className="gamenav">
            <button className="nav" onClick={() => setSelectedId(ids[Math.max(0, pos - 1)])} disabled={pos <= 0}>‹</button>
            <select value={selectedId} onChange={(e) => setSelectedId(e.target.value)}>
              {manifest.matches.map((mm) => (
                <option key={mm.match_id} value={mm.match_id}>
                  {mm.home_team} {mm.final_score[0]}–{mm.final_score[1]} {mm.away_team}{manifest.live_match_id === mm.match_id ? "  ● live" : ""}
                </option>
              ))}
            </select>
            <button className="nav" onClick={() => setSelectedId(ids[Math.min(ids.length - 1, pos + 1)])} disabled={pos >= ids.length - 1}>›</button>
          </div>
        )}
      </header>

      {manifest && bundles.length > 0 && <Scoreboard m={manifest} bundles={bundles} />}
      <div className="legend">
        <b>Fake money, no edge — a lab, not a tipster.</b> Pre-off <b>CLV</b> (closing-line value) is the rigorous metric: did a bet enter better than the market&apos;s opening price? Positive beats the line; this model sits slightly negative — well-calibrated, but it doesn&apos;t beat Kalshi.
      </div>

      {error && <div className="panel" style={{ color: "var(--red)" }}>Failed to load: {error}</div>}
      {!error && !selected && <div className="loading">Loading recorded games…</div>}

      {selected && (
        <div className="grid">
          <Terminal bundle={selected} bankroll={bankroll} kellyFraction={kellyFraction} filters={filters} live={isLive} />
          <Sandbox bundles={bundles} bankroll={bankroll} setBankroll={setBankroll} kellyFraction={kellyFraction} setKelly={setKelly} filters={filters} setFilters={setFilters} />
        </div>
      )}
    </div>
  );
}
