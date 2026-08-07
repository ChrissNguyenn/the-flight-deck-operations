#!/usr/bin/env python3
"""
Mirror Tan Son Nhat radar / WINTEM / SigWx imagery for the web app.

Why this exists as a separate script
------------------------------------
The TSN portal is plain **http://** behind a login; the published site is
**https://** GitHub Pages. Browsers block that mix, and the portal needs a
session cookie anyway — so the images have to be fetched server-side and
re-published.

They are pushed to the orphan branch `wx-radar`, which is FORCE-pushed with
a single commit every time. Radar is ~130 kB per frame and refreshes every
5 minutes; committing that to `main` would add hundreds of megabytes of
permanent git history per day. An orphan branch that is continually
replaced keeps the mirror at its current size (~1 MB) forever.

Run:  python fetch_radar_frames.py <output-dir>
"""

import json
import sys
from pathlib import Path

import acuro_bridge as ab

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "radar-mirror")
OUT.mkdir(parents=True, exist_ok=True)

RAW = ("https://raw.githubusercontent.com/ChrissNguyenn/"
       "the-flight-deck-operations/wx-radar/")

# (manifest key, fetcher, how many to mirror, subfolder)
JOBS = (
    ("radar", ab.fetch_tsn_radar, 6, "radar"),
    ("wintem", ab.fetch_tsn_wintem, 6, "charts"),
    ("sigwx", ab.fetch_tsn_sigwx, 6, "charts"),
)

manifest = {}
total = 0
for key, fetch, limit, folder in JOBS:
    try:
        items = fetch(limit)
    except Exception as exc:
        print(f"{key}: fetch failed ({exc})")
        manifest[key] = []
        continue
    (OUT / folder).mkdir(parents=True, exist_ok=True)
    got = []
    for name, path in items:
        dest = OUT / folder / name
        try:
            if not dest.exists():
                dest.write_bytes(ab._tsn_asset(path))
                total += 1
            got.append({"name": name, "url": RAW + folder + "/" + name})
        except Exception as exc:
            print(f"{key}: {name} failed ({exc})")
    manifest[key] = got
    print(f"{key}: {len(got)} frame(s)")

manifest["updated"] = ab.datetime.now(ab.timezone.utc).isoformat(timespec="seconds")
(OUT / "manifest.json").write_text(
    json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

# Drop anything no longer referenced so the mirror cannot grow without bound
keep = {i["name"] for v in manifest.values() if isinstance(v, list) for i in v}
for f in OUT.rglob("*"):
    if f.is_file() and f.name not in keep and f.name != "manifest.json":
        f.unlink()

print(f"mirror ready: {total} new file(s), "
      f"{sum(len(v) for v in manifest.values() if isinstance(v, list))} referenced")
