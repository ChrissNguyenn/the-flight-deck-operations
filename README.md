# THE FLIGHT DECK — OPERATIONS

Standalone Vietnam aviation weather (METAR / SPECI / TAF for all VV
airports), migrated from the ACURO QRH. Live site:
**https://chrissnguyenn.github.io/the-flight-deck-operations/**

## How the weather updates

```
VATM MET portal ──(login via Actions secrets)──▶ fetch_weather_static.py
        ▲                                              │ commits data/weather.json
        │ every 60 s                                   ▼
Actions loop job (self-sustaining)     GitHub Pages / raw.githubusercontent
                                                       ▲
                                            frontend polls every 60 s
```

- **Credentials** live only in GitHub Actions secrets
  (`MET_BASE_URL`, `MET_USERNAME`, `MET_PASSWORD`) and in the local,
  gitignored `config.json`. Never commit them.
- **Self-sustaining loop**: GitHub delays `schedule:` crons by 5–120
  minutes, so instead of one run per minute, a single Actions job loops
  fetch → commit-on-change → sleep 60 s for ~5h40m (under the 6-hour
  job limit). The half-hourly cron keeps one run **queued** in the
  `weather-update` concurrency group (`cancel-in-progress: false`), and
  it takes over the moment the running loop ends. No external pinger or
  token is needed. If the loop ever dies, the next cron firing restarts
  it within ~30 minutes — or press *Run workflow* in the Actions tab
  for an instant restart.
- The frontend reads `data/weather.json` from **raw.githubusercontent
  first** (updates seconds after each commit, needs the repo to be
  public) and falls back to the GitHub-Pages copy (lags a few minutes
  behind, because Pages rebuilds are throttled to ~10/hour).
- **Commit-on-change**: each run compares the fetched reports with the
  committed `weather.json` and only commits when something changed.
  A fresh **SPECI** therefore reaches the site within ~1–2 minutes of
  the portal receiving it, while unchanged minutes produce no commit,
  no Pages build, and no git noise.
- **TAF economy**: TAFs are issued only 4×/day but need a ~30-page
  portal scan; the every-minute runs reuse the previous TAFs and do a
  full rescan only on `:x0` minutes. METAR/SPECI (3 pages) are fetched
  on every run.

## Checking / restarting the loop

- **Is it running?** Actions tab → "Update Vietnam Weather" — one run
  should be *in progress* (the loop) and usually one *queued*.
- **Restart instantly**: Actions tab → Update Vietnam Weather →
  *Run workflow*. (The old cron-job.org/PAT pinger setup is obsolete —
  the loop replaced it and needs no external service.)

## Repo visibility vs Actions minutes

This repo is **public** (made public 2026-07-10; the git history was
verified clean of credentials, portal URLs, and API keys first).
Actions minutes on standard runners are therefore free and unlimited —
the continuous loop (~24 runner-hours/day) costs nothing.

Do **not** make the repo private again while the loop is active:
private-repo runs bill by the minute (~1,440/day), which exhausts a
Pro plan's 3,000 monthly minutes in ~2 days.
