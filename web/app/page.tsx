"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Sidebar, type Mode, type TabId } from "../components/Sidebar";
import { Terminal } from "../components/Terminal";
import { Sandbox } from "../components/Sandbox";
import { Overview } from "../components/tabs/Overview";
import { Bets } from "../components/tabs/Bets";
import { Markets } from "../components/tabs/Markets";
import { Games } from "../components/tabs/Games";
import { About } from "../components/tabs/About";
import { runMany } from "../lib/sim/engine";
import { loadAllBundles, loadLive, loadManifest, type Manifest } from "../lib/data";
import type { Bundle, Filters } from "../lib/sim/types";

const NO_FILTERS: Filters = { sellOnly: false, disableBuys: false, maxEntryMinute: null };
const TABS: TabId[] = ["overview", "replay", "bets", "markets", "sandbox", "games", "about"];

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
  const [liveBundles, setLiveBundles] = useState<Bundle[]>([]);
  const [upcomingBundles, setUpcomingBundles] = useState<Bundle[]>([]);
  const [liveUpdatedAt, setLiveUpdatedAt] = useState<number | null>(null);

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

  // Near-live: poll all in-progress matches from Vercel Blob (~1 min lag by design).
  const didAutoLive = useRef(false);
  useEffect(() => {
    let alive = true;
    const tick = () => loadLive().then(({ bundles: lb, upcoming, generatedAt }) => {
      if (!alive) return;
      setLiveBundles(lb);
      setUpcomingBundles(upcoming);
      setLiveUpdatedAt(generatedAt);
    });
    tick();
    const h = setInterval(tick, 45000);
    return () => { alive = false; clearInterval(h); };
  }, []);

  // When live games first appear, surface the most-recent one by default (once,
  // so we never yank the selection after the user starts navigating).
  useEffect(() => {
    if (!didAutoLive.current && liveBundles.length > 0) {
      didAutoLive.current = true;
      setSelectedId(liveBundles[0].match_id);
    }
  }, [liveBundles]);

  const setMode = (m: Mode) => { setModeState(m); localStorage.setItem("wck-mode", m); };
  const adv = mode === "advanced";

  // Replay shows live matches (top) plus all settled games — but a match that has just
  // settled can appear in BOTH the live feed and the settled manifest, so dedup by
  // match_id with the live copy winning (it carries the in-progress state).
  const replayBundles = useMemo(() => {
    const liveIds = new Set(liveBundles.map((b) => b.match_id));
    return [...liveBundles, ...bundles.filter((b) => !liveIds.has(b.match_id))];
  }, [liveBundles, bundles]);
  const ids = replayBundles.map((b) => b.match_id);
  const posn = ids.indexOf(selectedId);
  const selected = useMemo(() => replayBundles.find((b) => b.match_id === selectedId), [replayBundles, selectedId]);
  const isLive = !!selected?.live;
  // If the selected match vanished (e.g. a live game just settled), fall back to a settled one.
  useEffect(() => {
    if (selectedId && replayBundles.length && !replayBundles.some((b) => b.match_id === selectedId)) {
      setSelectedId(bundles[bundles.length - 1]?.match_id ?? "");
    }
  }, [replayBundles, selectedId, bundles]);
  const totalBets = useMemo(() => (bundles.length ? runMany(bundles).nFills : undefined), [bundles]);
  const pick = (id: string, to: TabId = "replay") => { setSelectedId(id); setTab(to); };

  const ready = manifest && bundles.length > 0;

  return (
    <div className="app">
      <Sidebar active={tab} setActive={setTab} mode={mode} setMode={setMode} betCount={totalBets} liveActive={liveBundles.length > 0} />
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
                  {replayBundles.map((b) => (
                    <option key={b.match_id} value={b.match_id}>
                      {b.live ? "● LIVE  " : ""}{b.home_team} {b.final_score[0]}–{b.final_score[1]} {b.away_team}
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
          <Bets bundles={bundles} liveBundles={liveBundles} upcomingBundles={upcomingBundles} liveUpdatedAt={liveUpdatedAt} selectedId={selectedId} bankroll={bankroll} kellyFraction={kellyFraction} filters={filters} adv={adv} />
        )}

        {ready && tab === "sandbox" && adv && (
          <div style={{ maxWidth: 600 }}>
            <div className="tabhead"><h1>Sandbox</h1><div className="sub">Tune the strategy and watch the edge move across all {bundles.length} games.</div></div>
            <Sandbox bundles={bundles} bankroll={bankroll} setBankroll={setBankroll} kellyFraction={kellyFraction} setKelly={setKelly} filters={filters} setFilters={setFilters} />
          </div>
        )}

        {ready && tab === "markets" && <Markets bundles={bundles} adv={adv} />}
        {ready && tab === "games" && <Games bundles={bundles} adv={adv} onPick={(id) => pick(id, "replay")} />}
        {ready && tab === "about" && <About />}
      </main>
    </div>
  );
}
