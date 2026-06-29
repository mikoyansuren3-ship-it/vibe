#!/bin/bash
# Near-live publisher: export the in-progress match bundle and push it to a PUBLIC
# Supabase Storage bucket (replaces the suspended Vercel Blob). Run on a ~60s launchd
# timer on the recorder host (com.wckalshi.livepublish).
# SUPABASE_URL / SUPABASE_SERVICE_KEY live in web/.env.local (gitignored), read via
# node --env-file. To revert to Vercel Blob, point the last line back at
# publish-blob.mjs (BLOB_READ_WRITE_TOKEN is still in .env.local).
set -euo pipefail
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"  # launchd has a minimal PATH
REPO="/Users/Suro/Vibe"
OUT="/tmp/wck-live"

cd "$REPO"
# shellcheck disable=SC1091
source .venv/bin/activate
wck export-bundles --live --db data/wc_tournament.sqlite3 --out "$OUT"
cd "$REPO/web"
node --env-file=.env.local scripts/publish-supabase.mjs "$OUT/live.json"
