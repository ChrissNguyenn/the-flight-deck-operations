#!/usr/bin/env python3
"""
Fetch Vietnam weather ONCE and write static files for GitHub Pages.

Run by .github/workflows/weather.yml every 30 minutes:
  data/weather.json      pre-parsed METAR/SPECI/TAF for all VV stations
  data/satellite.json    list of downloaded imagery paths
  data/satellite/*       the imagery itself

Credentials come from environment variables (GitHub Actions secrets):
  MET_USERNAME / MET_PASSWORD / MET_BASE_URL
Locally it falls back to config.json via acuro_bridge.
"""

import json
import sys
from pathlib import Path

import acuro_bridge as ab

DATA = Path(__file__).resolve().parent / "data"
DATA.mkdir(exist_ok=True)

# ---- weather (MET portal → aviationweather.gov fallback) ----
payload = ab.refresh_now()

# Never overwrite good data with a bad fetch: if the portal returned nothing
# (login hiccup, maintenance page), keep the previous weather.json and fail
# the run so it shows red in the Actions tab.
if not payload.get("stations"):
    print("ERROR: fetch returned 0 stations — keeping previous weather.json")
    sys.exit(1)

(DATA / "weather.json").write_text(
    json.dumps(payload, ensure_ascii=False), encoding="utf-8")
print(f"weather.json: {len(payload['stations'])} stations from {payload['source']}")

# ---- satellite imagery ----
# The frontend loads Himawari-9 frames directly from JMA, so committing
# copies here only bloated the repo and forced a Pages deploy every run.
(DATA / "satellite.json").write_text(json.dumps({
    "images": [],
    "note": None,
}, ensure_ascii=False), encoding="utf-8")
print("satellite.json: imagery served directly from JMA by the frontend")
