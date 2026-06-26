#!/bin/bash
# Near-live publisher: export the in-progress match bundle and push it to public
# Vercel Blob. Run on a ~60s launchd timer on the recorder host (com.wckalshi.livepublish).
# The Blob token lives in web/.env.local (gitignored), read via node --env-file.
set -euo pipefail
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"  # launchd has a minimal PATH
REPO="/Users/Suro/Vibe"
OUT="/tmp/wck-live"

cd "$REPO"
# shellcheck disable=SC1091
source .venv/bin/activate
wck export-bundles --live --db data/wc_tournament.sqlite3 --out "$OUT"
cd "$REPO/web"
node --env-file=.env.local scripts/publish-blob.mjs "$OUT/live.json"
