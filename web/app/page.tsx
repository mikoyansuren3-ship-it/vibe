"use client";

import { useEffect, useMemo, useState } from "react";
import { Sandbox } from "../components/Sandbox";
import { Terminal } from "../components/Terminal";
import { loadAllBundles, loadManifest, type Manifest } from "../lib/data";
import type { Bundle, Filters } from "../lib/sim/types";

const DEFAULT_FILTERS: Filters = { sellOnly: false, disableBuys: false, maxEntryMinute: null };

export default function Page() {
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [bundles, setBundles] = useState<Bundle[]>([]);
  const [selectedId, setSelectedId] = useState<string>("");
  const [error, setError] = useState<string | null>(null);

  // shared strategy config (drives BOTH the terminal and the sandbox)
  const [bankroll, setBankroll] = useState(100);
  const [kellyFraction, setKelly] = useState(0.25);
  const [filters, setFilters] = useState<Filters>(DEFAULT_FILTERS);

  useEffect(() => {
    (async () => {
      try {
        const m = await loadManifest();
        setManifest(m);
        const ids = m.matches.map((x) => x.match_id);
        const bs = await loadAllBundles(ids);
        setBundles(bs);
        setSelectedId(m.live_match_id || ids[ids.length - 1] || "");
      } catch (e) {
        setError(String(e));
      }
    })();
  }, []);

  const selected = useMemo(() => bundles.find((b) => b.match_id === selectedId), [bundles, selectedId]);
  const isLive = manifest?.live_match_id === selectedId && !!selectedId;

  return (
    <div className="wrap">
      <header className="top">
        <div>
          <h1>World Cup × Kalshi — Paper Gambling Simulator</h1>
          <div className="sub">
            Watch the in-play model paper-bet recorded matches, and tune the strategy live. Fake money, no edge — a lab, not a tipster.
          </div>
        </div>
        <div style={{ marginLeft: "auto" }} className="row">
          <select value={selectedId} onChange={(e) => setSelectedId(e.target.value)}>
            {(manifest?.matches ?? []).map((m) => (
              <option key={m.match_id} value={m.match_id}>
                {m.home_team} {m.final_score[0]}–{m.final_score[1]} {m.away_team}
                {manifest?.live_match_id === m.match_id ? "  ● live" : ""}
              </option>
            ))}
          </select>
        </div>
      </header>

      {error && <div className="panel" style={{ color: "var(--red)" }}>Failed to load bundles: {error}</div>}
      {!error && !selected && <div className="panel">Loading recorded games…</div>}

      {selected && (
        <div className="grid">
          <Terminal
            bundle={selected}
            bankroll={bankroll}
            kellyFraction={kellyFraction}
            filters={filters}
            live={isLive}
          />
          <Sandbox
            bundles={bundles}
            bankroll={bankroll}
            setBankroll={setBankroll}
            kellyFraction={kellyFraction}
            setKelly={setKelly}
            filters={filters}
            setFilters={setFilters}
          />
        </div>
      )}
    </div>
  );
}
