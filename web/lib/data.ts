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
}

export interface Manifest {
  matches: ManifestEntry[];
  aggregate: Record<string, unknown>;
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
  // One missing/truncated bundle must not brick the whole app: keep what loaded,
  // warn about the rest. Only a total failure is fatal.
  const results = await Promise.allSettled(ids.map((id) => loadBundle(id)));
  const ok = results
    .filter((r): r is PromiseFulfilledResult<Bundle> => r.status === "fulfilled")
    .map((r) => r.value);
  const failed = ids.filter((_, i) => results[i].status === "rejected");
  if (failed.length) console.warn(`bundles failed to load (skipped): ${failed.join(", ")}`);
  if (!ok.length && ids.length) throw new Error(`all ${ids.length} bundles failed to load`);
  return ok;
}

// Public store the recorder-host publisher writes the live match into. Migrated from
// Vercel Blob (suspended) to a public Supabase Storage bucket; `{base}/live.json` is the
// public object URL (CORS *, short cache-control). Override via env if the store moves.
const LIVE_FEED_BASE =
  process.env.NEXT_PUBLIC_LIVE_BASE ||
  process.env.NEXT_PUBLIC_BLOB_BASE || // legacy name, still honored
  "https://rgtzktwqpktvbeimrfow.supabase.co/storage/v1/object/public/live";

export interface LiveResult {
  /** False when the FEED FETCH failed (network error / non-2xx / hung request) —
   * distinct from "feed reachable and nothing is live" (ok=true, empty bundles).
   * A transient failure must not be rendered as "no live games": that empties the
   * live list and kicks the viewer off the match they were watching. */
  ok: boolean;
  bundles: Bundle[];
  /** Pre-kickoff projection bundles (`upcoming: true`). Independent of whether any
   * game is live — present even when `bundles` is empty. */
  upcoming: Bundle[];
  /** epoch ms when the publisher generated the feed, or null if unknown/unreachable.
   * Used to show an honest "updated Nm ago / live feed offline" state — the Blob
   * publisher can silently go stale (e.g. a suspended store) and we must not pretend. */
  generatedAt: number | null;
}

const LIVE_FAIL: LiveResult = { ok: false, bundles: [], upcoming: [], generatedAt: null };

/** Poll all in-progress matches (empty if none live) plus upcoming projections.
 * ~1-min lag by design. Reads the multi-game `bundles` array, falling back to the
 * legacy single `bundle` field for older live.json payloads. */
export async function loadLive(): Promise<LiveResult> {
  try {
    const r = await fetch(`${LIVE_FEED_BASE}/live.json?t=${Date.now()}`, {
      cache: "no-store",
      // A hung request must not outlive the 45s poll interval (they'd pile up).
      signal: AbortSignal.timeout(15_000),
    });
    if (!r.ok) return LIVE_FAIL;
    const doc = await r.json();
    const generatedAt = doc?.generated_at ? Date.parse(doc.generated_at) || null : null;
    // Upcoming projections survive even when nothing is live, so read them before the
    // `!doc.live` early-out.
    const upcoming = Array.isArray(doc?.upcoming) ? (doc.upcoming as Bundle[]) : [];
    if (!doc || !doc.live) return { ok: true, bundles: [], upcoming, generatedAt };
    const bundles = Array.isArray(doc.bundles)
      ? (doc.bundles as Bundle[])
      : doc.bundle ? [doc.bundle as Bundle] : [];
    return { ok: true, bundles, upcoming, generatedAt };
  } catch {
    return LIVE_FAIL;
  }
}
