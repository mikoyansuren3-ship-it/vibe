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

  // Near-live: poll all in-progress matches (~1 min lag by design).
  const didAutoLive = useRef(false);
  const userPicked = useRef(false); // the viewer has chosen a game — never auto-yank them after
  const lastGen = useRef(0); // newest generated_at applied — rejects out-of-order responses
  const liveFails = useRef(0);
  useEffect(() => {
    let alive = true;
    const tick = () => loadLive().then((res) => {
      if (!alive) return;
      if (!res.ok) {
        // Transient fetch failure: keep showing the last known-good feed — one dropped
        // request over a 2h match must not empty the live list and yank the viewer off
        // the game. Only a sustained outage (~3 min) clears it, and liveUpdatedAt goes
        // null so the UI shows its honest "live feed offline" state.
        liveFails.current += 1;
        if (liveFails.current >= 4) {
          setLiveBundles([]);
          setLiveUpdatedAt(null);
        }
        return;
      }
      // A slow response can land after a newer one: applying it would move the score /
      // playhead backwards. generated_at is monotonic at the publisher — enforce it here.
      if (res.generatedAt !== null && res.generatedAt < lastGen.current) return;
      if (res.generatedAt !== null) lastGen.current = res.generatedAt;
      liveFails.current = 0;
      setLiveBundles(res.bundles);
      setUpcomingBundles(res.upcoming);
      setLiveUpdatedAt(res.generatedAt);
    });
    tick();
    const h = setInterval(tick, 45000);
    return () => { alive = false; clearInterval(h); };
  }, []);

  // When live games first appear, surface the most-recent one by default — but only if the
  // viewer hasn't already picked a game. Without the userPicked guard, a viewer who opened a
  // settled match BEFORE any live game appeared gets yanked away the moment one does.
  useEffect(() => {
    if (!didAutoLive.current && !userPicked.current && liveBundles.length > 0) {
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
  // Any explicit game choice by the viewer flips userPicked, so the live auto-select won't
  // override it later. System-driven selects (initial load, vanished-selection fallback) use
  // setSelectedId directly and don't flip it.
  const selectGame = (id: string) => { userPicked.current = true; setSelectedId(id); };
  const pick = (id: string, to: TabId = "replay") => { selectGame(id); setTab(to); };

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
                <button className="nav" onClick={() => selectGame(ids[Math.max(0, posn - 1)])} disabled={posn <= 0}>‹</button>
                <select value={selectedId} onChange={(e) => selectGame(e.target.value)}>
                  {replayBundles.map((b) => (
                    <option key={b.match_id} value={b.match_id}>
                      {b.live ? "● LIVE  " : ""}{b.home_team} {b.final_score[0]}–{b.final_score[1]} {b.away_team}
                    </option>
                  ))}
                </select>
                <button className="nav" onClick={() => selectGame(ids[Math.min(ids.length - 1, posn + 1)])} disabled={posn >= ids.length - 1}>›</button>
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
