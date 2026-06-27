# Near-live publisher for a Windows recorder host (PowerShell port of publish_live.sh).
# Run every ~60s via Task Scheduler. Exports the in-progress match and pushes it to
# public Vercel Blob. The Blob token lives in web\.env.local (gitignored), read by
# node --env-file. See docs\windows-server.md.
$ErrorActionPreference = "Stop"
$Repo = Split-Path $PSScriptRoot -Parent          # repo root (this script is in scripts\)
$Out  = Join-Path $env:TEMP "wck-live"

Set-Location $Repo
& "$Repo\.venv\Scripts\wck.exe" export-bundles --live --db "data\wc_tournament.sqlite3" --out $Out
Set-Location (Join-Path $Repo "web")
node --env-file=.env.local "scripts\publish-blob.mjs" (Join-Path $Out "live.json")
