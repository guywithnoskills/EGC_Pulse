"""
source_truth.py. The SOURCE TRUTH / COVERAGE LEDGER layer.

Distinguishes what was ACTUALLY searched (direct platform data) from what was
merely DISCUSSED (open-web/news articles that mention a platform). Prevents the
core mislabel: a GDELT article saying "X went viral on TikTok" must NOT be shown
as "TikTok searched".

No scraping. This module only labels/normalizes the provenance of data that
compliant connectors already returned.
"""
import json
import re

# coverage_type vocabulary
DIRECT_OFFICIAL = "direct_official_api"
DIRECT_CONNECTED = "direct_connected_account"
DIRECT_RESEARCH = "direct_research_api"
DIRECT_TRANSPARENCY = "direct_transparency_api"
DIRECT_LICENSED = "direct_licensed_provider"
MANUAL = "manual_import"
OPEN_WEB_REF = "open_web_reference"
OPEN_NETWORK = "open_network_public"
HISTORICAL_OPEN_WEB = "historical_open_web"
UNAVAILABLE = "unavailable_compliantly"
LIMITED = "limited_metadata"
KNOWN_URL_ENRICHMENT = "known_url_enrichment"   # oEmbed of an already-known public URL (not a search)

# coverage_type -> short UI badge
BADGE = {
    DIRECT_OFFICIAL: "Direct API", DIRECT_CONNECTED: "Connected account",
    DIRECT_RESEARCH: "Research API", DIRECT_TRANSPARENCY: "Ads only",
    DIRECT_LICENSED: "Licensed feed", MANUAL: "Manual import",
    OPEN_WEB_REF: "Open-web reference", OPEN_NETWORK: "Open network",
    HISTORICAL_OPEN_WEB: "Historical open web", LIMITED: "Limited metadata",
    UNAVAILABLE: "Unavailable", KNOWN_URL_ENRICHMENT: "oEmbed (known URL)",
}

# direct_platform_data is true for everything except open-web references, known-URL
# enrichment, and limited/unavailable. Open-web and oEmbed are NOT direct platform data.
_NON_DIRECT = {OPEN_WEB_REF, HISTORICAL_OPEN_WEB, LIMITED, UNAVAILABLE, KNOWN_URL_ENRICHMENT}

CLOSED_PLATFORMS = {"TikTok", "Instagram", "Facebook"}

# canonical platform tokens for inferring DISCUSSED platforms from text/url
_DISCUSS = [
    ("TikTok", [r"tiktok", r"tik tok"], ["tiktok.com"]),
    ("Instagram", [r"instagram", r"\binsta\b", r"\big\b"], ["instagram.com"]),
    ("Facebook", [r"facebook", r"\bfb\b", r"meta platforms"], ["facebook.com", "fb.com"]),
    ("X / Twitter", [r"twitter", r"\bx\.com\b", r"\btweet"], ["twitter.com", "x.com"]),
    ("Reddit", [r"reddit", r"subreddit", r"r/"], ["reddit.com"]),
    ("YouTube", [r"youtube", r"yt\b"], ["youtube.com", "youtu.be"]),
    ("Threads", [r"threads\b"], ["threads.net"]),
    ("LinkedIn", [r"linkedin"], ["linkedin.com"]),
    ("Snapchat", [r"snapchat", r"snap inc"], ["snapchat.com"]),
]

# source_key -> source-truth facts. Keys match platform_access_manager mode keys.
SOURCE_TRUTH = {
    "news_gdelt": dict(sp="gdelt", disp="News", searched="open web / news",
                       ct=OPEN_WEB_REF, conf="source_verified_open_web_reference",
                       note="Open-web/news source that references a platform. Not platform-native social data."),
    "hackernews_public_api": dict(sp="hacker_news", disp="Hacker News", searched="hacker news",
                                  ct=OPEN_NETWORK, conf="source_verified_direct_platform",
                                  note="Hacker News public API result (direct to Hacker News, an open network)."),
    "mastodon_public_api": dict(sp="mastodon", disp="Mastodon", searched="mastodon public api",
                                ct=OPEN_NETWORK, conf="source_verified_direct_platform",
                                note="Mastodon public (fediverse) result. Not Instagram/Facebook/TikTok data."),
    "lemmy_public_api": dict(sp="lemmy", disp="Lemmy", searched="lemmy public api",
                             ct=OPEN_NETWORK, conf="source_verified_direct_platform",
                             note="Lemmy public (fediverse) result. Not Instagram/Facebook/TikTok data."),
    "bluesky_public_api": dict(sp="bluesky", disp="Bluesky", searched="bluesky public api",
                               ct=OPEN_NETWORK, conf="source_verified_direct_platform",
                               note="Bluesky public (AT Protocol) result. Direct to Bluesky, an open network."),
    "nostr_public_relays": dict(sp="nostr", disp="Nostr", searched="nostr relays",
                                ct=OPEN_NETWORK, conf="source_verified_direct_platform",
                                note="Nostr public relay result. Not Instagram/Facebook/TikTok data."),
    "peertube_public_search": dict(sp="peertube", disp="PeerTube", searched="peertube search",
                                   ct=OPEN_NETWORK, conf="source_verified_direct_platform",
                                   note="PeerTube federated video result. Not TikTok/YouTube platform data."),
    "reddit_official_api": dict(sp="reddit", disp="Reddit", searched="reddit official api",
                                ct=DIRECT_OFFICIAL, conf="source_verified_direct_platform",
                                note="Reddit official API result. Direct platform data."),
    "reddit_licensed_archive": dict(sp="reddit", disp="Licensed Provider", searched="reddit licensed archive",
                                    ct=DIRECT_LICENSED, conf="provider_supplied_licensed_feed",
                                    note="Reddit data supplied by a licensed provider."),
    "youtube_official_api": dict(sp="youtube", disp="YouTube", searched="youtube data api v3",
                                 ct=DIRECT_OFFICIAL, conf="source_verified_direct_platform",
                                 note="YouTube Data API v3 video result. Direct platform data (recency-oriented)."),
    "wikipedia_public_api": dict(sp="wikipedia", disp="Wikipedia", searched="mediawiki revisions api",
                                 ct=DIRECT_OFFICIAL, conf="source_verified_direct_platform",
                                 note="Official MediaWiki API revision metadata. Read-only monitoring; "
                                      "Pulse never edits Wikipedia articles."),
    "x_recent_search": dict(sp="x", disp="X / Twitter", searched="x recent search",
                            ct=DIRECT_OFFICIAL, conf="source_verified_direct_platform",
                            note="X recent-search result. Direct platform data (last ~7 days)."),
    "x_full_archive": dict(sp="x", disp="X / Twitter", searched="x full-archive search",
                           ct=DIRECT_OFFICIAL, conf="source_verified_direct_platform",
                           note="X full-archive result. Direct platform data."),
    "meta_ad_library": dict(sp="meta_ad_library", disp="Meta Ad Library", searched="meta ad library",
                            ct=DIRECT_TRANSPARENCY, conf="source_verified_direct_platform",
                            note="Meta Ad Library ad result. Ads only. Not organic public posts."),
    "facebook_ad_library": dict(sp="meta_ad_library", disp="Meta Ad Library", searched="meta/facebook ad library",
                                ct=DIRECT_TRANSPARENCY, conf="source_verified_direct_platform",
                                note="Meta/Facebook Ad Library ad result. Ads only. Not organic public posts."),
    "meta_graph_owned_account": dict(sp="meta_graph", disp="Meta", searched="meta graph owned account",
                                     ct=DIRECT_CONNECTED, conf="source_verified_direct_platform",
                                     note="Connected owned-account data. Not broad public listening."),
    "instagram_graph_owned_account": dict(sp="instagram_graph", disp="Instagram", searched="instagram graph owned account",
                                          ct=DIRECT_CONNECTED, conf="source_verified_direct_platform",
                                          note="Instagram connected owned-account data. Not public listening."),
    "instagram_hashtag_graph_limited": dict(sp="instagram_graph", disp="Instagram", searched="instagram hashtag (graph, limited)",
                                            ct=DIRECT_CONNECTED, conf="source_verified_direct_platform",
                                            note="Instagram hashtag via limited Graph API on a connected account."),
    "instagram_content_library_research": dict(sp="instagram_research", disp="Instagram", searched="instagram content library (research)",
                                               ct=DIRECT_RESEARCH, conf="source_verified_direct_platform",
                                               note="Instagram via approved Meta Content Library research access."),
    "facebook_page_owned_account": dict(sp="facebook_graph", disp="Facebook", searched="facebook page owned account",
                                        ct=DIRECT_CONNECTED, conf="source_verified_direct_platform",
                                        note="Facebook owned-Page data. Not public listening."),
    "facebook_content_library_research": dict(sp="facebook_research", disp="Facebook", searched="facebook content library (research)",
                                              ct=DIRECT_RESEARCH, conf="source_verified_direct_platform",
                                              note="Facebook via approved Meta Content Library research access."),
    "tiktok_display_connected_account": dict(sp="tiktok_display", disp="TikTok", searched="tiktok display connected account",
                                             ct=DIRECT_CONNECTED, conf="source_verified_direct_platform",
                                             note="TikTok connected-account videos. Not public listening."),
    "tiktok_research_api": dict(sp="tiktok_research", disp="TikTok", searched="tiktok research api",
                                ct=DIRECT_RESEARCH, conf="source_verified_direct_platform",
                                note="TikTok via approved Research API. Direct platform data."),
    "tiktok_commercial_content_api": dict(sp="tiktok_commercial", disp="TikTok", searched="tiktok commercial content api",
                                          ct=DIRECT_TRANSPARENCY, conf="source_verified_direct_platform",
                                          note="TikTok Commercial Content (transparency). Not organic listening."),
    "licensed_provider_meta": dict(sp="licensed_meta", disp="Licensed Provider", searched="licensed provider (meta)",
                                   ct=DIRECT_LICENSED, conf="provider_supplied_licensed_feed",
                                   note="Meta data supplied by a licensed provider."),
    "licensed_provider_instagram": dict(sp="licensed_instagram", disp="Licensed Provider", searched="licensed provider (instagram)",
                                        ct=DIRECT_LICENSED, conf="provider_supplied_licensed_feed",
                                        note="Instagram data supplied by a licensed provider."),
    "licensed_provider_facebook": dict(sp="licensed_facebook", disp="Licensed Provider", searched="licensed provider (facebook)",
                                       ct=DIRECT_LICENSED, conf="provider_supplied_licensed_feed",
                                       note="Facebook data supplied by a licensed provider."),
    "licensed_provider_tiktok": dict(sp="licensed_tiktok", disp="Licensed Provider", searched="licensed provider (tiktok)",
                                     ct=DIRECT_LICENSED, conf="provider_supplied_licensed_feed",
                                     note="TikTok data supplied by a licensed provider."),
    # ── compliant discovery layer (open web + known-URL enrichment) ──
    "open_web_social_discovery": dict(sp="open_web_discovery", disp="News", searched="open web",
                                      ct=OPEN_WEB_REF, conf="source_verified_open_web_reference",
                                      note="Open-web discovery result referencing a platform. It is not platform-native social data."),
    "instagram_known_url_reference": dict(sp="open_web_discovery", disp="News", searched="open web",
                                          ct=OPEN_WEB_REF, conf="source_verified_open_web_reference",
                                          note="Open-web result linking to Instagram. It is not Instagram platform data."),
    "facebook_known_url_reference": dict(sp="open_web_discovery", disp="News", searched="open web",
                                         ct=OPEN_WEB_REF, conf="source_verified_open_web_reference",
                                         note="Open-web result linking to Facebook. It is not Facebook platform data."),
    "tiktok_oembed_known_url": dict(sp="tiktok_oembed", disp="TikTok URL", searched="known TikTok URL enrichment",
                                    ct=KNOWN_URL_ENRICHMENT, conf="known_url_enrichment",
                                    note="TikTok oEmbed enriches a known public video URL. It does not perform TikTok keyword search."),
}

# manual import: map a user-supplied platform label to a display bucket
_MANUAL_DISP = {"tiktok": "TikTok", "instagram": "Instagram", "facebook": "Facebook",
                "reddit": "Reddit", "x": "X / Twitter", "twitter": "X / Twitter",
                "youtube": "YouTube", "threads": "Threads", "linkedin": "LinkedIn"}


def infer_discussed_platforms(text, url=None, source_name=None):
    blob = " ".join(str(x or "") for x in (text, source_name)).lower()
    host = (url or "").lower()
    found = []
    for canon, words, domains in _DISCUSS:
        if any(d in host for d in domains) or any(re.search(w, blob) for w in words):
            found.append(canon)
    return found


def is_direct_platform_data(coverage_type):
    return coverage_type not in _NON_DIRECT


def build_coverage_label(truth, discussed):
    ct, disp = truth["ct"], truth["disp"]
    if ct in (OPEN_WEB_REF, HISTORICAL_OPEN_WEB):
        closed = [d for d in discussed if d in CLOSED_PLATFORMS] or discussed
        if closed:
            return "Open-web article mentioning " + ", ".join(closed[:3])
        return "Open-web / news result"
    if ct == KNOWN_URL_ENRICHMENT:
        return "%s via oEmbed (known URL only)" % disp
    if ct == OPEN_NETWORK:
        return "%s public result" % disp
    if ct == DIRECT_OFFICIAL:
        return "%s official API result" % disp
    if ct == DIRECT_TRANSPARENCY:
        return "%s ad result" % disp
    if ct == DIRECT_CONNECTED:
        return "%s connected-account data" % disp
    if ct == DIRECT_RESEARCH:
        return "%s Research API result" % disp
    if ct == DIRECT_LICENSED:
        return "Licensed provider result"
    if ct == MANUAL:
        return "User-provided %s import" % disp if disp != "Manual Import" else "Manual import. User-provided data"
    return "Limited source metadata"


def build_coverage_note(truth, discussed):
    if truth["ct"] in (OPEN_WEB_REF, HISTORICAL_OPEN_WEB):
        closed = [d for d in discussed if d in CLOSED_PLATFORMS]
        if closed:
            p = closed[0]
            return ("This is not %s platform data. It is an open-web/news source that references %s." % (p, p))
    return truth.get("note", "Limited source metadata.")


def normalize_mention_source(rec, ctx):
    """Attach source-truth fields to a raw mention record. ctx carries the
    source_key (a platform_access_manager mode key). A per-record source_mode
    (set by the discovery layer) takes precedence, so different results from one
    run can carry different provenance (e.g. open-web reference vs oEmbed)."""
    key = rec.get("source_mode") or ctx.get("source_key", "")
    if key in ("manual_import_csv", "manual_import_json", "manual"):
        raw = (ctx.get("manual_platform") or rec.get("platform") or "manual").lower()
        disp = _MANUAL_DISP.get(raw, "Manual Import")
        truth = dict(sp="manual_import", disp=disp, searched="manual import", ct=MANUAL,
                     conf="user_supplied_manual_import",
                     note="User-provided data only. User is responsible for lawful upload rights.")
    else:
        truth = SOURCE_TRUTH.get(key)
        if not truth:
            truth = dict(sp=(rec.get("platform") or "unknown"), disp="Limited metadata",
                         searched=key or "unknown", ct=LIMITED, conf="limited_metadata",
                         note="Limited source metadata; provenance not fully verified.")
    discussed = infer_discussed_platforms(rec.get("content"), rec.get("url"), truth["disp"])
    rec["display_platform"] = truth["disp"]
    rec["source_platform"] = truth["sp"]
    rec["searched_platform"] = truth["searched"]
    rec["discussed_platforms"] = json.dumps(discussed)
    rec["direct_platform_data"] = 1 if is_direct_platform_data(truth["ct"]) else 0
    rec["platform_coverage_type"] = truth["ct"]
    rec["coverage_label"] = build_coverage_label(truth, discussed)
    rec["coverage_note"] = build_coverage_note(truth, discussed)
    rec["access_path"] = ctx.get("access_path") or ""
    rec["source_key"] = key
    rec["source_mode"] = key
    rec["confidence_level"] = truth["conf"]
    rec["run_id"] = ctx.get("run_id")
    return rec


def coverage_summary_for_mentions(mentions):
    """Honest counts: direct vs open-web references vs manual vs licensed, plus
    platforms actually searched (direct) vs platforms only discussed."""
    direct = openweb = manual = licensed = 0
    searched = {}
    discussed = {}
    for m in mentions:
        ct = m.get("platform_coverage_type") or LIMITED
        if ct == OPEN_WEB_REF or ct == HISTORICAL_OPEN_WEB:
            openweb += 1
        elif ct == MANUAL:
            manual += 1
        elif ct == DIRECT_LICENSED:
            licensed += 1
        if m.get("direct_platform_data"):
            direct += 1
            disp = m.get("display_platform") or "?"
            searched[disp] = searched.get(disp, 0) + 1
        for d in _as_list(m.get("discussed_platforms")):
            discussed[d] = discussed.get(d, 0) + 1
    return {
        "total": len(mentions), "direct": direct, "open_web_references": openweb,
        "manual_imports": manual, "licensed": licensed,
        "platforms_searched": [{"platform": k, "count": v} for k, v in sorted(searched.items(), key=lambda x: -x[1])],
        "platforms_discussed": [{"platform": k, "count": v} for k, v in sorted(discussed.items(), key=lambda x: -x[1])],
    }


def _as_list(v):
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v:
        try:
            return json.loads(v)
        except Exception:
            return []
    return []


EXPORT_FIELDS = ["tracked_term", "display_platform", "source_platform", "searched_platform",
                 "discussed_platforms", "direct_platform_data", "platform_coverage_type", "source_mode",
                 "coverage_label", "coverage_note", "source_url", "posted_at", "author",
                 "sentiment", "engagement"]


def export_safe_mention(m):
    """Return only export-safe, source-truth fields (no internal/secret fields)."""
    return {
        "tracked_term": m.get("keyword") or m.get("tracked_term"),
        "display_platform": m.get("display_platform"), "source_platform": m.get("source_platform"),
        "searched_platform": m.get("searched_platform"),
        "discussed_platforms": ",".join(_as_list(m.get("discussed_platforms"))),
        "direct_platform_data": bool(m.get("direct_platform_data")),
        "platform_coverage_type": m.get("platform_coverage_type"),
        "source_mode": m.get("source_mode") or m.get("source_key"),
        "coverage_label": m.get("coverage_label"), "coverage_note": m.get("coverage_note"),
        "source_url": m.get("url") or m.get("source_url"), "posted_at": m.get("posted_at"),
        "author": m.get("author"), "sentiment": m.get("sentiment"), "engagement": m.get("engagement"),
    }


def validate_source_claim(m):
    """Return a list of problems if a mention makes a misleading platform claim."""
    problems = []
    ct = m.get("platform_coverage_type")
    if ct in (OPEN_WEB_REF, HISTORICAL_OPEN_WEB) and m.get("direct_platform_data"):
        problems.append("open-web reference flagged as direct platform data")
    if m.get("display_platform") in CLOSED_PLATFORMS and ct in (OPEN_WEB_REF, HISTORICAL_OPEN_WEB):
        problems.append("open-web reference bucketed as a closed platform's direct feed")
    return problems


def assert_no_misleading_platform_claims(mentions):
    bad = []
    for m in mentions:
        p = validate_source_claim(m)
        if p:
            bad.append({"id": m.get("source_id") or m.get("url"), "problems": p})
    if bad:
        raise AssertionError("Misleading platform claims detected: %s" % bad[:5])
    return True


def coverage_badge(coverage_type):
    return BADGE.get(coverage_type, "Source")
