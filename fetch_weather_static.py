#!/usr/bin/env python3
"""
Fetch Vietnam weather ONCE and write static files for GitHub Pages.

Run by .github/workflows/weather.yml — triggered every minute by the
external cron-job.org pinger (workflow_dispatch), with the workflow's
own cron as a throttled backstop:
  data/weather.json      pre-parsed METAR/SPECI/TAF for all VV stations
  data/satellite.json    list of downloaded imagery paths

Built for the every-minute cadence:
  * weather.json is only rewritten when a report actually CHANGED, so
    the workflow only commits on change — a fresh SPECI reaches the
    site within ~1 minute, while quiet minutes cost no commit, no
    Pages build, and no git-history noise.
  * TAFs (issued 4x/day, ~30 portal pages to scan) are reused from the
    previous weather.json; a full rescan runs only on :x0 minutes.
    METAR/SPECI (3 pages, where SPECIs appear) are fetched every run.

Credentials come from environment variables (GitHub Actions secrets):
  MET_USERNAME / MET_PASSWORD / MET_BASE_URL
Locally it falls back to config.json via acuro_bridge.
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import acuro_bridge as ab

DATA = Path(__file__).resolve().parent / "data"
DATA.mkdir(exist_ok=True)

old = None
try:
    old = json.loads((DATA / "weather.json").read_text(encoding="utf-8"))
except Exception:
    pass

# Seed the bridge's TAF cache from the previous run — skip the heavy
# TAF slot scan except on :x0 minutes (or when there is nothing to seed).
now = datetime.now(timezone.utc)
if old and now.minute % 10 != 0:
    tafs = {s["icao"]: s["taf"] for s in old.get("stations", []) if s.get("taf")}
    if tafs:
        ab._taf_state["ts"] = time.time()
        ab._taf_state["tafs"] = tafs

# ---- weather (MET portal → aviationweather.gov fallback) ----
payload = ab.refresh_now()

# Never overwrite good data with a bad fetch: if the portal returned nothing
# (login hiccup, maintenance page), keep the previous weather.json and fail
# the run so it shows red in the Actions tab.
if not payload.get("stations"):
    print("ERROR: fetch returned 0 stations — keeping previous weather.json")
    sys.exit(1)

# Only rewrite when the reports changed ("updated" alone doesn't count) —
# an unchanged file means the workflow's commit step is a no-op.
if (old and old.get("source") == payload.get("source")
        and old.get("stations") == payload["stations"]):
    print(f"weather.json: no change — {len(payload['stations'])} stations "
          f"from {payload['source']} (last data change {old.get('updated')})")
else:
    (DATA / "weather.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"weather.json: UPDATED — {len(payload['stations'])} stations "
          f"from {payload['source']}")

# ---- satellite imagery ----
# The frontend loads Himawari-9 frames directly from JMA, so committing
# copies here only bloated the repo and forced a Pages deploy every run.
(DATA / "satellite.json").write_text(json.dumps({
    "images": [],
    "note": None,
}, ensure_ascii=False), encoding="utf-8")
print("satellite.json: imagery served directly from JMA by the frontend")
