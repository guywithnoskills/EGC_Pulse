# EGC Pulse — compliant social listening (demo)

Zero-infra, pure-Python (stdlib only). Grayscale, agency-grade UI. **Real data
only — no synthetic fill.** Source access is governed by
[`compliant_connectors.py`](compliant_connectors.py) — see
[COMPLIANT_CONNECTORS.md](COMPLIANT_CONNECTORS.md). No scraping, ever.

## Run locally

This is the supported way to run EGC Pulse today — a local/internal app on
`http://localhost:8787`. Run it from the **repo root**:

```bash
cp demo/.env.example demo/.env     # fill in keys you have (optional)
python3 demo/pulse_demo.py serve   # serves API + dashboard on http://localhost:8787
open http://localhost:8787         # macOS: open in your default browser
```

Open it in **Chrome** specifically (macOS):

```bash
open -a "Google Chrome" http://localhost:8787
```

Or use the optional one-command helper (copies `.env` if missing, starts the
server, and opens your browser — Chrome on macOS, sensible fallbacks elsewhere):

```bash
./demo/run_local.sh
```

> Linux: `xdg-open http://localhost:8787` · Windows: `start http://localhost:8787`.
> Change the port with `PORT=9000 python3 demo/pulse_demo.py serve`.

No keys? It still runs — the open/fediverse sources are live and keyless.

## Hosting

**Best fit today: run it locally / on an internal machine** (the command above).
It's a self-contained Python backend that serves both the API and the dashboard,
keeps secrets server-side in `demo/.env`, runs async collection jobs, and stores
state in a local SQLite file (`pulse_demo.db`). That is a stateful backend app,
not a static site.

**Netlify is *not* a fit.** Netlify hosts static assets + short-lived serverless
functions. This app needs a long-running process (async jobs, in-memory job
state), server-side `.env` secrets, and a writable SQLite database — none of
which map onto Netlify without rewriting the server into stateless functions plus
an external database and queue. Don't deploy it there.

**When you need it off your laptop**, host the container on a platform that runs a
persistent process with a mounted disk and server-side secrets:

| Option | Why |
|---|---|
| **Render / Railway / Fly.io** *(recommended)* | Run the `Dockerfile` directly; set secrets in their dashboard; attach a small persistent volume for `pulse_demo.db`. Easiest path. |
| **VPS (e.g. a small cloud VM) + Docker** | Most control; put it behind a reverse proxy (Caddy/nginx) with TLS. |
| **Internal server / private network** | Good for an internal-only tool — no public exposure at all. |

Container build/run (no secrets in the image — pass them at runtime; the DB lives
on a mounted volume via `PULSE_DB`, kept off the app code):

```bash
docker build -t egc-pulse demo/
docker run -p 8787:8787 --env-file demo/.env \
  -e PULSE_DB=/data/pulse_demo.db -v pulse-data:/data egc-pulse
# open http://localhost:8787
```

### Deploy to Render (recommended path)

A ready-to-use Blueprint lives at the repo root: [`render.yaml`](../render.yaml).

1. Push this repo to GitHub/GitLab (`.env` and `*.db` are gitignored — confirm
   they are **not** staged).
2. In Render → **New +** → **Blueprint** → select the repo. Render reads
   `render.yaml`, builds `demo/Dockerfile`, mounts a 1 GB disk at `/data`, and
   points `PULSE_DB` there so collected data survives restarts.
3. Render prompts for the secret env vars (all marked `sync: false`). Set only the
   keys you have; leave the rest blank to keep those sources honestly gated.
   `INTERNAL_USE_ONLY=true` and `AI_INSIGHTS_ENABLED=false` are preset.
4. Deploy. Render injects `PORT`; the app already binds `0.0.0.0:$PORT`. The
   health check hits `/`.

> The persistent disk needs a paid instance (`plan: starter`). For a throwaway
> demo, remove the `disk:` block and `PULSE_DB` from `render.yaml` and use
> `plan: free` — data then resets on each restart. **Railway / Fly.io** work the
> same way: point them at `demo/Dockerfile`, set the same env vars, attach a
> volume mounted where `PULSE_DB` points.

**Security for an internal tool:**

- Secrets are **server-side environment variables only** — set them in the host's
  secret store (or `--env-file`); they are never shipped to the browser. The
  Accounts & access drawer shows variable *names* only, never values.
- **Never commit `.env`** (it's gitignored; `.env.example` is the template). The
  `.dockerignore` keeps `.env` and `*.db` out of any image.
- This is **internal-use** software with **no built-in login**. A Render/Railway/
  Fly web service is **public by default**, so before sharing the URL put it behind
  **VPN, SSO, basic auth (reverse proxy), or an IP allowlist**. Don't expose an
  unauthenticated instance to the open internet.

## What's actually live vs. gated

| Platform | Status without keys | How it becomes live |
|---|---|---|
| **Mastodon, Lemmy, Nostr, PeerTube, Hacker News, News (GDELT)** | **Live** (keyless public APIs) | — already live |
| **Reddit** | Requires API key | set `REDDIT_CLIENT_ID` + `REDDIT_CLIENT_SECRET` |
| **X / Twitter** | Requires API key | set `X_BEARER_TOKEN` (recent); `X_ARCHIVE=full` for full archive (paid) |
| **Instagram** | Requires connected account | Meta app review + owned IG Business acct, or a licensed provider |
| **Facebook** | Requires connected account | Connected Page (Graph API) + app review, or a licensed provider |
| **TikTok** | Requires approved research access | TikTok Research API approval, or a licensed provider |
| **Public IG/FB/TikTok listening** | Requires licensed data provider | set `LICENSED_PROVIDER_API_KEY` + `LICENSED_PROVIDER_URL` |

Instagram / Facebook / TikTok public listening is **not** claimed live unless a
connected account or licensed provider is configured. The Sources & coverage
drawer shows the honest status and coverage of every source.

## Date range

Custom `start`/`end` (the core) plus presets (Today, Yesterday, Last 7/14/30/90,
YTD, Custom). The range is validated (end ≥ start), persisted in the URL + local
storage, and applied to every query, chart, KPI, keyword count, and the feed.
Sources with a max date-window (e.g. TikTok Research API) are auto-chunked and
merged/de-duped behind the scenes (`chunk_range` + `dedupe`).

## Historical modes

`recent_only` · `official_archive` · `licensed_archive` ·
`connected_account_history` · `research_api`. Per-source capability is in the
matrix (`/api/sources`). Backfill (`POST /api/backfill`) chunks the range per
source, dedupes, stores, and returns a per-source **coverage disclosure**.

## API

`GET /api/metrics|mentions?keyword=&start=&end=` · `GET /api/sources` ·
`GET /api/export/mentions.csv` · `GET /api/export/insights.json` · `GET /api/report`
· `POST /api/collect|backfill?q=&start=&end=` · `POST /api/keywords/{add,remove,clear}`

## Secrets

Keys live in `demo/.env` (gitignored). Never commit real secrets; in production
use a vault. `demo/.env.example` is the template.
