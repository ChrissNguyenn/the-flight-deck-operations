#!/usr/bin/env python3
"""
ACURO Bridge — Real Vietnam Weather backend (FastAPI, port 8000)
================================================================
Logs into an authenticated Vietnamese MET portal with a persistent
session and serves pre-parsed Vietnamese aviation weather to the EFB:

  GET /api/weather/vietnam            METAR / SPECI / TAF for all VV airports
  GET /api/weather/satellite          observation imagery list (downloaded)
  GET /api/weather/satellite-image/x  serves a downloaded image

Credentials live in config.json (gitignored) — never in this file:
  "met_base":     "<portal index.php URL>",
  "met_username": "…",
  "met_password": "…"

Fallback chain per refresh: MET portal → aviationweather.gov → HTTP 502
(the frontend has its own mock as the final layer).

Run:  python acuro_bridge.py           (uvicorn on 0.0.0.0:8000)
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

import requests
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

BASE_DIR = Path(__file__).resolve().parent
SAT_DIR = BASE_DIR / "images" / "satellite"

# ----------------------------------------------------------------------
# Config (credentials come from config.json — gitignored)
# ----------------------------------------------------------------------
def _load_config():
    try:
        with open(BASE_DIR / "config.json", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}

_CFG = _load_config()
# Environment variables win (GitHub Actions secrets); config.json for local
# use. The MET portal URL is deliberately NOT hardcoded here — this file is
# public on GitHub, so the portal identity lives only in secrets/config.
MET_BASE = (os.environ.get("MET_BASE_URL") or os.environ.get("MET_BASE")
             or _CFG.get("met_base", ""))
MET_USER = (os.environ.get("MET_USERNAME") or os.environ.get("MET_USERNAME")
             or _CFG.get("met_username", ""))
MET_PASS = (os.environ.get("MET_PASSWORD") or os.environ.get("MET_PASSWORD")
             or _CFG.get("met_password", ""))

SOURCE_LABEL = "VN MET"        # public-facing name of the authenticated feed

REFRESH_S = 300
HTTP_TIMEOUT = 15

STATION_NAMES = {
    "VVTS": "Tan Son Nhat — Ho Chi Minh City", "VVNB": "Noi Bai — Ha Noi",
    "VVDN": "Da Nang Intl", "VVCR": "Cam Ranh — Nha Trang",
    "VVPQ": "Phu Quoc Intl", "VVCI": "Cat Bi — Hai Phong",
    "VVCT": "Can Tho Intl", "VVDL": "Lien Khuong — Da Lat",
    "VVBM": "Buon Ma Thuot", "VVPC": "Phu Cat — Quy Nhon",
    "VVVH": "Vinh", "VVPB": "Phu Bai — Hue",
    "VVCS": "Con Dao", "VVCA": "Chu Lai",
    "VVDB": "Dien Bien Phu", "VVDH": "Dong Hoi",
    "VVPK": "Pleiku", "VVRG": "Rach Gia",
    "VVCM": "Ca Mau", "VVTX": "Tho Xuan — Thanh Hoa",
    "VVVD": "Van Don — Quang Ninh", "VVTH": "Dong Tac — Tuy Hoa",
    "VVGL": "Gia Lam — Ha Noi", "VVNS": "Na San — Son La",
    "VVVT": "Vung Tau",
}
AWC_URL = ("https://aviationweather.gov/api/data/metar?ids="
           + ",".join(STATION_NAMES) + "&format=json")

# ----------------------------------------------------------------------
# METAR text parsing (same logic the EFB used before, kept server-side)
# ----------------------------------------------------------------------
def parse_metar(raw):
    out = {"fltcat": "UNK", "wind": "—", "vis": "—", "clouds": "—",
           "temp": None, "dewp": None, "qnh": "—"}
    if not raw:
        return out
    txt = " " + raw.strip() + " "

    m = re.search(r"\s(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?(KT|MPS)\s", txt)
    if m:
        direction = "VRB" if m.group(1) == "VRB" else m.group(1) + "°"
        unit = "kt" if m.group(4) == "KT" else "m/s"
        gust = f" G{int(m.group(3))}" if m.group(3) else ""
        out["wind"] = f"{direction} @ {int(m.group(2))} {unit}{gust}"

    vis_m = None
    if " CAVOK " in txt:
        out["vis"] = "CAVOK"
        vis_m = 10000
    else:
        m = re.search(r"\s(\d{4})\s", txt)
        if m:
            vis_m = int(m.group(1))
            out["vis"] = ">10 km" if vis_m >= 9999 else f"{vis_m / 1000:.1f} km"
        else:
            m = re.search(r"\s(\d{1,2})SM\s", txt)
            if m:
                vis_m = int(m.group(1)) * 1609
                out["vis"] = m.group(1) + " SM"

    layers = re.findall(r"\b(FEW|SCT|BKN|OVC|VV)(\d{3})(?:CB|TCU)?", txt)
    if layers:
        out["clouds"] = " ".join(c + h for c, h in layers)
    elif re.search(r"\s(CAVOK|NSC|SKC|CLR)\s", txt):
        out["clouds"] = "NSC"

    m = re.search(r"\s(M?\d{2})/(M?\d{2})\s", txt)
    if m:
        out["temp"] = int(m.group(1).replace("M", "-"))
        out["dewp"] = int(m.group(2).replace("M", "-"))

    m = re.search(r"\sQ(\d{4})", txt)
    if m:
        out["qnh"] = f"{int(m.group(1))} hPa"
    else:
        m = re.search(r"\sA(\d{4})", txt)
        if m:
            out["qnh"] = f"{round(int(m.group(1)) / 100 * 33.8639)} hPa"

    ceiling = None
    for cover, height in layers:
        if cover in ("BKN", "OVC", "VV"):
            ft = int(height) * 100
            ceiling = ft if ceiling is None else min(ceiling, ft)
    vis_sm = (vis_m / 1609.0) if vis_m is not None else 99
    ceil = ceiling if ceiling is not None else 99999
    if vis_m is None and ceiling is None:
        out["fltcat"] = "UNK"
    elif vis_sm < 1 or ceil < 500:
        out["fltcat"] = "LIFR"
    elif vis_sm < 3 or ceil < 1000:
        out["fltcat"] = "IFR"
    elif vis_sm <= 5 or ceil <= 3000:
        out["fltcat"] = "MVFR"
    else:
        out["fltcat"] = "VFR"
    return out


# ----------------------------------------------------------------------
# MET portal session (persistent, auto re-login)
# ----------------------------------------------------------------------
_session = requests.Session()
_session.headers["User-Agent"] = "Mozilla/5.0 (ACURO-EFB-Bridge)"
_session_lock = threading.Lock()


def _met_login():
    if not MET_USER or not MET_PASS:
        raise RuntimeError("MET portal credentials missing (secrets/config.json)")
    r = _session.post(MET_BASE, data={
        "act": "login", "req": "2,0",
        "txtUsername": MET_USER, "txtPassword": MET_PASS,
        "cmdLogin": "Login",
    }, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    if "logout" not in r.text.lower():
        raise RuntimeError("MET portal login rejected (check credentials)")


def _met_get(params):
    """GET a portal page; re-login automatically if the session expired."""
    with _session_lock:
        r = _session.get(MET_BASE, params=params, timeout=HTTP_TIMEOUT)
        if "txtUsername" in r.text:          # bounced to the login form
            _met_login()
            r = _session.get(MET_BASE, params=params, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.text


def _slot_now():
    """Current UTC day (ddmmyyyy) and time rounded down to 30 min (HHMM)."""
    now = datetime.now(timezone.utc)
    return now.strftime("%d%m%Y"), f"{now.hour:02d}{(now.minute // 30) * 30:02d}"


def _extract_reports(html):
    """Pull (kind, icao, raw) reports out of the portal's <pre> blocks."""
    pres = re.findall(r"<pre[^>]*>(.*?)</pre>", html, re.S | re.I)
    text = re.sub(r"\s+", " ", unescape(" ".join(pres)))
    reports = []
    for chunk in text.split("="):
        m = re.search(r"\b(METAR|SPECI|TAF)(\s+(?:AMD|COR))?\s+:?\s*(VV[A-Z]{2})\b(.*)$", chunk)
        if not m:
            # portal also writes 'TAF VVBM : NIL' with icao before colon
            m = re.search(r"\b(TAF)\s+(VV[A-Z]{2})\s*:(.*)$", chunk)
            if not m:
                continue
            kind, icao, rest = m.group(1), m.group(2), m.group(3)
            if "NIL" in rest:
                continue
            reports.append((kind, icao, re.sub(r"\s+", " ", f"TAF {icao}{rest}").strip()))
            continue
        kind, icao = m.group(1), m.group(3)
        body = f"{kind}{m.group(2) or ''} {icao}{m.group(4)}"
        if "NIL" in (m.group(4) or "")[:8]:
            continue
        reports.append((kind, icao, re.sub(r"\s+", " ", body).strip()))
    return reports


def _report_time_key(raw):
    """(day, hour, minute) from the report's ddhhmmZ group, or None."""
    m = re.search(r"\b(\d{2})(\d{2})(\d{2})Z\b", raw or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _key_newer(a, b):
    """True when report-time key a is newer than b (month-wrap aware)."""
    if b is None:
        return True
    if a is None:
        return False
    da, db = a[0], b[0]
    if abs(da - db) > 15:                 # month rollover: day 01 beats day 31
        return da < db
    return a > b


def fetch_met_weather():
    """METAR/SPECI merged from the last 3 half-hour slots (the portal's
    slot pages only contain reports RECEIVED in that window — querying a
    slot right after it opens finds it nearly empty, and a SPECI stays in
    the slot it arrived in). TAFs come from the latest issue time."""
    now = datetime.now(timezone.utc)
    day, slot = _slot_now()

    best_m, best_s = {}, {}               # icao -> (time_key, raw)
    for back in range(3):                 # current slot + previous two
        t = now - timedelta(minutes=30 * back)
        d = t.strftime("%d%m%Y")
        s = f"{t.hour:02d}{(t.minute // 30) * 30:02d}"
        html = _met_get({"cat": "2", "sub": "0", "type": "0",
                         "cboTime": s, "cboDay": d, "cboElement": ""})
        for kind, icao, raw in _extract_reports(html):
            target = best_m if kind == "METAR" else (best_s if kind == "SPECI" else None)
            if target is None:
                continue
            key = _report_time_key(raw)
            if icao not in target or _key_newer(key, target[icao][0]):
                target[icao] = (key, raw)

    metars = {icao: raw for icao, (key, raw) in best_m.items()}
    # A SPECI is only current while it is NEWER than the routine METAR
    specis = {icao: raw for icao, (key, raw) in best_s.items()
              if icao not in best_m or _key_newer(key, best_m[icao][0])}
    # TAFs are issued at fixed hours but each airport's report lands in
    # whatever half-hour slot it was RECEIVED in — so scan every candidate
    # slot and keep the newest TAF per station. Stopping at the first slot
    # that had any TAF (the old behaviour) dropped most airports.
    now = datetime.now(timezone.utc)
    candidates = []
    for back in range(4):                 # last 2 h of reception slots
        t = now - timedelta(minutes=30 * back)
        candidates.append((t.strftime("%d%m%Y"),
                           f"{t.hour:02d}{(t.minute // 30) * 30:02d}"))
    for back_day in (0, 1):
        d = (now - timedelta(days=back_day)).strftime("%d%m%Y")
        # each issue hour ± the slots reports actually get received in
        for t in ("2230", "2300", "2330", "0000", "0030",
                  "1630", "1700", "1730",
                  "1030", "1100", "1130",
                  "0430", "0500", "0530"):
            if back_day == 0 and int(t) > int(slot):
                continue
            candidates.append((d, t))
    best_t = {}                           # icao -> (time_key, raw)
    seen = set()
    for d, t in candidates:
        if (d, t) in seen:
            continue
        seen.add((d, t))
        html = _met_get({"cat": "2", "sub": "1", "type": "0",
                          "cboTime": t, "cboDay": d, "cboElement": ""})
        for kind, icao, raw in _extract_reports(html):
            if kind != "TAF":
                continue
            key = _report_time_key(raw)
            if icao not in best_t or _key_newer(key, best_t[icao][0]):
                best_t[icao] = (key, raw)
    tafs = {icao: raw for icao, (key, raw) in best_t.items()}

    # Backfill any station the portal still left without a TAF from the
    # public international feed (major airports are always on it).
    missing = sorted(set(list(metars) + list(specis)) - set(tafs))
    if missing:
        try:
            r = requests.get("https://aviationweather.gov/api/data/taf",
                             params={"ids": ",".join(missing), "format": "raw"},
                             timeout=15)
            r.raise_for_status()
            for block in re.split(r"\n(?=TAF\b)", r.text.strip()):
                m = re.search(r"\bTAF\s+(?:AMD\s+|COR\s+)?(VV[A-Z]{2})\b", block)
                if m and m.group(1) not in tafs:
                    tafs[m.group(1)] = re.sub(r"\s+", " ", block).strip()
        except Exception:
            pass

    if not metars and not specis:
        raise RuntimeError("no reports parsed from MET portal (page format change?)")

    stations = []
    for icao in sorted(set(list(metars) + list(specis))):
        raw_for_parse = specis.get(icao) or metars.get(icao)   # SPECI is newer
        st = {"icao": icao, "name": STATION_NAMES.get(icao, icao),
              "metar": metars.get(icao), "speci": specis.get(icao),
              "taf": tafs.get(icao), "raw": raw_for_parse}
        st.update(parse_metar(raw_for_parse))
        stations.append(st)
    return stations, SOURCE_LABEL


# ----------------------------------------------------------------------
# Real-time D-ATIS (atis.guru) for the main training airports
# ----------------------------------------------------------------------
ATIS_AIRPORTS = ["VVTS", "VVNB", "VVDN", "VVCR"]


def fetch_atis(icao):
    """Scrape arrival/departure ATIS text from atis.guru/atis/<icao>."""
    r = _session.get(f"https://atis.guru/atis/{icao}", timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    # keep each ATIS on one line: the text uses &#xA; entities as newlines
    html = r.text.replace("&#xA;", " ").replace("&#10;", " ")
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    text = unescape(re.sub(r"<[^>]+>", "\n", html))
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    out = []
    for i, line in enumerate(lines):
        if line in ("Arrival ATIS", "Departure ATIS") and i + 2 < len(lines):
            kind = "ARR" if line.startswith("Arr") else "DEP"
            entry = {"kind": kind, "time": lines[i + 1],
                     "text": re.sub(r"\s+", " ", lines[i + 2]).strip()}
            if "ATIS" in entry["text"] and not any(e["kind"] == kind for e in out):
                out.append(entry)
    return out


def attach_atis(stations):
    """Best-effort ATIS enrichment — never blocks the weather refresh."""
    for st in stations:
        if st["icao"] in ATIS_AIRPORTS:
            try:
                st["atis"] = fetch_atis(st["icao"]) or None
            except Exception:
                st["atis"] = None


def fetch_awc_weather():
    """Fallback: public international feed (no SPECI/TAF, big airports only)."""
    data = json.loads(_session.get(AWC_URL, timeout=HTTP_TIMEOUT).text)
    stations = []
    for item in data:
        icao = (item.get("icaoId") or "").upper()
        if not icao.startswith("VV"):
            continue
        raw = item.get("rawOb") or ""
        st = {"icao": icao, "name": STATION_NAMES.get(icao, icao),
              "metar": raw, "speci": None, "taf": None, "raw": raw}
        st.update(parse_metar(raw))
        if item.get("fltCat"):
            st["fltcat"] = item["fltCat"]
        stations.append(st)
    if not stations:
        raise RuntimeError("AWC returned no VV stations")
    return sorted(stations, key=lambda s: s["icao"]), "aviationweather.gov"


# ----------------------------------------------------------------------
# Cache + background refresh
# ----------------------------------------------------------------------
_cache = {"ts": 0.0, "payload": None}
_cache_lock = threading.Lock()


def refresh_now():
    stations, source, errors = None, None, []
    for fetcher in (fetch_met_weather, fetch_awc_weather):
        try:
            stations, source = fetcher()
            break
        except Exception as exc:
            errors.append(f"{fetcher.__name__}: {exc}")
    if stations is None:
        raise RuntimeError("; ".join(errors))
    attach_atis(stations)
    payload = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "stations": stations,
    }
    with _cache_lock:
        _cache["ts"] = time.time()
        _cache["payload"] = payload
    return payload


def get_weather():
    with _cache_lock:
        fresh = _cache["payload"] is not None and (time.time() - _cache["ts"]) < REFRESH_S
        cached = _cache["payload"]
    if fresh:
        return cached
    try:
        return refresh_now()
    except Exception:
        if cached is not None:
            return cached
        raise


def _background_loop():
    while True:
        try:
            refresh_now()
        except Exception as exc:
            print(f"[bridge] weather refresh failed: {exc}")
        time.sleep(REFRESH_S)


# ----------------------------------------------------------------------
# FastAPI app
# ----------------------------------------------------------------------
app = FastAPI(title="ACURO Bridge", version="2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # EFB may be served from LAN, file://, or GitHub
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup():
    threading.Thread(target=_background_loop, daemon=True,
                     name="acuro-weather").start()


@app.get("/api/health")
def api_health():
    return {"ok": True}


@app.get("/api/weather/vietnam")
def api_weather():
    try:
        return JSONResponse(get_weather())
    except Exception as exc:
        raise HTTPException(status_code=502,
                            detail=f"weather sources unreachable: {exc}")


def fetch_satellite_images(dest_dir):
    """Download observation imagery through the portal session.
    Returns the list of saved file names (interface chrome is skipped)."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    names = []
    base_root = MET_BASE.rsplit("/", 1)[0]
    for sub in ("0", "1"):
        html = _met_get({"cat": "3", "sub": sub})
        for src in re.findall(r'<img[^>]+src=["\']?([^"\'\s>]+)', html, re.I):
            low = src.lower()
            # skip interface chrome — keep only observation products
            if any(x in low for x in ("interface", "bullet", "logo",
                                      "underconstruction", "banner")):
                continue
            if not low.startswith("images/"):
                continue
            name = re.sub(r"[^A-Za-z0-9._-]", "_", src.split("/")[-1])
            if name in names:
                continue
            r = _session.get(f"{base_root}/{src}", timeout=HTTP_TIMEOUT)
            if r.ok and r.content:
                (dest_dir / name).write_bytes(r.content)
                names.append(name)
    return names


NO_IMAGERY_NOTE = ("The MET portal currently has no observation "
                   "imagery (page is under construction).")


@app.get("/api/weather/satellite")
def api_satellite():
    """Scrape + download observation imagery (needs the portal session)."""
    try:
        names = fetch_satellite_images(SAT_DIR)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"satellite fetch failed: {exc}")
    return {"images": [f"/api/weather/satellite-image/{n}" for n in names],
            "note": None if names else NO_IMAGERY_NOTE}


@app.get("/api/weather/satellite-image/{filename}")
def api_satellite_image(filename: str):
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="bad filename")
    path = SAT_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="image not found")
    return FileResponse(path)


if __name__ == "__main__":
    print("=" * 56)
    print("  ACURO Bridge — Real Vietnam Weather  (port 8000)")
    print("  MET portal login:", "configured" if MET_USER else "NOT SET")
    print("=" * 56)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
