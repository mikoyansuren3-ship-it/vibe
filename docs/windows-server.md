# Running the recorder on a Windows desktop (24/7, off the MacBook)

The collector only polls **out** to API-Football + Kalshi and pushes **out** to Vercel
Blob — there is **no inbound traffic**, so no ports to open and nothing exposed. The
website stays on Vercel; this box just feeds it.

## What runs here
1. **Recorder** — `wck record --source prod` (long-running). Polls live match state +
   Kalshi prices, settles finished games, writes `data\wc_tournament.sqlite3`.
2. **Publisher** — `scripts\publish_live.ps1` every ~60s → pushes the in-progress match
   to public Blob (`live.json`) so the site shows it near-live.

## One-time setup

### 1. Install prerequisites
- **Python 3.12** (python.org — check "Add to PATH").
- **Node 24** (nodejs.org).
- **Git** (git-scm.com).

### 2. Clone + build
```powershell
git clone https://github.com/mikoyansuren3-ship-it/vibe.git
cd vibe
python -m venv .venv
.\.venv\Scripts\pip install -e .
cd web; npm install; cd ..
```

### 3. Copy the 4 secret files from the Mac (they're gitignored — not in the repo)
Copy these to the same paths on Windows:
| Mac | Windows |
|---|---|
| `.env` | `vibe\.env` |
| `config/local.yaml` | `vibe\config\local.yaml` |
| `web/.env.local` | `vibe\web\.env.local` |
| `~/.kalshi/prod.pem` | `C:\Users\<you>\.kalshi\prod.pem` |

Then **edit `vibe\.env`** so the Kalshi key path is the Windows path:
```
KALSHI_PRIVATE_KEY_PATH=C:\Users\<you>\.kalshi\prod.pem
```
(If `config\local.yaml` also sets a key path, update it the same way.)

### 4. Smoke-test
```powershell
.\.venv\Scripts\wck record --source prod --duration 60   # captures ~1 min, then exits
.\scripts\publish_live.ps1                                # should print "published live.json → ..."
```

### 5. Never sleep
Settings → System → Power → **Screen and sleep → When plugged in, put to sleep: Never.**
(If it sleeps, collection stops.)

### 6. Run the recorder as a service (auto-start, auto-restart)
Use **NSSM** (https://nssm.cc) — the standard way to run a program as a Windows service:
```powershell
nssm install wck-recorder "C:\path\to\vibe\.venv\Scripts\wck.exe" "record --source prod --out-db data\wc_tournament.sqlite3"
nssm set wck-recorder AppDirectory "C:\path\to\vibe"
nssm set wck-recorder AppStdout "C:\path\to\vibe\data\recorder.out.log"
nssm set wck-recorder AppStderr "C:\path\to\vibe\data\recorder.err.log"
nssm start wck-recorder
```
NSSM auto-restarts it if it ever exits (the launchd `KeepAlive` equivalent).

### 7. Schedule the publisher every minute (Task Scheduler)
- Create Task → Trigger: **Daily, repeat every 1 minute, indefinitely**.
- Action: `powershell.exe` with arguments
  `-NoProfile -ExecutionPolicy Bypass -File "C:\path\to\vibe\scripts\publish_live.ps1"`
- Check **Run whether user is logged on or not**.

### 8. Hand off from the Mac
Once the Windows box is recording + publishing, **stop the Mac jobs** so there aren't two
collectors writing or two publishers clobbering `live.json`:
```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.wckalshi.recorder.plist
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.wckalshi.livepublish.plist
```

## Notes
- The Windows DB starts empty and re-accumulates from when it starts; already-captured
  games are already baked into the committed bundles, so nothing is lost.
- Refreshing the **settled-game** bundles (`wck export-bundles` → commit → redeploy) can
  stay manual for now, or later move to this box pushing all bundles to Blob too.
