// Upload a file to a PUBLIC Supabase Storage bucket (live-feed transport, replaces
// the suspended Vercel Blob). Run with: node --env-file=.env.local ...
//
//   node --env-file=.env.local scripts/publish-supabase.mjs <file> [key]
//
// Env (in web/.env.local, gitignored):
//   SUPABASE_URL          e.g. https://abcd1234.supabase.co   (no trailing slash)
//   SUPABASE_SERVICE_KEY  service_role / secret key — server-side only, bypasses RLS
//   SUPABASE_BUCKET       optional, defaults to "live"
//
// Overwrites a stable object path (x-upsert) with a short cache-control so the static
// frontend can fetch it directly from the public object URL:
//   {SUPABASE_URL}/storage/v1/object/public/{bucket}/{key}
// Reads with `?t=<now>` cache-bust on the client keep it ~live.
//
// Fails LOUDLY but CLEANLY (one line, non-zero exit, a heartbeat file) instead of
// dumping a stack trace every 60s when the store is misconfigured/down.

import { readFileSync, writeFileSync } from "node:fs";
import { basename } from "node:path";

const file = process.argv[2];
if (!file) {
  console.error("usage: node publish-supabase.mjs <file> [key]");
  process.exit(1);
}
const key = process.argv[3] || basename(file);
const body = readFileSync(file, "utf8");

const RAW_URL = process.env.SUPABASE_URL || "";
const SUPABASE_URL = RAW_URL.replace(/\/+$/, ""); // tolerate a trailing slash
const SERVICE_KEY = process.env.SUPABASE_SERVICE_KEY || "";
const BUCKET = process.env.SUPABASE_BUCKET || "live";

// A small heartbeat next to the source file so ops/launchd can see last status without
// scraping logs. ok:false + reason is the signal that the live feed has gone stale.
function heartbeat(ok, reason) {
  try {
    writeFileSync(
      `${file}.publish-status.json`,
      JSON.stringify({ ok, at: new Date().toISOString(), key, bucket: BUCKET, reason: reason || null }) + "\n",
    );
  } catch {
    // best-effort; never let the heartbeat itself crash the publisher
  }
}

if (!SUPABASE_URL || !SERVICE_KEY) {
  const missing = [!SUPABASE_URL && "SUPABASE_URL", !SERVICE_KEY && "SUPABASE_SERVICE_KEY"]
    .filter(Boolean)
    .join(" + ");
  heartbeat(false, `missing env: ${missing}`);
  console.error(`publish-supabase: ${missing} not set. Run with: node --env-file=.env.local ...`);
  process.exit(1);
}

// POST with x-upsert:true overwrites the object in place (stable public path).
const endpoint = `${SUPABASE_URL}/storage/v1/object/${BUCKET}/${encodeURIComponent(key)}`;
const publicUrl = `${SUPABASE_URL}/storage/v1/object/public/${BUCKET}/${encodeURIComponent(key)}`;

try {
  const res = await fetch(endpoint, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${SERVICE_KEY}`,
      apikey: SERVICE_KEY,
      "Content-Type": "application/json",
      "Cache-Control": "max-age=5",
      "x-upsert": "true",
    },
    body,
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(`HTTP ${res.status} ${res.statusText} ${detail.slice(0, 200)}`);
  }
  heartbeat(true);
  console.log(`published ${key} (${body.length} bytes) → ${publicUrl}`);
} catch (err) {
  const name = err?.name || "Error";
  const msg = err?.message || String(err);
  heartbeat(false, `${name}: ${msg}`);
  console.error(`publish-supabase: FAILED to publish ${key} — ${name}: ${msg}`);
  if (/\b40[13]\b|invalid|jwt|apikey/i.test(msg)) {
    console.error(
      "  → Auth rejected. Check SUPABASE_SERVICE_KEY is the service_role/secret key " +
      "(not the anon/publishable key) and SUPABASE_URL matches the project.",
    );
  } else if (/\b404\b|bucket/i.test(msg)) {
    console.error(
      `  → Bucket "${BUCKET}" not found. Create a PUBLIC bucket named "${BUCKET}" in the ` +
      "Supabase dashboard (Storage → New bucket → Public), or set SUPABASE_BUCKET.",
    );
  }
  process.exit(1);
}
