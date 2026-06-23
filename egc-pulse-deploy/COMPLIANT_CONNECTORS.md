# compliant-social-connectors

The governance module (`compliant_connectors.py`) that decides **how** any
social source may be added to EGC Pulse. Adding a new platform means adding a
registry entry here — it does **not** mean writing a scraper.

## Non-negotiable rules

1. **No scraping.** No headless browsers, no fake/aged accounts, no session-cookie
   reuse, no unofficial/private APIs, no bot-style automation. These are rejected
   by design — there is no code path that does them.
2. **Access preference order:** official API → connected (owned) account → licensed
   data provider. If none apply, the platform is *Not available compliantly*.
3. **Honesty in the UI.** A source is only "Live" when it is actually configured and
   returning authorized data. We never label public IG/FB/TikTok listening "Live"
   without a connected account or a licensed feed.
4. **Date honesty.** If a source only returns recent data, the coverage disclosure
   says so. We never imply we searched all history.

## Status labels

| Label | Meaning |
|---|---|
| **Live** | Configured and returning data now |
| **Requires API key** | Self-serve official key needed (Reddit, X) |
| **Requires connected account** | Brand must OAuth an owned account (IG, FB) |
| **Requires approved research access** | Platform research-program approval (TikTok) |
| **Requires licensed data provider** | Paid licensed feed for public listening |
| **Not available compliantly** | No lawful path today |

## Historical modes

`recent_only` · `official_archive` · `licensed_archive` ·
`connected_account_history` · `research_api`

## Connector checklist (every registry entry must declare)

auth · data_types · date_range_support (`native` / `chunked` / `filter_only` /
`connected_only` / `none`) · max_lookback · rate_limit · permissions ·
historical_modes · storage · display / cache / export / resell · coverage note.

## Capability matrix

| Platform | Status (unconfigured) | Auth | Date range | Historical | Public listening |
|---|---|---|---|---|---|
| **Reddit** | Requires API key | OAuth2 client-creds | filter_only | recent_only, licensed_archive | Official API (recency); deep history → licensed/archive |
| **X / Twitter** | Requires API key | App-only bearer | native (start/end_time) | recent_only, official_archive | Recent = 7 days; full archive → paid/enterprise (`X_ARCHIVE=full`) |
| **Instagram** | Requires connected account | Meta Graph API / licensed | connected_only | connected_account_history, licensed_archive | No public API search; owned account or licensed provider |
| **Facebook** | Requires connected account | Meta Graph API / licensed | connected_only | connected_account_history, licensed_archive | No public search (CrowdTangle retired); owned Pages or licensed |
| **TikTok** | Requires approved research access | Research/Display/Commercial / licensed | chunked (max window) | research_api, licensed_archive | Research API approval (chunked) or licensed provider |
| Mastodon / Lemmy / Nostr / PeerTube / Hacker News / News (GDELT) | Live | none (public) | filter_only | recent_only | Open public APIs — genuinely compliant |

## Date-window chunking

Sources with `date_range_support = "chunked"` (e.g. TikTok Research API) declare a
max window. `chunk_range(start, end, max_days)` splits long ranges into per-call
windows; results are merged and de-duplicated by `(platform, post_id|url)` via
`dedupe()`. Backfill (`/api/backfill`) drives this and returns a per-source
coverage disclosure.

## Adding a source — checklist

1. Add a `SOURCES` entry with the full checklist + a `coverage` string.
2. Implement a `collect_<x>(term, start, end)` that uses **only** an official API,
   a connected-account token, or a licensed provider — and returns `[]` (with a log)
   when not configured. Never fabricate data.
3. Set `configured` to the env check that makes it actually work.
4. Map its status in `source_status()`.
5. Confirm storage/deletion rules and display/cache/export/resell flags.
