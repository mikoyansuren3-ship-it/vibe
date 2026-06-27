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
  return Promise.all(ids.map((id) => loadBundle(id)));
}

// Public Vercel Blob store the recorder-host publisher writes the live match into.
const BLOB_BASE =
  process.env.NEXT_PUBLIC_BLOB_BASE || "https://tgk7qzxearwylitn.public.blob.vercel-storage.com";

/** Poll all in-progress matches (empty if none live). ~1-min lag by design.
 * Reads the multi-game `bundles` array, falling back to the legacy single
 * `bundle` field for older live.json payloads. */
export async function loadLive(): Promise<Bundle[]> {
  try {
    const r = await fetch(`${BLOB_BASE}/live.json?t=${Date.now()}`, { cache: "no-store" });
    if (!r.ok) return [];
    const doc = await r.json();
    if (!doc || !doc.live) return [];
    if (Array.isArray(doc.bundles)) return doc.bundles as Bundle[];
    return doc.bundle ? [doc.bundle as Bundle] : [];
  } catch {
    return [];
  }
}
