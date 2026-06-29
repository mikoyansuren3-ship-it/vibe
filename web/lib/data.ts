// Load bundles + manifest. Settled games are static under /bundles; the live
// (in-progress) game, when present, is overwritten in place by the recorder-host
// publisher (Phase 4) and fetched with cache-busting.

import type { Bundle } from "./sim/types";

export interface ManifestEntry {
  match_id: string;
  home_team: string;
  away_team: string;
  outcome: "H" | "D" | "A";
  final_score: [number, number];
  n_ticks: number;
  n_fills: number;
  has_derived?: boolean;
  live?: boolean;
  preoff_is_kickoff?: boolean;
  first_capture_minute?: number;
}

export interface OutcomeCalibration {
  mean_predicted: number;
  empirical_freq: number;
  bias: number;
  ece: number;
}

/** The aggregate metrics block (manifest.aggregate = kelly; manifest.edge_eval = fixed). */
export interface Aggregate {
  n_matches: number;
  n_fills: number;
  stake_mode: string;
  avg_clv_preoff: number;
  avg_clv_5m: number;
  avg_clv: number;
  clv_ci_preoff: [number, number];
  n_clusters_preoff: number;
  edge_verdict: "negative" | "positive" | "indistinguishable_from_zero" | string;
  realized_pnl: number;
  roi: number;
  t_stat: number;
  pnl_ci: [number, number];
  calibration: { n: number; brier: number; log_loss: number; ece: number; calibration_factor: number };
  calibration_per_outcome?: Record<string, OutcomeCalibration>;
}

export interface CoverageSummary {
  n_matches: number;
  n_kickoff: number;
  n_mid_game_start: number;
  note?: string;
}

export interface Provenance {
  generated_at: string;
  model_git_sha: string | null;
  config_sha256: string;
  stake_mode: string;
  db?: { name?: string; size_bytes?: number; sha256?: string };
}

export interface Manifest {
  matches: ManifestEntry[];
  aggregate: Record<string, unknown>;
  edge_eval?: Record<string, unknown>;
  coverage_summary?: CoverageSummary;
  provenance?: Provenance;
  config: Record<string, unknown>;
  live_match_id?: string | null;
}

export async function loadManifest(): Promise<Manifest> {
  const r = await fetch("/bundles/manifest.json", { cache: "no-store" });
  if (!r.ok) throw new Error(`manifest ${r.status}`);
  return r.json();
}

export async function loadBundle(matchId: string, opts: { live?: boolean } = {}): Promise<Bundle> {
  const url = opts.live ? `/bundles/${matchId}.json?t=${Date.now()}` : `/bundles/${matchId}.json`;
  const r = await fetch(url, opts.live ? { cache: "no-store" } : {});
  if (!r.ok) throw new Error(`bundle ${matchId} ${r.status}`);
  return r.json();
}

export async function loadAllBundles(ids: string[]): Promise<Bundle[]> {
  return Promise.all(ids.map((id) => loadBundle(id)));
}

// Public Vercel Blob store the recorder-host publisher writes the live match into.
const BLOB_BASE =
  process.env.NEXT_PUBLIC_BLOB_BASE || "https://tgk7qzxearwylitn.public.blob.vercel-storage.com";

export interface LiveResult {
  bundles: Bundle[];
  /** epoch ms when the publisher generated the feed, or null if unknown/unreachable.
   * Used to show an honest "updated Nm ago / live feed offline" state — the Blob
   * publisher can silently go stale (e.g. a suspended store) and we must not pretend. */
  generatedAt: number | null;
}

/** Poll all in-progress matches (empty if none live). ~1-min lag by design.
 * Reads the multi-game `bundles` array, falling back to the legacy single
 * `bundle` field for older live.json payloads. */
export async function loadLive(): Promise<LiveResult> {
  try {
    const r = await fetch(`${BLOB_BASE}/live.json?t=${Date.now()}`, { cache: "no-store" });
    if (!r.ok) return { bundles: [], generatedAt: null };
    const doc = await r.json();
    const generatedAt = doc?.generated_at ? Date.parse(doc.generated_at) || null : null;
    if (!doc || !doc.live) return { bundles: [], generatedAt };
    const bundles = Array.isArray(doc.bundles)
      ? (doc.bundles as Bundle[])
      : doc.bundle ? [doc.bundle as Bundle] : [];
    return { bundles, generatedAt };
  } catch {
    return { bundles: [], generatedAt: null };
  }
}
