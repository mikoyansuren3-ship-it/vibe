#!/usr/bin/env bash
# One-command health check for the World Cup live recorder.
#   bash scripts/recorder_status.sh
cd "$(dirname "$0")/.." || exit 1

echo "== recorder agent =="
line=$(launchctl list | grep wckalshi)
if [ -z "$line" ]; then
  echo "  NOT LOADED — agent isn't installed/running"
else
  pid=$(echo "$line" | awk '{print $1}')
  exit_code=$(echo "$line" | awk '{print $2}')
  if [ "$pid" = "-" ]; then
    echo "  LOADED but NOT running (last exit $exit_code)"
  else
    echo "  RUNNING (pid $pid, last exit $exit_code)"
  fi
fi

echo "== sleep guard =="
if pmset -g assertions 2>/dev/null | grep -q caffeinate; then
  echo "  caffeinate active — won't idle-sleep while recording"
else
  echo "  no caffeinate assertion (ok if no recorder running)"
fi

echo "== last recorder log line =="
tail -n 1 data/recorder.err.log 2>/dev/null || echo "  (no log yet)"

echo "== captured matches (freshness) =="
.venv/bin/python - <<'PY'
from wc_kalshi.models.db import Database
from wc_kalshi.util import utcnow
try:
    db = Database("sqlite:///data/wc_tournament.sqlite3")
    ids = db.match_ids()
    if not ids:
        print("  (no matches captured yet)")
    for mid in ids:
        ms = db.iter_match_snapshots(mid)
        last = ms[-1]
        settled = any(s.period.value == "FT" for s in ms)
        age = (utcnow() - last.ts).total_seconds()
        flag = " [SETTLED]" if settled else ""
        print(f"  {ms[0].home_team} vs {ms[0].away_team}: {len(ms)} snaps, "
              f"last {last.minute}' {last.home_score}-{last.away_score}{flag} ({age:.0f}s ago)")
except Exception as exc:
    print("  db error:", exc)
PY
