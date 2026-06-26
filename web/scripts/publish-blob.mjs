// Upload a file to the public Vercel Blob store (used by the near-live publisher).
// Reads BLOB_READ_WRITE_TOKEN from env — run with: node --env-file=.env.local ...
//
//   node --env-file=.env.local scripts/publish-blob.mjs <file> [key]
//
// Writes a PUBLIC, stable-path blob (overwritten in place, 60s cache) so the static
// frontend can fetch it directly.

import { put } from "@vercel/blob";
import { readFileSync } from "node:fs";
import { basename } from "node:path";

const file = process.argv[2];
if (!file) {
  console.error("usage: node publish-blob.mjs <file> [key]");
  process.exit(1);
}
const key = process.argv[3] || basename(file);
const body = readFileSync(file, "utf8");

const blob = await put(key, body, {
  access: "public",
  allowOverwrite: true,
  addRandomSuffix: false,
  contentType: "application/json",
  cacheControlMaxAge: 60,
});
console.log(`published ${key} (${body.length} bytes) → ${blob.url}`);
