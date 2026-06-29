// Upload a file to the public Vercel Blob store (used by the near-live publisher).
// Reads BLOB_READ_WRITE_TOKEN from env — run with: node --env-file=.env.local ...
//
//   node --env-file=.env.local scripts/publish-blob.mjs <file> [key]
//
// Writes a PUBLIC, stable-path blob (overwritten in place, 60s cache) so the static
// frontend can fetch it directly. Fails LOUDLY but CLEANLY (one line, non-zero exit,
// a heartbeat file) instead of dumping a stack trace every 60s when the store is down.

import { put } from "@vercel/blob";
import { readFileSync, writeFileSync } from "node:fs";
import { basename } from "node:path";

const file = process.argv[2];
if (!file) {
  console.error("usage: node publish-blob.mjs <file> [key]");
  process.exit(1);
}
const key = process.argv[3] || basename(file);
const body = readFileSync(file, "utf8");

// A small heartbeat next to the source file so ops/launchd can see last status without
// scraping logs. ok:false + reason is the signal that the live feed has gone stale.
function heartbeat(ok, reason) {
  try {
    writeFileSync(
      `${file}.publish-status.json`,
      JSON.stringify({ ok, at: new Date().toISOString(), key, reason: reason || null }) + "\n",
    );
  } catch {
    // best-effort; never let the heartbeat itself crash the publisher
  }
}

try {
  const blob = await put(key, body, {
    access: "public",
    allowOverwrite: true,
    addRandomSuffix: false,
    contentType: "application/json",
    cacheControlMaxAge: 60,
  });
  heartbeat(true);
  console.log(`published ${key} (${body.length} bytes) → ${blob.url}`);
} catch (err) {
  const name = err?.name || "Error";
  const msg = err?.message || String(err);
  heartbeat(false, `${name}: ${msg}`);
  // One concise line — no stack trace spam.
  console.error(`publish-blob: FAILED to publish ${key} — ${name}: ${msg}`);
  if (/suspend/i.test(msg)) {
    console.error(
      "  → The Vercel Blob store is SUSPENDED. The live feed will stay stale until you " +
      "either un-suspend it in the Vercel dashboard (Storage → your Blob store), or create " +
      "a new Blob store and update BLOB_READ_WRITE_TOKEN in web/.env.local. " +
      "The web app now shows an honest 'live feed offline' state meanwhile.",
    );
  } else if (!process.env.BLOB_READ_WRITE_TOKEN) {
    console.error("  → BLOB_READ_WRITE_TOKEN is not set. Run with: node --env-file=.env.local ...");
  }
  process.exit(1);
}
