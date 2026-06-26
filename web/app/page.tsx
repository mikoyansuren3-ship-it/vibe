"use client";

import { useEffect, useMemo, useState } from "react";
import { Sidebar, type Mode, type TabId } from "../components/Sidebar";
import { Terminal } from "../components/Terminal";
import { Sandbox } from "../components/Sandbox";
import { Overview } from "../components/tabs/Overview";
import { Bets } from "../components/tabs/Bets";
import { Games } from "../components/tabs/Games";
import { About } from "../components/tabs/About";
import { runMany } from "../lib/sim/engine";
import { loadAllBundles, loadManifest, type Manifest } from "../lib/data";
import type { Bundle, Filters } from "../lib/sim/types";

const NO_FILTERS: Filters = { sellOnly: false, disableBuys: false, maxEntryMinute: null };
const TABS: TabId[] = ["overview", "replay", "bets", "sandbox", "games", "about"];

export default function Page() {
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [bundles, setBundles] = useState<Bundle[]>([]);
  const [selectedId, setSelectedId] = useState<string>("");
  const [error, setError] = useState<string | null>(null);

  const [tab, setTab] = useState<TabId>("overview");
  const [mode, setModeState] = useState<Mode>("basic");
  const [bankroll, setBankroll] = useState(100);
  const [kellyFraction, setKelly] = useState(0.25);
  const [filters, setFilters] = useState<Filters>(NO_FILTERS);

  // load data
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

  // restore mode + tab
  useEffect(() => {
    const sm = localStorage.getItem("wck-mode");
    if (sm === "advanced" || sm === "basic") setModeState(sm);
    const h = (typeof location !== "undefined" ? location.hash.replace("#", "") : "") as TabId;
    if (TABS.includes(h)) setTab(h);
  }, []);
  useEffect(() => { if (typeof location !== "undefined") history.replaceState(null, "", `#${tab}`); }, [tab]);
  useEffect(() => { if (mode === "basic" && tab === "sandbox") setTab("overview"); }, [mode, tab]);

  const setMode = (m: Mode) => { setModeState(m); localStorage.setItem("wck-mode", m); };
  const adv = mode === "advanced";

  const ids = manifest?.matches.map((x) => x.match_id) ?? [];
  const posn = ids.indexOf(selectedId);
  const selected = useMemo(() => bundles.find((b) => b.match_id === selectedId), [bundles, selectedId]);
  const isLive = manifest?.live_match_id === selectedId && !!selectedId;
  const totalBets = useMemo(() => (bundles.length ? runMany(bundles).nFills : undefined), [bundles]);
  const pick = (id: string, to: TabId = "replay") => { setSelectedId(id); setTab(to); };

  const ready = manifest && bundles.length > 0;

  return (
    <div className="app">
      <Sidebar active={tab} setActive={setTab} mode={mode} setMode={setMode} betCount={totalBets} />
      <main className="main">
        {error && <div className="panel" style={{ color: "var(--red)" }}>Failed to load: {error}</div>}
        {!error && !ready && <div className="loading">Loading recorded games…</div>}

        {ready && tab === "overview" && <Overview manifest={manifest!} bundles={bundles} adv={adv} onPick={pick} />}

        {ready && tab === "replay" && selected && (
          <div>
            <div className="tabhead">
              <h1>Replay</h1>
              <div className="sub">Watch the bot paper-bet a game, minute by minute.</div>
              <div className="gamenav" style={{ marginLeft: 0 }}>
                <button className="nav" onClick={() => setSelectedId(ids[Math.max(0, posn - 1)])} disabled={posn <= 0}>‹</button>
                <select value={selectedId} onChange={(e) => setSelectedId(e.target.value)}>
                  {manifest!.matches.map((mm) => (
                    <option key={mm.match_id} value={mm.match_id}>
                      {mm.home_team} {mm.final_score[0]}–{mm.final_score[1]} {mm.away_team}{manifest!.live_match_id === mm.match_id ? "  ● live" : ""}
                    </option>
                  ))}
                </select>
                <button className="nav" onClick={() => setSelectedId(ids[Math.min(ids.length - 1, posn + 1)])} disabled={posn >= ids.length - 1}>›</button>
              </div>
            </div>
            <Terminal bundle={selected} bankroll={bankroll} kellyFraction={kellyFraction} filters={filters} live={isLive} adv={adv} />
          </div>
        )}

        {ready && tab === "bets" && (
          <Bets bundles={bundles} selectedId={selectedId} bankroll={bankroll} kellyFraction={kellyFraction} filters={filters} adv={adv} />
        )}

        {ready && tab === "sandbox" && adv && (
          <div style={{ maxWidth: 600 }}>
            <div className="tabhead"><h1>Sandbox</h1><div className="sub">Tune the strategy and watch the edge move across all {bundles.length} games.</div></div>
            <Sandbox bundles={bundles} bankroll={bankroll} setBankroll={setBankroll} kellyFraction={kellyFraction} setKelly={setKelly} filters={filters} setFilters={setFilters} />
          </div>
        )}

        {ready && tab === "games" && <Games bundles={bundles} adv={adv} onPick={(id) => pick(id, "replay")} />}
        {ready && tab === "about" && <About />}
      </main>
    </div>
  );
}
