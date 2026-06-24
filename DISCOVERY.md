# Compliant discovery ladder (Instagram, Facebook, TikTok, Meta)

EGC Pulse never scrapes. It never uses headless browsers, fake accounts, session
cookies, private APIs, or unofficial mobile endpoints, and it never parses
Instagram, Facebook, TikTok, or X result HTML. "Discovery" here means finding
public references through compliant, official channels and labeling their source
truth honestly.

The single most important rule: a result is only marked as a platform being
"directly searched" when the data came from that platform's official API, a
connected account, approved research access, a licensed provider, or a lawful
manual import. Everything else is an open-web reference or a known-URL
enrichment, and is clearly labeled as not platform-native data.

## The ladder

### 1. Direct official platform search (gated by credentials)
- TikTok Research API (if approved)
- Meta Content Library (if approved)
- Instagram Graph hashtag search (only with proper connected-account permissions)
- Meta Ad Library (ads and transparency only, not organic posts)
- X official API
- Reddit official API

These live in `platform_access_manager.py` and `compliant_connectors.py`. Each is
gated until its credentials are configured. None are faked.

### 2. Known URL enrichment
- TikTok oEmbed for an already-known TikTok video URL only.
  - Input must be a TikTok video URL.
  - It does not search, crawl profiles, fetch hashtag pages, enumerate videos, or
    bypass platform search.
  - Stored as `coverage_type=known_url_enrichment`, `display_platform="TikTok URL"`,
    `source_mode=tiktok_oembed_known_url`, `direct_platform_data=false`.
- Instagram and Facebook embeds are used only if official and allowed. We do not
  use any embed endpoint for bulk discovery or to bypass search restrictions.

### 3. Open web discovery (gated by a search provider API)
Search a configured, official open-web search provider for brand terms, hashtags,
and platform URLs. We never scrape Google, Bing, Brave, or any result page HTML.
We call the provider's official JSON API and respect its terms and robots policy.

Query logic (`build_open_web_queries`), kept controlled and small:
```
"brand term" "tiktok.com"
"#brandhashtag" "tiktok.com"
"brand term" "instagram.com"
"#brandhashtag" "instagram.com"
"brand term" "facebook.com"
"brand term" "TikTok"
"brand term" "Instagram"
"brand term" "Facebook"
```
Results are stored as open-web references:
`searched_platform="open web"`, `display_platform="Open Web / News"`,
`discussed_platforms=["TikTok"]` (etc.), `direct_platform_data=false`,
`coverage_type=open_web_reference`.

Gating: if `SEARCH_PROVIDER` and `SEARCH_API_KEY` are not set, the source stays
gated and shows: "Open web social discovery requires a search provider API key or
existing open web connector." No data is faked.

Environment variables (server-side only, never in the browser):
```
SEARCH_PROVIDER=        # brave (primary, recommended) | bing | custom
SEARCH_API_KEY=         # Brave Search API key (api-dashboard.search.brave.com)
SEARCH_API_ENDPOINT=    # optional; default provided for brave/bing
```

### 4. Manual import
The user uploads lawful exports or rows. Stored as `manual_import`.

### 5. Licensed provider
Broad public listening on closed platforms through a licensed provider, if
configured (`LICENSED_PROVIDER_API_KEY`, `LICENSED_PROVIDER_URL`).

## Why open web discovery is not Instagram or TikTok listening
An open-web page that mentions a brand and links to a TikTok video tells us the
brand is being discussed. It does not give us TikTok's native post data, metrics,
or search results. So it is stored and shown as an open-web reference that
discusses TikTok, never as "TikTok searched." This is enforced in
`source_truth.py`: open-web and known-URL coverage types are not direct platform
data, and metrics never count them as direct platform mentions.

## What the module provides (`compliant_discovery.py`)
`build_open_web_queries`, `classify_discovered_url`, `extract_platform_from_url`,
`extract_hashtags_from_text`, `extract_known_social_urls`,
`normalize_open_web_reference`, `enrich_known_tiktok_url_oembed`,
`source_truth_for_discovery`, `search_provider_query` (gated),
`collect_open_web_discovery` (the wired collector), and
`reject_disallowed_collection_method` (a hard guard against banned techniques).

## Skills are not runtime connectors
ChatGPT or Claude skills cannot be deployed to the host domain as runtime API
connectors. Reusable discovery logic lives in backend modules and API routes
(`compliant_discovery.py`, `compliant_connectors.py`, `platform_access_manager.py`,
`source_truth.py`), not in a skill package loaded at runtime.
