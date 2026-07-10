# THE FLIGHT DECK — OPERATIONS

Standalone Vietnam aviation weather (METAR / SPECI / TAF for all VV
airports), migrated from the ACURO QRH. Live site:
**https://chrissnguyenn.github.io/the-flight-deck-operations/**

## How the weather updates

```
VATM MET portal ──(login via Actions secrets)──▶ fetch_weather_static.py
        ▲                                              │ commits data/weather.json
        │ 1-min trigger                                ▼
cron-job.org ──▶ GitHub workflow_dispatch API    GitHub Pages / raw.githubusercontent
                                                       ▲
                                            frontend polls every 60 s
```

- **Credentials** live only in GitHub Actions secrets
  (`MET_BASE_URL`, `MET_USERNAME`, `MET_PASSWORD`) and in the local,
  gitignored `config.json`. Never commit them.
- The workflow's own `schedule:` cron is a **backstop only** — GitHub
  delays scheduled runs by 5–120 minutes. The reliable 1-minute cadence
  comes from an external pinger (below).
- The frontend reads `data/weather.json` from **raw.githubusercontent
  first** (updates seconds after each commit, needs the repo to be
  public) and falls back to the GitHub-Pages copy (lags a few minutes
  behind, because Pages rebuilds are throttled to ~10/hour).

## One-time setup: 1-minute pinger (cron-job.org)

1. **Create a fine-grained GitHub token** —
   github.com → Settings → Developer settings → Personal access tokens
   → Fine-grained tokens → *Generate new token*:
   - Repository access: **Only select repositories** → `the-flight-deck-operations`
   - Repository permissions: **Actions → Read and write** (nothing else)
   - Copy the token. Optionally keep it in the gitignored
     `weathertoken.txt` — never commit it.
2. **Create the cron job** — cron-job.org → *Create cronjob*:
   - URL: `https://api.github.com/repos/ChrissNguyenn/the-flight-deck-operations/actions/workflows/weather.yml/dispatches`
   - Schedule: **every 1 minute**
   - Advanced → Request method: **POST**
   - Advanced → Headers:
     - `Authorization: Bearer <YOUR TOKEN>`
     - `Accept: application/vnd.github+json`
     - `Content-Type: application/json`
   - Advanced → Request body: `{"ref":"main"}`
3. **Test** — a successful dispatch returns HTTP **204** (empty body).
   Same request from a terminal:

   ```
   curl -i -X POST \
     -H "Authorization: Bearer <YOUR TOKEN>" \
     -H "Accept: application/vnd.github+json" \
     https://api.github.com/repos/ChrissNguyenn/the-flight-deck-operations/actions/workflows/weather.yml/dispatches \
     -d '{"ref":"main"}'
   ```

## ⚠ Repo visibility vs Actions minutes

A 1-minute cadence ≈ **1,440 workflow runs/day**.

- **Private repo**: runs bill against the plan quota (Free 2,000 /
  Pro 3,000 minutes per month) — that is exhausted in **~2 days**, then
  all updates stop until the next billing month.
- **Public repo**: Actions minutes on standard runners are **free and
  unlimited** — the 1-minute cadence just works, and the
  raw.githubusercontent fast path starts working too.

The git history has been checked: no credentials, portal URL, or API
keys were ever committed, so making the repo public is safe
(Settings → General → Danger Zone → Change visibility).
