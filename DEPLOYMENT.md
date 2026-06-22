# EGC Pulse — deployment & API connection reference (technical)

For the click-by-click, non-technical walkthrough see [DEPLOY.md](DEPLOY.md).
This file explains the architecture, environment variables, and how connections
actually work in production.

## Architecture: single-origin app

EGC Pulse is **one Python process** (`pulse_demo.py serve`) that serves **both**
the dashboard (`/`) **and** the JSON API (`/api/*`). The frontend calls the API
with **relative paths** (`/api/...`), so:

- There is **no separate frontend host** and **no CORS** to configure.
- It works identically at `http://localhost:8787` and on a hosted domain.
- There is **no localhost hardcoded** anywhere in the frontend.

```
Browser ──/──▶ dashboard.html ─┐
        ──/api/*──▶ JSON API ──┴── same Python server (same origin)
```

## Local run

```bash
cp demo/.env.example demo/.env      # optional: add any API keys you have
python3 demo/pulse_demo.py serve    # http://localhost:8787
```

## Production hosting

Host the container (`demo/Dockerfile`) on any platform that runs a persistent
process with server-side env vars — **Render / Railway / Fly.io** (see
[`render.yaml`](../render.yaml)), a **VPS + Docker**, or an internal server.
**Netlify is not suitable** — it has no Python runtime and no long-running
process, so the API routes, async jobs, and SQLite state cannot run there.
(A static frontend on Netlify would still need this Python API hosted elsewhere.)

Render injects `PORT`; the app binds `0.0.0.0:$PORT` automatically.

## Frontend API base URL

- **Default:** relative paths — correct for the single-origin deploy above.
- **Split hosting (optional):** if you ever serve the frontend separately, set
  `PUBLIC_API_BASE_URL` on the backend. `GET /api/config` returns it as
  `api_base`, and the frontend applies it at startup. In that case you must also
  serve the page from somewhere that can reach `/api/config` and enable CORS.
- **`GET /api/config`** returns only safe, non-secret values:
  `{ api_base, app_mode, internal_use, ai_enabled, sources }`.

## Environment variables (all server-side only)

Set locally in `demo/.env`; in production set them in the host's environment
settings (e.g. Render → Environment), then redeploy. **Never** put secrets in the
frontend, the repo, or the browser.

| Variable | Purpose |
|---|---|
| `INTERNAL_USE_ONLY` | Shows the "Internal use only" label (default `true`). |
| `AI_INSIGHTS_ENABLED` | AI insight endpoint; **off** unless `true`. |
| `PUBLIC_API_BASE_URL` | Only for split hosting; leave unset for single-origin. |
| `PULSE_DB` | SQLite path; point at a mounted disk in production for persistence. |
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` / `REDDIT_USER_AGENT` | Reddit official API. |
| `X_BEARER_TOKEN` (`X_FULL_ARCHIVE_ENABLED`) | X official API (recent; full archive if enabled). |
| `META_APP_ID` / `META_APP_SECRET` / `META_ACCESS_TOKEN` | Meta Graph (owned accounts / Ad Library / approved research). |
| `TIKTOK_CLIENT_KEY` / `TIKTOK_CLIENT_SECRET` / `TIKTOK_ACCESS_TOKEN` (+ access flags) | TikTok connected/research/commercial. |
| `LICENSED_PROVIDER_API_KEY` / `LICENSED_PROVIDER_URL` | Licensed data provider adapter. |
| `ANTHROPIC_API_KEY` | Only used if `AI_INSIGHTS_ENABLED=true`. |

## What works without credentials

Open, keyless, compliant public APIs — **live out of the box**: Mastodon, Lemmy,
Nostr, PeerTube, Hacker News, News (GDELT). Plus **Manual import** of data you
have the lawful right to upload.

## What requires credentials (real adapters)

- **Reddit** — official API; set `REDDIT_*`. Real connector + live "Test connection".
- **X / Twitter** — official API; set `X_BEARER_TOKEN`. Recent search unless full
  archive is enabled. Real connector + live "Test connection".
- **Licensed provider** — set `LICENSED_PROVIDER_API_KEY` + `LICENSED_PROVIDER_URL`.
  Generic adapter; coverage depends on your provider contract. Returns nothing
  until configured — it never fabricates data. Provider rows are labeled
  `licensed_provider`.

## What remains gated (no direct connector)

- **Meta / Instagram / Facebook** — connected-account, Ad Library/transparency
  (ads only), or approved research access; or via a licensed provider. No broad
  public-listening adapter is wired, so the UI shows the status, **not** a
  "Test connection" button.
- **TikTok** — connected account, Research/Commercial approval, or licensed
  provider. Same: gated status, no dead button.

Open-web mentions of Instagram/Facebook/TikTok are **open-web references**, not
that platform's native data — source-truth labeling enforces this and the app
never claims those platforms are "live" unless a compliant path is configured.

## Two different capabilities: listening vs. account analytics

- **Listening** (mentions): *who is talking publicly* about a term. Sources:
  Bluesky, Reddit, News (GDELT), fediverse, manual import. Gives public posts +
  public engagement + sentiment. **Never** impressions or reach.
- **Account analytics** (impressions / reach / engagement): a connected account's
  **own private metrics**. These are visible only to the owner, so they are only
  available for accounts that authorize this app. Endpoint: `GET /api/insights/account`.

Impressions and reach for accounts you do **not** control (competitors, the
public) are impossible to obtain compliantly — they are never public.

## Owned-account analytics (impressions & reach) setup

`GET /api/insights/account` returns Instagram + Facebook insights for connected
accounts via the Meta Graph API. Until configured it returns an honest
"not connected" payload (no fabricated numbers). To connect **your own or a
client's** account:

1. Create a **Meta app** at developers.facebook.com (Business type).
2. Connect an **Instagram Business/Creator account** to a **Facebook Page** the
   account owner controls.
3. Get a long-lived **`META_ACCESS_TOKEN`**, the **`META_IG_USER_ID`** (IG Business
   account id), and **`META_FB_PAGE_ID`**; set them as server-side env vars.
4. Request permissions `instagram_basic`, `instagram_manage_insights`,
   `pages_read_engagement` — Meta **app review** is required for production /
   client accounts (your own accounts work in the app's dev mode without full
   review). See the [Meta Insights docs](https://developers.facebook.com/docs/instagram-platform/insights/).

> The connector code is implemented to Meta's documented endpoints, but it cannot
> be verified end-to-end without a real connected account + token — verify once
> your Meta app and a connected account exist. The same pattern extends to TikTok
> (Business/Display API) and YouTube (Analytics API).

## Why "skills" are not runtime connectors

ChatGPT/Claude **skills are specs and instructions** used during development —
they are **not** deployed API services and the running website does **not** load
any `skill.zip` at runtime. All production behavior lives in real backend modules:

- `platform_access_manager.py` — the access ladder + source metadata (authoritative)
- `compliant_connectors.py` — the actual fetchers + licensed-provider adapter
- `source_truth.py` — provenance/coverage labeling
- `ai_policy.py` — the optional, off-by-default read-only AI layer

To add reusable connector behavior on the host, implement it as a module/route
here — never as a runtime skill dependency.

## Security

Secrets are **server-side environment variables only** — never shipped to the
browser (the Accounts & access drawer shows variable *names* only), never logged,
never sent to the AI layer. `.env` and `*.db` are gitignored and excluded from the
Docker image. The app has **no built-in login**: before exposing the URL, put it
behind VPN, SSO, basic auth, or an IP allowlist.
