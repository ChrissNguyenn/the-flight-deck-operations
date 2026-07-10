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

# ---- global storm watch (data/storms.json) ----
# International SIGMETs (AWC has no CORS, so the browser can't fetch them
# itself), NHC active storms, and JTWC tropical cyclone warnings for the
# STORM WATCH tab. Sources move slowly, so refresh every 5th minute; each
# source keeps its previous data when a fetch fails.
import re
import requests

STORMS = DATA / "storms.json"
old_storms = None
try:
    old_storms = json.loads(STORMS.read_text(encoding="utf-8"))
except Exception:
    pass

if old_storms is None or now.minute % 5 == 2 or "--storms" in sys.argv:
    UA = {"User-Agent": "Mozilla/5.0 (flight-deck-ops)"}

    def _get(url, timeout=20):
        r = requests.get(url, headers=UA, timeout=timeout)
        r.raise_for_status()
        return r

    prev = old_storms or {}
    storms = {"sigmets": prev.get("sigmets", []),
              "nhc": prev.get("nhc", []),
              "jtwc": prev.get("jtwc", [])}

    try:  # -- international SIGMETs, slimmed to what the UI shows
        raw = _get("https://aviationweather.gov/api/data/isigmet?format=json").json()
        now_s = int(time.time())
        storms["sigmets"] = [{
            "fir": s.get("firName") or s.get("firId") or s.get("icaoId"),
            "hazard": s.get("hazard"), "qual": s.get("qualifier"),
            "base": s.get("base"), "top": s.get("top"),
            "from": s.get("validTimeFrom"), "to": s.get("validTimeTo"),
            "series": s.get("seriesId"),
            "raw": (s.get("rawSigmet") or "")[:600],
        } for s in raw if (s.get("validTimeTo") or 0) > now_s]
    except Exception as exc:
        print(f"storms: SIGMET fetch failed ({exc}) — keeping previous")

    try:  # -- NHC (Atlantic / East & Central Pacific)
        raw = _get("https://www.nhc.noaa.gov/CurrentStorms.json").json()
        nhc = []
        for st in raw.get("activeStorms", []):
            sid = str(st.get("id", "")).lower()
            binno = str(st.get("binNumber", ""))
            cone = None
            if sid and binno:
                cand = (f"https://www.nhc.noaa.gov/storm_graphics/{binno}/"
                        f"{sid.upper()}_5day_cone_with_line_and_wind_sm2.png")
                try:
                    if requests.head(cand, headers=UA, timeout=10).ok:
                        cone = cand
                except Exception:
                    pass
            nhc.append({"name": st.get("name"),
                        "class": st.get("classification"),
                        "intensity": st.get("intensity"),
                        "pressure": st.get("pressure"),
                        "lat": st.get("latitudeNumeric"),
                        "lon": st.get("longitudeNumeric"),
                        "moveDir": st.get("movementDir"),
                        "moveSpd": st.get("movementSpeed"),
                        "updated": st.get("lastUpdate"), "cone": cone})
        storms["nhc"] = nhc
    except Exception as exc:
        print(f"storms: NHC fetch failed ({exc}) — keeping previous")

    try:  # -- JTWC (NW Pacific / Indian Ocean / Southern Hemisphere)
        rss = _get("https://www.metoc.navy.mil/jtwc/rss/jtwc.rss").text
        jtwc = []
        for desc in re.findall(r"<description><!\[CDATA\[(.*?)\]\]></description>",
                               rss, re.S):
            # one block per storm: "<b>Typhoon 09W (Bavi) Warning #39</b>...<ul>links</ul>"
            for block in re.split(r"<p><b>", desc)[1:]:
                header = re.sub(r"<[^>]+>", " ", block.split("</b>")[0])
                header = re.sub(r"\s+", " ", header).strip()
                if "warning" not in header.lower():
                    continue
                links = re.findall(r"href='([^']+)'", block.split("</ul>")[0])
                gif = next((u for u in links
                            if re.search(r"/[a-z]{2}\d{4}\.gif$", u)), None)
                ir = next((u for u in links if u.endswith("sair.jpg")), None)
                txt_url = next((u for u in links if u.endswith("web.txt")), None)
                issued = None
                m = re.search(r"Issued at\s*([\d/]+Z)", block)
                if m:
                    issued = m.group(1)
                warn_txt = None
                if txt_url:
                    try:
                        warn_txt = _get(txt_url, timeout=15).text[:3500]
                    except Exception:
                        pass
                jtwc.append({"title": header, "issued": issued, "gif": gif,
                             "ir": ir, "textUrl": txt_url, "text": warn_txt})
        storms["jtwc"] = jtwc
    except Exception as exc:
        print(f"storms: JTWC fetch failed ({exc}) — keeping previous")

    unchanged = old_storms and all(
        old_storms.get(k) == storms[k] for k in ("sigmets", "nhc", "jtwc"))
    if unchanged:
        print(f"storms.json: no change — {len(storms['sigmets'])} SIGMETs, "
              f"{len(storms['nhc']) + len(storms['jtwc'])} tropical systems")
    else:
        storms["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        STORMS.write_text(json.dumps(storms, ensure_ascii=False), encoding="utf-8")
        print(f"storms.json: UPDATED — {len(storms['sigmets'])} SIGMETs, "
              f"{len(storms['nhc'])} NHC + {len(storms['jtwc'])} JTWC systems")
