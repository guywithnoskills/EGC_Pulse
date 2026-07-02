"""
compliant-social-connectors. The platform-access governance module.

This module is the single place that decides HOW a source may be accessed. It
encodes a hard rule: prefer official APIs → connected (owned) accounts →
licensed data providers. It NEVER permits scraping, headless browsers, fake
accounts, session-cookie reuse, or unofficial private APIs.

Each source carries a capability matrix entry (the connector checklist):
  auth · data_types · date_range_support · max_lookback · rate_limit ·
  permissions · historical_modes · storage · display/cache/export/resell ·
  compliance_note · coverage()

pulse_demo.py imports from here for both collection and source metadata, so the
UI can show honest per-platform status and coverage disclosures.
"""
import base64
import json
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

UA = "egc-pulse/0.2 (compliant social listening; contact: listening@egcgroup.com)"

# ── Status labels (exact set required by the product) ────────────────────────
LIVE = "Live"
API_KEY = "Requires API key"
CONNECTED = "Requires connected account"
RESEARCH = "Requires approved research access"
LICENSED = "Requires licensed data provider"
NONE = "Not available compliantly"

# ── Historical modes ─────────────────────────────────────────────────────────
HISTORICAL_MODES = [
    "recent_only",
    "official_archive",
    "licensed_archive",
    "connected_account_history",
    "research_api",
]


# ── helpers ──────────────────────────────────────────────────────────────────
def now_iso():
    return datetime.now(timezone.utc).isoformat()


def fetch_json(url, timeout=10, headers=None):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def strip_html(s):
    import html as _html
    return _html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def env(*names):
    """True if every named env var is set (non-empty)."""
    return all(os.environ.get(n) for n in names)


def chunk_range(start, end, max_days):
    """Split [start,end] (YYYY-MM-DD) into <=max_days windows for APIs with a
    max date-window limit (e.g. TikTok Research API). Returns list of (s,e)."""
    if not start or not end or not max_days:
        return [(start, end)]
    fmt = "%Y-%m-%d"
    try:
        s = datetime.strptime(start, fmt)
        e = datetime.strptime(end, fmt)
    except ValueError:
        return [(start, end)]
    out = []
    cur = s
    from datetime import timedelta
    while cur <= e:
        win_end = min(cur + timedelta(days=max_days - 1), e)
        out.append((cur.strftime(fmt), win_end.strftime(fmt)))
        cur = win_end + timedelta(days=1)
    return out


def dedupe(records):
    """Dedupe by (platform, platform_post_id) then by (platform, url)."""
    seen, out = set(), []
    for r in records:
        k = (r.get("platform"), r.get("platform_post_id") or r.get("url"))
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def _iso_from_date(d, end=False):
    return (d + ("T23:59:59Z" if end else "T00:00:00Z")) if d else None


# ── OFFICIAL-API connectors (real) ───────────────────────────────────────────
# Reddit app-only OAuth bearer tokens live ~1h; cache per-process so a multi-term
# sweep doesn't re-authenticate (and burn rate-limit) on every single call.
_reddit_token = {"value": None, "exp": 0.0}


def reddit_user_agent():
    """Reddit REQUIRES a unique, descriptive User-Agent of the form
    '<platform>:<app-id>:<version> (by /u/<username>)'. Generic agents
    ('Python/urllib', 'Java', ...) are heavily throttled or blocked. Set
    REDDIT_USER_AGENT to include your own Reddit username."""
    return os.environ.get("REDDIT_USER_AGENT") or "python:egc-pulse:v1.0 (by /u/egc-pulse)"


def _reddit_bearer_token():
    """Fetch + cache an application-only OAuth token via the client_credentials
    grant. Valid for confidential clients (Reddit app type 'script' or 'web app'
    that hold a client_secret); app-only read access covers /search. Raises a
    classified RuntimeError on failure so the caller can surface WHY (instead of
    silently returning nothing)."""
    if _reddit_token["value"] and time.time() < _reddit_token["exp"]:
        return _reddit_token["value"]
    auth = base64.b64encode(
        ("%s:%s" % (os.environ["REDDIT_CLIENT_ID"], os.environ["REDDIT_CLIENT_SECRET"])).encode()
    ).decode()
    req = urllib.request.Request(
        "https://www.reddit.com/api/v1/access_token",
        data=b"grant_type=client_credentials",
        headers={"Authorization": "Basic " + auth, "User-Agent": reddit_user_agent(),
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=12).read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise RuntimeError("reddit auth 401: client_id/secret rejected. Verify the keys and that the app type is 'script' or 'web app'.")
        if e.code == 403:
            raise RuntimeError("reddit auth 403: forbidden. The app likely needs Data API access approval (Reddit Responsible Builder Policy).")
        if e.code == 429:
            raise RuntimeError("reddit auth 429: rate limited while fetching a token.")
        raise RuntimeError("reddit auth HTTP %d while fetching a token." % e.code)
    except Exception as e:
        raise RuntimeError("reddit auth failed (network/TLS): %s" % (str(e)[:80]))
    tok = resp.get("access_token")
    if not tok:
        raise RuntimeError("reddit auth: no access_token returned (error=%s)." % resp.get("error", "unknown"))
    _reddit_token["value"] = tok
    _reddit_token["exp"] = time.time() + min(int(resp.get("expires_in", 3600)) - 60, 3500)
    return tok


def collect_reddit(term, start=None, end=None, limit=100):
    """Official Reddit Data API (OAuth app-only / client_credentials). Site-wide
    post search, recency-oriented: the public search has no reliable date filter,
    so the date range is applied to stored data; deep history needs a licensed/
    archive adapter. NOTE: /search returns POSTS (links) only, not comments."""
    if not env("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"):
        return []
    token = _reddit_bearer_token()
    url = "https://oauth.reddit.com/search?" + urllib.parse.urlencode({
        "q": term, "sort": "new", "limit": max(1, min(int(limit or 100), 100)),
        "type": "link", "restrict_sr": "false", "raw_json": 1})
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token, "User-Agent": reddit_user_agent()})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RuntimeError("reddit 429: rate limited (limit is 100 QPM per client_id). Back off and retry.")
        if e.code in (401, 403):
            _reddit_token["value"] = None  # token stale/rejected — force a fresh auth next call
            raise RuntimeError("reddit search HTTP %d: token rejected or Data API access not approved." % e.code)
        raise RuntimeError("reddit search HTTP %d." % e.code)
    except Exception as e:
        raise RuntimeError("reddit search failed: %s" % (str(e)[:80]))
    out = []
    for c in data.get("data", {}).get("children", []):
        d = c.get("data", {})
        out.append({
            "platform": "reddit", "platform_post_id": d.get("id"), "author": d.get("author"),
            "content": (d.get("title") or "") + (("\n" + d["selftext"]) if d.get("selftext") else ""),
            "url": "https://www.reddit.com" + (d.get("permalink") or ""),
            "posted_at": datetime.fromtimestamp(d.get("created_utc", 0), timezone.utc).isoformat(),
            "engagement": int(d.get("score", 0)) + int(d.get("num_comments", 0)),
            "extra": {"subreddit": d.get("subreddit"), "score": d.get("score"),
                      "comments": d.get("num_comments")},
        })
    return out


def _youtube_stats(video_ids, key):
    """Batch-fetch public statistics (views/likes/comments) for up to 50 video
    ids per videos.list call (1 quota unit each). Best-effort — returns {} on
    any error so a stats hiccup never blocks the mentions themselves."""
    out = {}
    ids = [v for v in (video_ids or []) if v]
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        url = "https://www.googleapis.com/youtube/v3/videos?" + urllib.parse.urlencode(
            {"part": "statistics", "id": ",".join(chunk), "key": key})
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=12) as resp:
                d = json.loads(resp.read().decode("utf-8", "ignore"))
        except Exception:
            continue
        for it in d.get("items", []):
            s = it.get("statistics") or {}
            out[it.get("id")] = {"views": int(s.get("viewCount") or 0),
                                 "likes": int(s.get("likeCount") or 0),
                                 "comments": int(s.get("commentCount") or 0)}
    return out


def collect_youtube(term, start=None, end=None, limit=50):
    """Official YouTube Data API v3 search.list (API-key auth, public data only).
    Returns recent videos whose title/description match the term. Quota: a
    search.list call costs 100 units and the free tier is 10,000 units/day
    (~100 searches/day). No OAuth and no approval gate — a self-service Google
    Cloud API key is enough. Raises a classified error so failures aren't silent."""
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        return []
    params = {"key": key, "q": term, "part": "snippet", "type": "video",
              "order": "date", "maxResults": max(1, min(int(limit or 50), 50)),
              "regionCode": "US", "relevanceLanguage": "en"}   # US-localized results
    if start:
        params["publishedAfter"] = _iso_from_date(start)
    if end:
        params["publishedBefore"] = _iso_from_date(end, end=True)
    url = "https://www.googleapis.com/youtube/v3/search?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise RuntimeError("youtube 403: API key invalid, 'YouTube Data API v3' not enabled, or daily quota (10k units) exceeded.")
        if e.code == 400:
            raise RuntimeError("youtube 400: bad request (check the API key value).")
        if e.code == 429:
            raise RuntimeError("youtube 429: rate limited.")
        raise RuntimeError("youtube search HTTP %d." % e.code)
    except Exception as e:
        raise RuntimeError("youtube search failed: %s" % (str(e)[:80]))
    items = data.get("items", [])
    stats = _youtube_stats([(it.get("id") or {}).get("videoId") for it in items], key)
    out = []
    for it in items:
        vid = (it.get("id") or {}).get("videoId")
        sn = it.get("snippet") or {}
        if not vid:
            continue
        st = stats.get(vid, {})
        views, likes, comments = st.get("views", 0), st.get("likes", 0), st.get("comments", 0)
        title = strip_html(sn.get("title") or "")
        desc = strip_html(sn.get("description") or "")
        out.append({
            "platform": "youtube", "platform_post_id": vid,
            "author": strip_html(sn.get("channelTitle") or "") or None,
            "content": title + (("\n" + desc) if desc else ""),
            "url": "https://www.youtube.com/watch?v=" + vid,
            "posted_at": sn.get("publishedAt") or now_iso(),
            "engagement": likes + comments,   # interactions
            "reach": views,                    # views = public reach/visibility proxy
            "extra": {"channel": sn.get("channelTitle"), "channel_id": sn.get("channelId"),
                      "views": views, "likes": likes, "comments": comments},
        })
    return out


def collect_x(term, start=None, end=None, limit=50):
    """Official X API v2. Recent Search (last 7 days) by default; Full-Archive
    Search when the account has access (set X_ARCHIVE=full). Date range is passed
    via the official start_time/end_time parameters."""
    if not env("X_BEARER_TOKEN"):
        return []
    full = os.environ.get("X_ARCHIVE", "recent").lower() == "full"
    endpoint = "all" if full else "recent"
    params = {
        "query": term + " -is:retweet",
        "max_results": min(max(limit, 10), 100),
        "tweet.fields": "created_at,public_metrics,lang",
        "expansions": "author_id", "user.fields": "username",
    }
    if _iso_from_date(start):
        params["start_time"] = _iso_from_date(start)
    if _iso_from_date(end, True):
        params["end_time"] = _iso_from_date(end, True)
    try:
        req = urllib.request.Request(
            "https://api.x.com/2/tweets/search/%s?%s" % (endpoint, urllib.parse.urlencode(params)),
            headers={"Authorization": "Bearer " + os.environ["X_BEARER_TOKEN"], "User-Agent": UA})
        data = json.loads(urllib.request.urlopen(req, timeout=10).read())
    except Exception as e:
        print("  [x] %s" % e)
        return []
    users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
    out = []
    for t in data.get("data", []):
        u = users.get(t.get("author_id"), {})
        pm = t.get("public_metrics", {})
        out.append({
            "platform": "x", "platform_post_id": t.get("id"),
            "author": ("@" + u["username"]) if u.get("username") else None, "content": t.get("text"),
            "url": "https://x.com/%s/status/%s" % (u.get("username", "i/web"), t.get("id")),
            "lang": t.get("lang"), "posted_at": t.get("created_at"),
            "engagement": sum(int(pm.get(k, 0)) for k in
                              ("like_count", "retweet_count", "reply_count", "quote_count")),
        })
    return out


# ── COMPLIANT connectors for closed platforms (no scraping; honest stubs) ─────
# These NEVER scrape. They use official authorized paths only and return []
# (with a clear log) when the required app review / connected account / licensed
# feed is not configured. They must never fabricate public listening data.
def collect_instagram(term, start=None, end=None, limit=40):
    if env("LICENSED_PROVIDER_API_KEY"):
        return _licensed("instagram", term, start, end, limit)
    # Owned-account / hashtag search needs META_ACCESS_TOKEN + app review + a
    # connected Business/Creator account. Not fabricated here.
    if env("META_ACCESS_TOKEN"):
        print("  [instagram] connected-account path requires Meta app review + IG Business ID; no public search")
    return []


def collect_facebook(term, start=None, end=None, limit=40):
    if env("LICENSED_PROVIDER_API_KEY"):
        return _licensed("facebook", term, start, end, limit)
    if env("META_ACCESS_TOKEN"):
        print("  [facebook] owned-Page path requires connected Page + app review; no public search")
    return []


def collect_tiktok(term, start=None, end=None, limit=40):
    if env("LICENSED_PROVIDER_API_KEY"):
        return _licensed("tiktok", term, start, end, limit)
    # Research API supports a max date window. Caller should chunk_range() it.
    if env("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"):
        print("  [tiktok] Research API requires approval + chunked date windows; not auto-enabled")
    return []


def _licensed(platform, term, start, end, limit):
    """Adapter for a licensed data provider (e.g. a firehose reseller). Wired to
    LICENSED_PROVIDER_API_KEY + LICENSED_PROVIDER_URL. Returns [] until pointed
    at a real provider. It does not invent data."""
    base = os.environ.get("LICENSED_PROVIDER_URL")
    if not base:
        print("  [%s] LICENSED_PROVIDER_API_KEY set but LICENSED_PROVIDER_URL missing" % platform)
        return []
    try:
        # Send common parameter aliases so the same adapter fits different
        # provider contracts (platform, query, start_date, end_date).
        q = urllib.parse.urlencode({"platform": platform, "q": term, "query": term,
                                    "start": start or "", "end": end or "",
                                    "start_date": start or "", "end_date": end or "", "limit": limit})
        data = fetch_json(base + "?" + q,
                          headers={"Authorization": "Bearer " + os.environ["LICENSED_PROVIDER_API_KEY"]})
        # Provider data is labeled licensed_provider downstream by source_truth.
        return data.get("mentions", []) if isinstance(data, dict) else []
    except Exception as e:
        print("  [%s/licensed] %s" % (platform, e))
        return []


# ── OWNED-ACCOUNT ANALYTICS (impressions / reach / engagement) ────────────────
# The ONLY compliant source of impressions & reach. These are private metrics
# that ONLY the account owner can see, so this works for accounts that have
# authorized THIS app via Meta Login. Your own or your clients'. Never
# third-party, never scraped. Returns a clear "not configured" payload until a
# connected account + token are present; it never fabricates numbers.
GRAPH = "https://graph.facebook.com/v21.0"


def _insights_series(data):
    out = []
    for m in (data.get("data") or []):
        vals = m.get("values") or []
        out.append({"metric": m.get("name"), "title": m.get("title") or m.get("name"),
                    "total": sum(int(v.get("value") or 0) for v in vals),
                    "series": [{"t": v.get("end_time"), "value": v.get("value")} for v in vals]})
    return out


def meta_account_insights(start=None, end=None):
    """Instagram Business/Creator account insights (impressions, reach,
    profile_views) via the Meta Graph API, for an account that connected this
    app. Owner-authorized data only. Metric names can shift by Graph API
    version; these follow the current documented account-level metrics."""
    token, ig = os.environ.get("META_ACCESS_TOKEN"), os.environ.get("META_IG_USER_ID")
    if not (token and ig):
        return {"platform": "instagram", "configured": False,
                "needs": ["META_ACCESS_TOKEN", "META_IG_USER_ID"],
                "note": "Connect an owner-authorized Instagram Business/Creator account to load impressions & reach.",
                "metrics": []}
    try:
        url = "%s/%s/insights?%s" % (GRAPH, ig, urllib.parse.urlencode(
            {"metric": "impressions,reach,profile_views", "period": "day", "access_token": token}))
        return {"platform": "instagram", "configured": True, "ok": True, "account_id": ig,
                "source": "meta_graph_api_insights", "metrics": _insights_series(fetch_json(url))}
    except urllib.error.HTTPError as e:
        return {"platform": "instagram", "configured": True, "ok": False, "metrics": [],
                "error": "Meta rejected the request (HTTP %d). Check the token, the connected IG Business "
                         "account, and instagram_manage_insights permission / app review." % e.code}
    except Exception as e:
        return {"platform": "instagram", "configured": True, "ok": False, "metrics": [], "error": str(e)[:160]}


def facebook_page_insights(start=None, end=None):
    """Owned Facebook Page insights (impressions, unique reach) via the Meta
    Graph API, for a Page this app manages. Owner-authorized data only."""
    token, page = os.environ.get("META_ACCESS_TOKEN"), os.environ.get("META_FB_PAGE_ID")
    if not (token and page):
        return {"platform": "facebook", "configured": False,
                "needs": ["META_ACCESS_TOKEN", "META_FB_PAGE_ID"],
                "note": "Connect an owned Facebook Page to load impressions & reach.",
                "metrics": []}
    try:
        url = "%s/%s/insights?%s" % (GRAPH, page, urllib.parse.urlencode(
            {"metric": "page_impressions,page_impressions_unique,page_post_engagements",
             "period": "day", "access_token": token}))
        return {"platform": "facebook", "configured": True, "ok": True, "page_id": page,
                "source": "meta_graph_api_insights", "metrics": _insights_series(fetch_json(url))}
    except urllib.error.HTTPError as e:
        return {"platform": "facebook", "configured": True, "ok": False, "metrics": [],
                "error": "Meta rejected the request (HTTP %d). Check the token, the connected Page, and "
                         "pages_read_engagement permission / app review." % e.code}
    except Exception as e:
        return {"platform": "facebook", "configured": True, "ok": False, "metrics": [], "error": str(e)[:160]}


# ── OPEN / keyless connectors (genuinely compliant public APIs) ───────────────
def collect_mastodon(term, start=None, end=None, limit=25, instance="mastodon.social"):
    toks = re.findall(r"[A-Za-z0-9]+", term)
    if not toks:
        return []
    try:
        data = fetch_json("https://%s/api/v1/timelines/tag/%s?limit=%d" % (instance, toks[0], min(limit, 40)))
    except Exception as e:
        print("  [mastodon] %s" % e)
        return []
    out = []
    for s in data:
        acct = (s.get("account") or {}).get("acct", "")
        out.append({"platform": "mastodon", "platform_post_id": s.get("id"),
                    "author": ("@" + acct) if acct else None, "content": strip_html(s.get("content")),
                    "url": s.get("url"), "lang": (s.get("language") or "en")[:2],
                    "posted_at": s.get("created_at") or now_iso(),
                    "engagement": int(s.get("favourites_count") or 0) + int(s.get("reblogs_count") or 0)})
    return out


def collect_bluesky(term, start=None, end=None, limit=25):
    """Bluesky / AT Protocol public post search via the official AppView.
    No key, no auth, no scraping. Free. (searchPosts is served by api.bsky.app;
    the public.api.bsky.app host gates search behind auth.)"""
    if not term:
        return []
    try:
        data = fetch_json("https://api.bsky.app/xrpc/app.bsky.feed.searchPosts?" +
                          urllib.parse.urlencode({"q": term, "limit": min(limit, 100)}))
    except Exception as e:
        print("  [bluesky] %s" % e)
        return []
    out = []
    for p in (data.get("posts", []) if isinstance(data, dict) else []):
        rec = p.get("record") or {}
        author = p.get("author") or {}
        handle = author.get("handle") or ""
        uri = p.get("uri") or ""
        rkey = uri.rsplit("/", 1)[-1] if uri else ""
        url = ("https://bsky.app/profile/%s/post/%s" % (handle, rkey)) if (handle and rkey) else None
        out.append({"platform": "bluesky", "platform_post_id": p.get("cid") or uri,
                    "author": ("@" + handle) if handle else (author.get("displayName") or None),
                    "content": rec.get("text"), "url": url,
                    "posted_at": rec.get("createdAt") or p.get("indexedAt") or now_iso(),
                    "engagement": int(p.get("likeCount") or 0) + int(p.get("repostCount") or 0) + int(p.get("replyCount") or 0)})
    return out


def collect_lemmy(term, start=None, end=None, limit=25, instance="lemmy.world"):
    try:
        data = fetch_json("https://%s/api/v3/search?%s" % (instance, urllib.parse.urlencode(
            {"q": term, "type_": "Posts", "sort": "New", "limit": min(limit, 40)})))
    except Exception as e:
        print("  [lemmy] %s" % e)
        return []
    out = []
    for row in data.get("posts", []):
        p, cr, ct = row.get("post", {}), row.get("creator", {}), row.get("counts", {})
        out.append({"platform": "lemmy", "platform_post_id": str(p.get("id")), "author": cr.get("name"),
                    "content": (p.get("name") or "") + (("\n" + p["body"]) if p.get("body") else ""),
                    "url": p.get("ap_id") or p.get("url"), "posted_at": p.get("published") or now_iso(),
                    "engagement": int(ct.get("score", 0)) + int(ct.get("comments", 0))})
    return out


def collect_hn(term, start=None, end=None, limit=25):
    try:
        data = fetch_json("https://hn.algolia.com/api/v1/search?"
                          + urllib.parse.urlencode({"query": term, "tags": "story", "hitsPerPage": limit}))
    except Exception as e:
        print("  [hn] %s" % e)
        return []
    out = []
    for h in data.get("hits", []):
        ts = h.get("created_at_i")
        out.append({"platform": "hackernews", "platform_post_id": h.get("objectID"), "author": h.get("author"),
                    "content": h.get("title"), "url": "https://news.ycombinator.com/item?id=%s" % h.get("objectID"),
                    "lang": "en",
                    "posted_at": datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else now_iso(),
                    "engagement": int(h.get("points") or 0) + int(h.get("num_comments") or 0)})
    return out


def collect_peertube(term, start=None, end=None, limit=20):
    try:
        data = fetch_json("https://sepiasearch.org/api/v1/search/videos?"
                          + urllib.parse.urlencode({"search": term, "count": min(limit, 20), "sort": "-publishedAt"}))
    except Exception as e:
        print("  [peertube] %s" % e)
        return []
    out = []
    for v in data.get("data", []):
        ch = v.get("channel") or {}
        out.append({"platform": "peertube", "platform_post_id": str(v.get("uuid") or v.get("id")),
                    "author": ch.get("displayName") or ch.get("name"),
                    "content": (v.get("name") or "") + ((". " + v["description"]) if v.get("description") else ""),
                    "url": v.get("url"), "posted_at": v.get("publishedAt") or now_iso(),
                    "engagement": int(v.get("views") or 0)})
    return out


def collect_gdelt(term, start=None, end=None, limit=25):
    try:
        data = fetch_json("https://api.gdeltproject.org/api/v2/doc/doc?"
                          + urllib.parse.urlencode({"query": term, "mode": "ArtList", "format": "json",
                                                    "maxrecords": min(limit, 50), "sort": "DateDesc"}))
    except Exception as e:
        print("  [news] %s" % e)
        return []
    out = []
    for a in data.get("articles", []):
        try:
            posted = datetime.strptime(a.get("seendate", ""), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).isoformat()
        except Exception:
            posted = now_iso()
        out.append({"platform": "news", "platform_post_id": a.get("url", ""), "author": a.get("domain"),
                    "content": a.get("title"), "url": a.get("url"),
                    "lang": (a.get("language") or "en")[:2].lower(), "posted_at": posted, "engagement": 0})
    return out


def collect_nostr(term, start=None, end=None, limit=20):
    try:
        evs = _nostr_ws()
    except Exception as e:
        print("  [nostr] %s" % e)
        return []
    out = []
    for ev in evs:
        content = ev.get("content", "") or ""
        if not re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", content, re.I):
            continue
        ts, eid = ev.get("created_at"), ev.get("id", "")
        out.append({"platform": "nostr", "platform_post_id": eid,
                    "author": (ev.get("pubkey", "")[:10] + "…") if ev.get("pubkey") else None,
                    "content": content, "url": "https://njump.me/" + eid,
                    "posted_at": datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else now_iso(),
                    "engagement": 0})
        if len(out) >= limit:
            break
    return out


def _nostr_ws(host="relay.damus.io", want=250, timeout=7):
    raw = socket.create_connection((host, 443), timeout=timeout)
    s = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall(("GET / HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (host, key)).encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        c = s.recv(4096)
        if not c:
            raise IOError("handshake closed")
        buf += c
    buf = buf.split(b"\r\n\r\n", 1)[1]
    sub = json.dumps(["REQ", "p", {"kinds": [1], "limit": want}]).encode()
    mask = os.urandom(4)
    hdr = bytearray([0x81])
    n = len(sub)
    hdr.append(0x80 | n) if n < 126 else (hdr.append(0x80 | 126) or hdr.extend(n.to_bytes(2, "big")))
    hdr += mask
    s.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(sub)))
    s.settimeout(timeout)
    events, start = [], time.time()

    def need(k):
        nonlocal buf
        while len(buf) < k:
            c = s.recv(8192)
            if not c:
                raise IOError("closed")
            buf += c
    try:
        while time.time() - start < timeout and len(events) < want:
            need(2)
            ln, off = buf[1] & 0x7f, 2
            if ln == 126:
                need(4); ln = int.from_bytes(buf[2:4], "big"); off = 4
            need(off + ln)
            op, payload, = buf[0] & 0x0f, buf[off:off + ln]
            buf = buf[off + ln:]
            if op == 0x8:
                break
            if op in (0x9, 0xA):
                continue
            try:
                m = json.loads(payload.decode("utf-8", "ignore"))
            except Exception:
                continue
            if isinstance(m, list) and m and m[0] == "EVENT":
                events.append(m[2])
            elif isinstance(m, list) and m and m[0] == "EOSE":
                break
    finally:
        try:
            s.close()
        except Exception:
            pass
    return events


# ── SOURCE REGISTRY + capability matrix (the connector checklist) ─────────────
SOURCES = [
    {"key": "reddit", "name": "Reddit", "tier": "focus", "collector": collect_reddit,
     "auth": "OAuth2 client-credentials (REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET)",
     "configured": lambda: env("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"),
     "base_status": API_KEY, "live_status": LIVE,
     "data_types": ["post", "comment", "author", "score", "permalink", "timestamp", "url"],
     "date_range_support": "filter_only", "max_lookback": "API recency-oriented; deep history via licensed/archive adapter",
     "rate_limit": "~100 QPM (OAuth)", "permissions": "Registered app; commercial use requires paid/approved tier",
     "historical_modes": ["recent_only", "licensed_archive"],
     "storage": "Store IDs + content; honor deletions", "display": True, "cache": True, "export": True, "resell": False,
     "coverage": "Reddit official API is recency-oriented. Deep history requires a licensed/archive provider.",
     "note": "Official Reddit Data API. No scraping."},

    {"key": "x", "name": "X / Twitter", "tier": "focus", "collector": collect_x,
     "auth": "App-only Bearer token (X_BEARER_TOKEN); X_ARCHIVE=full for full-archive",
     "configured": lambda: env("X_BEARER_TOKEN"),
     "base_status": API_KEY, "live_status": LIVE,
     "data_types": ["post", "author", "public_metrics", "lang", "timestamp", "url"],
     "date_range_support": "native", "max_lookback": "Recent = last 7 days; Full-archive (to 2006) needs paid/enterprise",
     "rate_limit": "Tiered; monthly post cap", "permissions": "Recent Search standard; Full-Archive needs paid/enterprise",
     "historical_modes": ["recent_only", "official_archive"],
     "storage": "Store IDs; rehydrate on read; honor deletions", "display": True, "cache": True, "export": True, "resell": False,
     "coverage": "X Recent Search covers the last 7 days. Full archive requires paid/enterprise access (X_ARCHIVE=full).",
     "note": "Official X API v2 with native start_time/end_time."},

    {"key": "instagram", "name": "Instagram", "tier": "focus", "collector": collect_instagram,
     "auth": "Meta Graph API (META_ACCESS_TOKEN) for owned/authorized accounts; or licensed provider",
     "configured": lambda: env("LICENSED_PROVIDER_API_KEY", "LICENSED_PROVIDER_URL"),
     "base_status": CONNECTED, "live_status": LICENSED,
     "data_types": ["owned media", "owned insights", "hashtag (limited)", "business discovery"],
     "date_range_support": "connected_only", "max_lookback": "Connected-account history; public history via licensed provider",
     "rate_limit": "Graph API per-app limits; hashtag = 30 unique / 7 days",
     "permissions": "Meta app review + connected IG Business/Creator account",
     "historical_modes": ["connected_account_history", "licensed_archive"],
     "storage": "Owned-account data per DPA; no public scraping", "display": True, "cache": True, "export": True, "resell": False,
     "coverage": "Instagram public keyword search is NOT available via API. Connect an owned Business/Creator account, or use a licensed provider for broad listening.",
     "note": "No scraping. Graph API (owned) or licensed provider only."},

    {"key": "facebook", "name": "Facebook", "tier": "focus", "collector": collect_facebook,
     "auth": "Meta Graph API (META_ACCESS_TOKEN) for owned Pages; or licensed provider",
     "configured": lambda: env("LICENSED_PROVIDER_API_KEY", "LICENSED_PROVIDER_URL"),
     "base_status": CONNECTED, "live_status": LICENSED,
     "data_types": ["owned page posts", "page insights", "comments (with permission)"],
     "date_range_support": "connected_only", "max_lookback": "Owned-Page history; public via licensed provider",
     "rate_limit": "Graph API per-app limits", "permissions": "Meta app review + connected Page",
     "historical_modes": ["connected_account_history", "licensed_archive"],
     "storage": "Owned-Page data per DPA; no public scraping", "display": True, "cache": True, "export": True, "resell": False,
     "coverage": "Public Facebook search is unavailable via API (CrowdTangle retired). Owned Pages via Graph API, or a licensed provider.",
     "note": "No scraping. Owned Pages (Graph API) or licensed provider only."},

    {"key": "tiktok", "name": "TikTok", "tier": "focus", "collector": collect_tiktok,
     "auth": "Research API (TIKTOK_CLIENT_KEY/SECRET, approved); Display/Commercial Content API; or licensed provider",
     "configured": lambda: env("LICENSED_PROVIDER_API_KEY", "LICENSED_PROVIDER_URL"),
     "base_status": RESEARCH, "live_status": LICENSED,
     "data_types": ["research: videos/comments/users (approved)", "owned account (Display)", "ads (Commercial Content)"],
     "date_range_support": "chunked", "max_lookback": "Research API max date-window per call. Chunk long ranges",
     "rate_limit": "Research API daily quotas", "permissions": "Research API approval (non-commercial) or licensed provider",
     "historical_modes": ["research_api", "connected_account_history", "licensed_archive"],
     "storage": "Inside approved environment; no public scraping", "display": True, "cache": True, "export": False, "resell": False,
     "coverage": "TikTok broad public history requires Research API approval (chunked date windows) or a licensed provider.",
     "note": "No scraping. Research/Display/Commercial APIs or licensed provider only."},

    # ── open / keyless live sources (genuinely compliant public APIs) ──
    {"key": "mastodon", "name": "Mastodon", "tier": "open", "collector": collect_mastodon,
     "auth": "None (public fediverse API)", "configured": lambda: True, "base_status": LIVE, "live_status": LIVE,
     "data_types": ["public posts (hashtag)", "author", "timestamp", "url"], "date_range_support": "filter_only",
     "max_lookback": "Recent public timeline", "rate_limit": "Per-instance", "permissions": "None",
     "historical_modes": ["recent_only"], "storage": "Public data", "display": True, "cache": True, "export": True, "resell": False,
     "coverage": "Mastodon public hashtag timeline (recent).", "note": "Open fediverse API."},
    {"key": "lemmy", "name": "Lemmy", "tier": "open", "collector": collect_lemmy,
     "auth": "None (public API)", "configured": lambda: True, "base_status": LIVE, "live_status": LIVE,
     "data_types": ["public posts", "author", "score", "url"], "date_range_support": "filter_only",
     "max_lookback": "Recent", "rate_limit": "Per-instance", "permissions": "None",
     "historical_modes": ["recent_only"], "storage": "Public data", "display": True, "cache": True, "export": True, "resell": False,
     "coverage": "Lemmy public post search (recent).", "note": "Open fediverse API."},
    {"key": "nostr", "name": "Nostr", "tier": "open", "collector": collect_nostr,
     "auth": "None (public relays)", "configured": lambda: True, "base_status": LIVE, "live_status": LIVE,
     "data_types": ["public notes", "pubkey", "timestamp", "url"], "date_range_support": "filter_only",
     "max_lookback": "Recent relay feed", "rate_limit": "Per-relay", "permissions": "None",
     "historical_modes": ["recent_only"], "storage": "Public data", "display": True, "cache": True, "export": True, "resell": False,
     "coverage": "Nostr recent relay feed, keyword-filtered.", "note": "Open protocol over WebSocket."},
    {"key": "peertube", "name": "PeerTube", "tier": "open", "collector": collect_peertube,
     "auth": "None (SepiaSearch)", "configured": lambda: True, "base_status": LIVE, "live_status": LIVE,
     "data_types": ["public videos", "channel", "views", "url"], "date_range_support": "filter_only",
     "max_lookback": "Recent", "rate_limit": "Public", "permissions": "None",
     "historical_modes": ["recent_only"], "storage": "Public data", "display": True, "cache": True, "export": True, "resell": False,
     "coverage": "PeerTube federated video search (recent).", "note": "Open federated search."},
    {"key": "hackernews", "name": "Hacker News", "tier": "open", "collector": collect_hn,
     "auth": "None (Algolia API)", "configured": lambda: True, "base_status": LIVE, "live_status": LIVE,
     "data_types": ["stories", "author", "points", "url"], "date_range_support": "filter_only",
     "max_lookback": "Full HN archive (by relevance)", "rate_limit": "Public", "permissions": "None",
     "historical_modes": ["recent_only", "official_archive"], "storage": "Public data", "display": True, "cache": True, "export": True, "resell": False,
     "coverage": "Hacker News public search.", "note": "Public Algolia API."},
    {"key": "news", "name": "News (GDELT)", "tier": "open", "collector": collect_gdelt,
     "auth": "None (GDELT)", "configured": lambda: True, "base_status": LIVE, "live_status": LIVE,
     "data_types": ["news articles", "domain", "lang", "url"], "date_range_support": "filter_only",
     "max_lookback": "Rolling recent window", "rate_limit": "Public (rate-limited)", "permissions": "None",
     "historical_modes": ["recent_only"], "storage": "Public metadata", "display": True, "cache": True, "export": True, "resell": False,
     "coverage": "Global news (GDELT), recent window.", "note": "Open news API."},
]

BY_KEY = {s["key"]: s for s in SOURCES}


def source_status(key):
    """Honest, current operational status per the required label set."""
    s = BY_KEY.get(key)
    if not s:
        return NONE
    if key in ("reddit", "x"):
        return LIVE if s["configured"]() else API_KEY
    if s["tier"] == "open":
        return LIVE
    # closed platforms: licensed provider makes broad listening live; otherwise
    # the nearest compliant path is shown honestly (never "Live" without data).
    if env("LICENSED_PROVIDER_API_KEY", "LICENSED_PROVIDER_URL"):
        return LIVE
    return RESEARCH if key == "tiktok" else CONNECTED


def matrix():
    """Capability matrix for the UI / docs. No functions, JSON-safe."""
    drop = ("collector", "configured", "base_status", "live_status")
    out = []
    for s in SOURCES:
        out.append({k: v for k, v in s.items() if k not in drop} | {
            "status": source_status(s["key"]), "configured": s["configured"]()})
    return out


def collectable():
    """Sources that can actually return data right now (configured + has collector)."""
    return [s["key"] for s in SOURCES if s["configured"]() and s["collector"]]


def collect_source(key, term, start=None, end=None):
    s = BY_KEY.get(key)
    if not s or not s["collector"] or not s["configured"]():
        return []
    return s["collector"](term, start, end)
