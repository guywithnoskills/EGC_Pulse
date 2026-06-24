"""
compliant_discovery.py. Compliant open-web discovery and known-URL enrichment.

This module finds PUBLIC references to a brand across the open web, and enriches
already-known public URLs. It is NOT a scraper. It never touches platform HTML
result pages, private APIs, unofficial mobile endpoints, session cookies, or fake
accounts, and it never bypasses any platform's search restrictions.

The compliant discovery ladder (see DISCOVERY.md):
  1. Direct official platform search   (TikTok Research API, Meta Content Library,
     Instagram Graph hashtag, Meta Ad Library ads-only, X API, Reddit API)
  2. Known URL enrichment              (TikTok oEmbed for known video URLs only)
  3. Open web discovery                (a configured, official search-provider API)
  4. Manual import                     (lawful user uploads)
  5. Licensed provider                 (broad public listening, if configured)

Source truth: open-web results are stored as open_web_reference (NOT direct
platform data). A TikTok oEmbed enrichment of a known URL is known_url_enrichment
(NOT a TikTok search). Instagram, Facebook, and TikTok are never marked as
"searched" here. Direct platform data only ever comes from an official API, a
connected account, approved research access, a licensed provider, or lawful
manual import (handled elsewhere in the access ladder).
"""
import hashlib
import json
import os
import re
import urllib.parse
import urllib.request

UA = "egc-pulse/0.2 (compliant social listening; contact: listening@egcgroup.com)"

# ── hard guard: reject any non-compliant collection method by name ───────────
_BANNED = ["scrape", "scraper", "headless", "selenium", "playwright", "puppeteer",
           "session_cookie", "cookie_replay", "private_api", "unofficial_api",
           "mobile_endpoint", "fake_account", "stealth", "html_parse_results"]


def reject_disallowed_collection_method(method_name):
    """Raise if a method name implies a non-compliant technique. Open-web
    discovery and oEmbed enrichment must pass this check."""
    m = (method_name or "").lower()
    if any(b in m for b in _BANNED):
        raise ValueError(
            "Non-compliant collection method rejected: %s. Use official APIs, connected accounts, "
            "approved research APIs, transparency/ad APIs, licensed providers, an official open-web "
            "search API, or lawful manual import." % method_name)
    return True


# ── URL + text helpers ───────────────────────────────────────────────────────
_DOMAIN_PLATFORM = [
    ("tiktok", ["tiktok.com"]),
    ("instagram", ["instagram.com"]),
    ("facebook", ["facebook.com", "fb.com", "fb.watch"]),
    ("x", ["twitter.com", "x.com"]),
    ("youtube", ["youtube.com", "youtu.be"]),
    ("reddit", ["reddit.com"]),
    ("threads", ["threads.net"]),
    ("linkedin", ["linkedin.com"]),
]
_PLATFORM_DISP = {"tiktok": "TikTok", "instagram": "Instagram", "facebook": "Facebook",
                  "x": "X / Twitter", "youtube": "YouTube", "reddit": "Reddit",
                  "threads": "Threads", "linkedin": "LinkedIn"}

_HASHTAG_RE = re.compile(r"#([A-Za-z0-9_]{2,50})")
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)


def extract_platform_from_url(url):
    host = (url or "").lower()
    for plat, domains in _DOMAIN_PLATFORM:
        if any(d in host for d in domains):
            return plat
    return None


def extract_hashtags_from_text(text):
    seen, out = set(), []
    for h in _HASHTAG_RE.findall(text or ""):
        k = h.lower()
        if k not in seen:
            seen.add(k)
            out.append("#" + h)
    return out


def extract_known_social_urls(text):
    out = []
    for u in _URL_RE.findall(text or ""):
        u = u.rstrip(".,);]")
        if extract_platform_from_url(u):
            out.append(u)
    return out


def _norm_hashtag(term):
    return re.sub(r"[^A-Za-z0-9]", "", term or "")


def _domain(url):
    try:
        return urllib.parse.urlparse(url).netloc or None
    except Exception:
        return None


def _short_id(s):
    return "owd_" + hashlib.sha256((s or "").encode()).hexdigest()[:16]


def classify_discovered_url(url):
    """Map a discovered URL to a source mode. A TikTok VIDEO url is enrichable via
    oEmbed; any other tiktok url is only an open-web reference (we do not crawl)."""
    plat = extract_platform_from_url(url)
    if plat == "tiktok":
        return "tiktok_oembed_known_url" if "/video/" in (url or "").lower() else "open_web_social_discovery"
    if plat == "instagram":
        return "instagram_known_url_reference"
    if plat == "facebook":
        return "facebook_known_url_reference"
    return "open_web_social_discovery"


# ── 3. open web query builder (controlled, not spammy) ───────────────────────
def build_open_web_queries(term, hashtags=None, platforms=None):
    """A small, precise set of discovery queries for one tracked term. These are
    plain query strings to hand to an official search-provider API. They are not
    executed against any platform's own search."""
    term = (term or "").strip()
    if not term:
        return []
    tag = _norm_hashtag(term)
    # Lean query set to conserve the search provider's quota (Brave free credit
    # ~= 1,000 queries/month). One broad brand query plus site:-scoped queries that
    # surface TikTok / Instagram / Facebook content for the term DIRECTLY (the
    # search provider returns the platform's own public URLs, which we then enrich
    # via oEmbed for TikTok videos). TikTok is queried first so it is included even
    # if SEARCH_MAX_QUERIES is lowered. Override the cap for a raised/paid quota.
    base = [
        'site:tiktok.com "%s"' % term,       # TikTok videos + profiles for the term
        '"%s"' % term,                       # broad open-web brand mentions (news, blogs, forums)
        'site:instagram.com "%s"' % term,    # Instagram content for the term
        'site:facebook.com "%s"' % term,     # Facebook content for the term
    ]
    q = [x for x in base if x]
    for h in (hashtags or [])[:1]:
        ht = h if str(h).startswith("#") else "#" + str(h)
        q.append('site:tiktok.com "%s"' % ht)
    seen, out = set(), []
    for it in q:
        if it not in seen:
            seen.add(it)
            out.append(it)
    try:
        cap = int(os.getenv("SEARCH_MAX_QUERIES") or 4)
    except ValueError:
        cap = 4
    return out[:max(1, cap)]


# ── open web search provider adapter (GATED; official APIs only) ─────────────
def search_provider_configured():
    if not os.getenv("SEARCH_API_KEY"):
        return False
    provider = (os.getenv("SEARCH_PROVIDER") or "").strip().lower()
    return bool(provider or os.getenv("SEARCH_API_ENDPOINT"))


def search_provider_status():
    if search_provider_configured():
        return {"configured": True, "provider": os.getenv("SEARCH_PROVIDER") or "custom"}
    return {"configured": False,
            "message": "Open web social discovery requires a search provider API key or existing open web connector. "
                       "Set SEARCH_PROVIDER, SEARCH_API_KEY, and SEARCH_API_ENDPOINT."}


_PROVIDER_NAMES = {"brave": "Brave", "bing": "Bing", "custom": "Custom"}


def discovery_status():
    """Safe, non-secret status of the open-web discovery provider for the UI.
    Reports only booleans and the provider NAME. The SEARCH_API_KEY value is
    never read into the response."""
    provider = (os.getenv("SEARCH_PROVIDER") or "").strip().lower()
    key = bool(os.getenv("SEARCH_API_KEY"))
    can = search_provider_configured()
    msg = None if can else "Open web social discovery requires a server-side search provider key."
    return {
        "provider": _PROVIDER_NAMES.get(provider, provider.title()) if provider else None,
        "provider_configured": bool(provider),
        "key_configured": key,
        "endpoint_configured": bool(os.getenv("SEARCH_API_ENDPOINT")),
        "can_collect": can,
        "message": msg,
        "last_error": _LAST_SEARCH_ERROR or None,
    }


def _freshness(provider, start, end):
    """Map the app date range to each provider's freshness/recency parameter,
    where the provider supports it. Returns None when not applicable."""
    if not start or not end:
        return None
    try:
        from datetime import datetime
        days = (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days
    except Exception:
        return None
    if provider == "brave":
        return "%sto%s" % (start, end)                 # Brave supports a custom date range
    if provider == "bing":
        return "Day" if days <= 1 else ("Week" if days <= 7 else "Month")
    return None


def _parse_search_results(data):
    """Normalize common official search-provider JSON shapes to {title,url,snippet}."""
    if not isinstance(data, dict):
        return []
    out = []
    candidates = []
    if isinstance(data.get("web"), dict):
        candidates = data["web"].get("results", [])            # Brave
    elif isinstance(data.get("webPages"), dict):
        candidates = data["webPages"].get("value", [])         # Bing
    elif isinstance(data.get("organic_results"), list):
        candidates = data["organic_results"]                   # SerpAPI
    elif isinstance(data.get("items"), list):
        candidates = data["items"]                             # Google CSE
    elif isinstance(data.get("results"), list):
        candidates = data["results"]                           # generic
    for it in candidates:
        if not isinstance(it, dict):
            continue
        url = it.get("url") or it.get("link") or it.get("href")
        if not url:
            continue
        out.append({"title": it.get("title") or it.get("name") or "",
                    "url": url,
                    "snippet": it.get("snippet") or it.get("description") or it.get("content") or ""})
    return out


_LAST_SEARCH_ERROR = ""  # last provider error (e.g. quota), surfaced to the UI/sweep


def search_provider_query(query, count=8, start=None, end=None):
    """Query a CONFIGURED, official open-web search provider JSON API and return
    [{title,url,snippet}]. Returns [] when no provider is configured. It never
    scrapes search-engine result HTML. Callers must respect the provider's terms
    and robots policy. SEARCH_API_KEY is read server-side only and is never logged.
    Provider order of preference: brave (primary), bing, custom."""
    provider = (os.getenv("SEARCH_PROVIDER") or "").lower()
    key = os.getenv("SEARCH_API_KEY")
    endpoint = os.getenv("SEARCH_API_ENDPOINT")
    if not key or not (provider or endpoint):
        return []
    headers = {"User-Agent": UA, "Accept": "application/json"}
    fresh = _freshness(provider, start, end)
    if provider == "brave":
        endpoint = endpoint or "https://api.search.brave.com/res/v1/web/search"
        headers["X-Subscription-Token"] = key
        params = {"q": query, "count": min(count, 20)}
        if fresh:
            params["freshness"] = fresh
    elif provider == "bing":
        endpoint = endpoint or "https://api.bing.microsoft.com/v7.0/search"
        headers["Ocp-Apim-Subscription-Key"] = key
        params = {"q": query, "count": min(count, 50), "responseFilter": "Webpages"}
        if fresh:
            params["freshness"] = fresh
    else:  # custom / generic JSON endpoint with bearer auth
        if not endpoint:
            return []
        headers["Authorization"] = "Bearer " + key
        params = {"q": query, "count": count}
    url = endpoint + ("&" if "?" in endpoint else "?") + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers=headers)
        data = json.loads(urllib.request.urlopen(req, timeout=12).read().decode("utf-8", "ignore"))
    except Exception as e:
        # The key is read server-side only; scrub it from any exception text before
        # logging so it is never recorded as a value.
        global _LAST_SEARCH_ERROR
        msg = str(e)
        if key:
            msg = msg.replace(key, "***")
        if "429" in msg or "quota" in msg.lower() or "limit" in msg.lower():
            _LAST_SEARCH_ERROR = ("Brave Search monthly quota/credit reached. Raise the spending limit in the Brave "
                                  "API dashboard, or wait for the monthly reset.")
        else:
            _LAST_SEARCH_ERROR = "%s search error: %s" % (provider or "custom", msg[:100])
        print("  [open_web_discovery] %s" % _LAST_SEARCH_ERROR[:160])
        return []
    return _parse_search_results(data)


# ── 2. known URL enrichment: TikTok oEmbed (official, known URLs only) ───────
def enrich_known_tiktok_url_oembed(url):
    """Enrich a KNOWN TikTok VIDEO URL via TikTok's official oEmbed endpoint. It
    does NOT search, crawl profiles, fetch hashtag pages, enumerate videos, or
    bypass platform search. Input must be a TikTok video URL."""
    if extract_platform_from_url(url) != "tiktok" or "/video/" not in (url or "").lower():
        return None
    try:
        req = urllib.request.Request("https://www.tiktok.com/oembed?url=" + urllib.parse.quote(url, safe=""),
                                     headers={"User-Agent": UA, "Accept": "application/json"})
        o = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "ignore"))
    except Exception:
        return None
    title = o.get("title") or ""
    return {"title": title, "author_name": o.get("author_name"), "author_url": o.get("author_url"),
            "provider_name": o.get("provider_name") or "TikTok", "thumbnail_url": o.get("thumbnail_url"),
            "source_url": url, "hashtags": extract_hashtags_from_text(title + " " + (o.get("html") or ""))}


# ── normalize a discovered result into a mention record ──────────────────────
def normalize_open_web_reference(result, term=None):
    """Turn a search result {title,url,snippet} into a raw mention record tagged
    with a source_mode. source_truth.normalize_mention_source then attaches the
    honest coverage labels (open-web reference, not platform-native data)."""
    url = result.get("url") or result.get("link") or ""
    title = result.get("title") or result.get("name") or ""
    snippet = result.get("snippet") or result.get("description") or ""
    content = (title + " " + snippet).strip() or url
    return {"platform": "open_web", "platform_post_id": url or _short_id(title),
            "author": _domain(url), "content": content, "url": url,
            "posted_at": result.get("date") or result.get("posted_at"),
            "engagement": 0, "source_mode": classify_discovered_url(url)}


def enrich_known_urls(urls, term=None):
    """Enrich a list of MANUALLY supplied URLs. Only TikTok VIDEO URLs are
    accepted (enriched via official oEmbed). Every other URL is rejected. This is
    not search: it never crawls profiles, fetches hashtag pages, or enumerates
    videos. Returns {records, accepted, rejected}."""
    if isinstance(urls, str):
        urls = [u for u in re.split(r"[\s,]+", urls) if u.strip()]
    records, accepted, rejected = [], [], []
    for raw in (urls or []):
        url = (str(raw) or "").strip().rstrip(".,);]")
        if not url:
            continue
        if extract_platform_from_url(url) != "tiktok" or "/video/" not in url.lower():
            rejected.append(url)
            continue
        e = enrich_known_tiktok_url_oembed(url)
        if not e:
            rejected.append(url)
            continue
        extra = (" by " + e["author_name"]) if e.get("author_name") else ""
        content = ((e.get("title") or "TikTok video") + extra
                   + ((" " + " ".join(e["hashtags"])) if e.get("hashtags") else "")).strip()
        records.append({"platform": "open_web", "platform_post_id": url, "author": e.get("author_name"),
                        "content": content, "url": url, "posted_at": None, "engagement": 0,
                        "source_mode": "tiktok_oembed_known_url"})
        accepted.append(url)
    return {"records": records, "accepted": accepted, "rejected": rejected}


def source_truth_for_discovery(result):
    """Honest source-truth facts for a discovered result (used in tests/docs).
    The live pipeline applies these via source_truth using source_mode."""
    url = result.get("url") or result.get("link") or ""
    mode = result.get("source_mode") or classify_discovered_url(url)
    plat = extract_platform_from_url(url)
    discussed = [_PLATFORM_DISP[plat]] if plat in _PLATFORM_DISP else []
    if mode == "tiktok_oembed_known_url":
        return {"source_mode": mode, "searched_platform": "known TikTok URL enrichment",
                "display_platform": "TikTok URL", "discussed_platforms": discussed or ["TikTok"],
                "direct_platform_data": False, "coverage_type": "known_url_enrichment",
                "coverage_note": "TikTok oEmbed enriches a known public video URL. It does not perform TikTok keyword search."}
    return {"source_mode": mode, "searched_platform": "open web", "display_platform": "Open Web / News",
            "discussed_platforms": discussed, "direct_platform_data": False,
            "coverage_type": "open_web_reference",
            "coverage_note": "This is an open web result referencing a platform. It is not direct platform data."}


# ── the collector wired into the access ladder (gated unless configured) ─────
def collect_open_web_discovery(term, start=None, end=None, limit=24, hashtags=None, enrich_oembed=True):
    """Run controlled open-web discovery queries via the configured search
    provider. Enriches discovered TikTok video URLs via official oEmbed. Returns
    [] when no search provider is configured (the source stays gated)."""
    global _LAST_SEARCH_ERROR
    if not search_provider_configured():
        return []
    queries = build_open_web_queries(term, hashtags)
    if not queries:
        return []
    _LAST_SEARCH_ERROR = ""
    per = max(3, limit // max(len(queries), 1))
    seen, out = set(), []
    for q in queries:
        # No recency/date restriction on discovery: we want the brand's evergreen
        # social footprint. TikTok / Instagram / Facebook pages are rarely
        # date-stamped, so a search-provider freshness filter drops them entirely.
        # Results are stored with the collection date and filtered by range at
        # display time, so date scoping still applies to what the user sees.
        for r in search_provider_query(q, count=per):
            url = r.get("url") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            rec = normalize_open_web_reference(r, term)
            if enrich_oembed and rec["source_mode"] == "tiktok_oembed_known_url":
                e = enrich_known_tiktok_url_oembed(url)
                if e:
                    extra = (" by " + e["author_name"]) if e.get("author_name") else ""
                    rec["content"] = ((e.get("title") or rec["content"]) + extra
                                      + (" " + " ".join(e["hashtags"]) if e.get("hashtags") else "")).strip()
                    rec["author"] = e.get("author_name") or rec["author"]
                else:
                    rec["source_mode"] = "open_web_social_discovery"  # could not enrich; open-web reference to TikTok
            out.append(rec)
            if len(out) >= limit:
                return out
    if not out and _LAST_SEARCH_ERROR:
        raise RuntimeError(_LAST_SEARCH_ERROR)
    return out
