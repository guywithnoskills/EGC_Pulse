"""
platform_access_manager.py

Compliance-first source access manager for social listening. The LEGAL ACCESS
LADDER. It decides the highest available compliant path per platform from
configured env vars, exposes honest statuses + coverage disclosures, and
dispatches collection to the real fetchers in compliant_connectors.py.

Rules (enforced by design. There is no code path that does otherwise):
- No closed-platform scraping, headless browsers, fake accounts, session
  cookies, private/unofficial APIs, or rate-limit/auth/app-review evasion.
- Prefer official APIs → connected accounts → approved research APIs →
  official transparency/ad APIs → licensed providers → manual import.
- Manual import only for data the user has lawful rights to upload.

This module is the authoritative source metadata layer. compliant_connectors.py
provides the low-level fetch functions it calls; it is not duplicated here.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from enum import Enum
import os
from typing import Any, Dict, Iterable, List, Optional, Tuple

import compliant_connectors as cc
import compliant_discovery as cd
import wikipedia_monitor as wm


class SourceStatus(str, Enum):
    LIVE = "Live"
    REQUIRES_API_KEY = "Requires API key"
    REQUIRES_CONNECTED_ACCOUNT = "Requires connected account"
    REQUIRES_APPROVED_RESEARCH_ACCESS = "Requires approved research access"
    REQUIRES_LICENSED_DATA_PROVIDER = "Requires licensed data provider"
    NOT_AVAILABLE_COMPLIANTLY = "Not available compliantly"


class AccessPath(str, Enum):
    OFFICIAL_API = "official_api"
    CONNECTED_ACCOUNT = "connected_account"
    APPROVED_RESEARCH_API = "approved_research_api"
    OFFICIAL_TRANSPARENCY_OR_ADS_API = "official_transparency_or_ads_api"
    LICENSED_PROVIDER = "licensed_provider"
    MANUAL_IMPORT = "manual_import"
    UNAVAILABLE_COMPLIANTLY = "unavailable_compliantly"


class HistoricalMode(str, Enum):
    RECENT_ONLY = "recent_only"
    OFFICIAL_ARCHIVE = "official_archive"
    LICENSED_ARCHIVE = "licensed_archive"
    CONNECTED_ACCOUNT_HISTORY = "connected_account_history"
    RESEARCH_API = "research_api"
    ADS_ARCHIVE = "ads_archive"
    COMMERCIAL_CONTENT = "commercial_content"
    MANUAL_IMPORT = "manual_import"


HISTORICAL_MODES: List[str] = [m.value for m in HistoricalMode]


@dataclass(frozen=True)
class ConnectorCapability:
    key: str
    platform: str
    display_name: str
    access_path: AccessPath
    default_status: SourceStatus
    historical_modes: Tuple[HistoricalMode, ...]
    auth_method: str
    env_required: Tuple[str, ...] = ()
    flag_env: Optional[str] = None          # extra boolean gate (e.g. research enabled)
    allowed_data_types: Tuple[str, ...] = ()
    date_range_support: str = "unknown"
    max_window_days: Optional[int] = None
    max_historical_lookback: str = "unknown"
    rate_limits: str = "See platform/API plan"
    required_permissions: Tuple[str, ...] = ()
    storage_rules: str = "Store only permitted fields; retain source IDs/URLs for compliance handling."
    deletion_compliance: str = "Honor deletion by platform + source ID/URL per provider/platform terms."
    can_display: bool = True
    can_cache: bool = True
    can_export: bool = True
    can_resell: bool = False
    coverage_template: str = ""
    notes: str = ""


@dataclass
class ResolvedSource:
    key: str
    platform: str
    display_name: str
    status: SourceStatus
    access_path: AccessPath
    historical_modes: List[str]
    configured: bool
    can_collect: bool
    coverage_disclosure: str
    date_range_support: str
    max_window_days: Optional[int]
    max_historical_lookback: str
    auth_method: str
    allowed_data_types: List[str]
    required_permissions: List[str]
    storage_rules: str
    deletion_compliance: str
    can_display: bool
    can_cache: bool
    can_export: bool
    can_resell: bool
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["access_path"] = self.access_path.value
        return data


@dataclass
class DateChunk:
    start_date: date
    end_date: date

    def to_dict(self) -> Dict[str, str]:
        return {"start_date": self.start_date.isoformat(), "end_date": self.end_date.isoformat()}


def _env_has(name: str) -> bool:
    v = os.getenv(name)
    return bool(v and v.strip())


def _all_env_present(names: Iterable[str]) -> bool:
    return all(_env_has(n) for n in names)


def _flag_on(name: Optional[str]) -> bool:
    return bool(name) and os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def parse_iso_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def chunk_date_range(start: date, end: date, max_window_days: Optional[int]) -> List[DateChunk]:
    if end < start:
        raise ValueError("end_date cannot be before start_date")
    if not max_window_days or max_window_days <= 0:
        return [DateChunk(start, end)]
    chunks: List[DateChunk] = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=max_window_days - 1), end)
        chunks.append(DateChunk(cur, chunk_end))
        cur = chunk_end + timedelta(days=1)
    return chunks


def chunk_range(start: Optional[str], end: Optional[str], max_window_days: Optional[int]) -> List[Tuple[Optional[str], Optional[str]]]:
    """String-friendly wrapper used by the collection loop."""
    if not start or not end:
        return [(start, end)]
    try:
        chunks = chunk_date_range(parse_iso_date(start), parse_iso_date(end), max_window_days)
    except ValueError:
        return [(start, end)]
    return [(c.start_date.isoformat(), c.end_date.isoformat()) for c in chunks]


def dedupe_key(platform: str, source_id: Optional[str] = None, url: Optional[str] = None) -> str:
    p = (platform or "").lower().strip()
    if source_id:
        return f"{p}:id:{source_id.strip()}"
    if url:
        return f"{p}:url:{url.strip()}"
    raise ValueError("source_id or url is required for dedupe")


def dedupe(records: List[dict]) -> List[dict]:
    seen, out = set(), []
    for r in records:
        try:
            k = dedupe_key(r.get("platform", ""), r.get("platform_post_id"), r.get("url"))
        except ValueError:
            continue
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def validate_no_illegal_collection_method(method_name: str) -> None:
    banned = ["scrape", "scraper", "headless", "selenium", "playwright", "puppeteer",
              "session_cookie", "private_api", "unofficial_api", "fake_account", "stealth"]
    if any(t in (method_name or "").lower() for t in banned):
        raise ValueError(
            f"Non-compliant collection method rejected: {method_name}. Use official APIs, "
            "connected accounts, approved research APIs, transparency/ad APIs, licensed providers, or manual import.")


# ── capability matrix: the legal access ladder, per platform ─────────────────
_C = ConnectorCapability
CAPABILITY_MATRIX: Dict[str, ConnectorCapability] = {
    # Reddit
    "reddit_official_api": _C("reddit_official_api", "reddit", "Reddit Official API", AccessPath.OFFICIAL_API,
        SourceStatus.REQUIRES_API_KEY, (HistoricalMode.RECENT_ONLY,), "OAuth app credentials",
        env_required=("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"),
        allowed_data_types=("posts", "subreddit", "author", "permalink", "timestamp", "score", "comment_count", "url"),
        date_range_support="recent (filter stored by timestamp)", max_historical_lookback="Not a full archive.",
        required_permissions=("read",),
        coverage_template="Reddit searched via official API (recency-oriented). Deep history needs a licensed/archive provider."),
    "reddit_licensed_archive": _C("reddit_licensed_archive", "reddit", "Reddit Licensed Archive", AccessPath.LICENSED_PROVIDER,
        SourceStatus.REQUIRES_LICENSED_DATA_PROVIDER, (HistoricalMode.LICENSED_ARCHIVE,), "Licensed provider API key",
        env_required=("LICENSED_PROVIDER_API_KEY", "LICENSED_PROVIDER_URL"),
        allowed_data_types=("posts", "comments", "historical_mentions", "engagement", "url"),
        date_range_support="provider-dependent historical", max_historical_lookback="Provider contract dependent.",
        coverage_template="Reddit historical data via licensed provider under configured license scope."),

    # YouTube
    "youtube_official_api": _C("youtube_official_api", "youtube", "YouTube Data API", AccessPath.OFFICIAL_API,
        SourceStatus.REQUIRES_API_KEY, (HistoricalMode.RECENT_ONLY,), "API key",
        env_required=("YOUTUBE_API_KEY",),
        allowed_data_types=("video", "title", "description", "channel", "permalink", "timestamp"),
        date_range_support="recent (publishedAfter/publishedBefore)", max_historical_lookback="Search is recency-oriented.",
        required_permissions=("public_data",),
        coverage_template="YouTube searched via official Data API v3 (video title/description match)."),

    # X / Twitter
    "x_recent_search": _C("x_recent_search", "x", "X Recent Search", AccessPath.OFFICIAL_API,
        SourceStatus.REQUIRES_API_KEY, (HistoricalMode.RECENT_ONLY,), "Bearer token",
        env_required=("X_BEARER_TOKEN",),
        allowed_data_types=("posts", "author_id", "created_at", "public_metrics", "url"),
        date_range_support="native start_time/end_time (last 7 days)", max_window_days=7,
        max_historical_lookback="Recent window only.",
        coverage_template="X searched recent data only. Full archive requires paid or enterprise access."),
    "x_full_archive": _C("x_full_archive", "x", "X Full-Archive Search", AccessPath.OFFICIAL_API,
        SourceStatus.REQUIRES_API_KEY, (HistoricalMode.OFFICIAL_ARCHIVE,), "Bearer token with full-archive access",
        env_required=("X_BEARER_TOKEN",), flag_env="X_FULL_ARCHIVE_ENABLED",
        allowed_data_types=("posts", "author_id", "created_at", "public_metrics", "url"),
        date_range_support="native full-archive date params", max_window_days=31,
        max_historical_lookback="Back to 2006 for eligible tiers.",
        coverage_template="X searched full archive for the selected date range."),

    # Meta (cross IG+FB)
    "meta_graph_owned_account": _C("meta_graph_owned_account", "meta", "Meta Graph. Owned Account", AccessPath.CONNECTED_ACCOUNT,
        SourceStatus.REQUIRES_CONNECTED_ACCOUNT, (HistoricalMode.CONNECTED_ACCOUNT_HISTORY,), "Meta Graph API user/page token",
        env_required=("META_APP_ID", "META_APP_SECRET", "META_ACCESS_TOKEN"),
        allowed_data_types=("owned_ig_business", "owned_fb_pages", "posts", "insights", "comments_if_permissioned"),
        date_range_support="connected/owned account only", max_historical_lookback="Token/permission/app-review dependent.",
        required_permissions=("instagram_basic", "pages_read_engagement", "pages_show_list", "read_insights"),
        coverage_template="Meta coverage limited to connected owned accounts only. Not broad public listening."),
    "meta_content_library_research": _C("meta_content_library_research", "meta", "Meta Content Library (Research)", AccessPath.APPROVED_RESEARCH_API,
        SourceStatus.REQUIRES_APPROVED_RESEARCH_ACCESS, (HistoricalMode.RESEARCH_API,), "Approved Meta Content Library access",
        env_required=("META_ACCESS_TOKEN",), flag_env="META_CONTENT_LIBRARY_ENABLED",
        allowed_data_types=("public_fb_content", "public_ig_content", "research_fields"),
        date_range_support="research query environment", max_historical_lookback="Per approved project + Meta terms.",
        coverage_template="Meta Content Library requires approved research access (not ordinary commercial listening)."),
    "meta_ad_library": _C("meta_ad_library", "meta", "Meta Ad Library", AccessPath.OFFICIAL_TRANSPARENCY_OR_ADS_API,
        SourceStatus.REQUIRES_API_KEY, (HistoricalMode.ADS_ARCHIVE,), "Meta access token",
        env_required=("META_ACCESS_TOKEN",),
        allowed_data_types=("ads", "page_id", "page_name", "ad_snapshot_url", "delivery_dates", "platforms"),
        date_range_support="ad delivery date min/max (ads only)", max_historical_lookback="Ad Library policy dependent.",
        coverage_template="Meta Ad Library searched ads only. Not organic public posts."),
    "licensed_provider_meta": _C("licensed_provider_meta", "meta", "Licensed Provider. Meta", AccessPath.LICENSED_PROVIDER,
        SourceStatus.REQUIRES_LICENSED_DATA_PROVIDER, (HistoricalMode.LICENSED_ARCHIVE,), "Licensed provider contract/API",
        env_required=("LICENSED_PROVIDER_API_KEY", "LICENSED_PROVIDER_URL"),
        allowed_data_types=("provider_permitted_public_data", "source_url", "source_id", "engagement"),
        date_range_support="provider contract dependent", max_historical_lookback="Provider contract dependent.",
        coverage_template="Meta public listening via licensed provider under configured license scope."),

    # Instagram
    "instagram_graph_owned_account": _C("instagram_graph_owned_account", "instagram", "Instagram Graph. Owned Account", AccessPath.CONNECTED_ACCOUNT,
        SourceStatus.REQUIRES_CONNECTED_ACCOUNT, (HistoricalMode.CONNECTED_ACCOUNT_HISTORY,), "IG Graph API Business/Creator token",
        env_required=("META_APP_ID", "META_APP_SECRET", "META_ACCESS_TOKEN"),
        allowed_data_types=("owned_media", "owned_insights", "comments_if_permissioned"),
        date_range_support="owned/authorized account only", max_historical_lookback="Permission/token dependent.",
        required_permissions=("instagram_basic", "instagram_manage_insights", "pages_show_list"),
        coverage_template="Instagram coverage limited to a connected owned account only."),
    "instagram_hashtag_graph_limited": _C("instagram_hashtag_graph_limited", "instagram", "Instagram Hashtag (Graph, limited)", AccessPath.CONNECTED_ACCOUNT,
        SourceStatus.REQUIRES_CONNECTED_ACCOUNT, (HistoricalMode.RECENT_ONLY,), "IG Graph hashtag search (permissioned)",
        env_required=("META_APP_ID", "META_APP_SECRET", "META_ACCESS_TOKEN"),
        allowed_data_types=("hashtag_recent_media",), date_range_support="recent; 30 unique hashtags / 7 days",
        max_historical_lookback="Recent only.", required_permissions=("instagram_basic", "instagram_manage_insights"),
        coverage_template="Instagram hashtag search is limited (30 hashtags / 7 days) and needs a connected account + app review."),
    "instagram_content_library_research": _C("instagram_content_library_research", "instagram", "Instagram Content Library (Research)", AccessPath.APPROVED_RESEARCH_API,
        SourceStatus.REQUIRES_APPROVED_RESEARCH_ACCESS, (HistoricalMode.RESEARCH_API,), "Approved Meta Content Library access",
        env_required=("META_ACCESS_TOKEN",), flag_env="META_CONTENT_LIBRARY_ENABLED",
        allowed_data_types=("approved_research_public_ig_content",), date_range_support="research query environment",
        max_historical_lookback="Per approved project + Meta terms.",
        coverage_template="Instagram public research data requires Meta Content Library approval."),
    "licensed_provider_instagram": _C("licensed_provider_instagram", "instagram", "Licensed Provider. Instagram", AccessPath.LICENSED_PROVIDER,
        SourceStatus.REQUIRES_LICENSED_DATA_PROVIDER, (HistoricalMode.LICENSED_ARCHIVE,), "Licensed provider contract/API",
        env_required=("LICENSED_PROVIDER_API_KEY", "LICENSED_PROVIDER_URL"),
        allowed_data_types=("provider_permitted_public_ig_data", "source_url", "source_id", "engagement"),
        date_range_support="provider contract dependent", max_historical_lookback="Provider contract dependent.",
        coverage_template="Instagram public data via licensed provider under configured license scope."),

    # Facebook
    "facebook_page_owned_account": _C("facebook_page_owned_account", "facebook", "Facebook Page. Owned Account", AccessPath.CONNECTED_ACCOUNT,
        SourceStatus.REQUIRES_CONNECTED_ACCOUNT, (HistoricalMode.CONNECTED_ACCOUNT_HISTORY,), "Meta Graph API Page token",
        env_required=("META_APP_ID", "META_APP_SECRET", "META_ACCESS_TOKEN"),
        allowed_data_types=("owned_page_posts", "page_insights", "comments_if_permissioned"),
        date_range_support="owned Page history", max_historical_lookback="Permission/token dependent.",
        required_permissions=("pages_read_engagement", "pages_show_list", "read_insights"),
        coverage_template="Facebook coverage limited to connected owned Page data only."),
    "facebook_content_library_research": _C("facebook_content_library_research", "facebook", "Facebook Content Library (Research)", AccessPath.APPROVED_RESEARCH_API,
        SourceStatus.REQUIRES_APPROVED_RESEARCH_ACCESS, (HistoricalMode.RESEARCH_API,), "Approved Meta Content Library access",
        env_required=("META_ACCESS_TOKEN",), flag_env="META_CONTENT_LIBRARY_ENABLED",
        allowed_data_types=("approved_research_public_fb_content",), date_range_support="research query environment",
        max_historical_lookback="Per approved project + Meta terms.",
        coverage_template="Facebook public research data requires Meta Content Library approval."),
    "facebook_ad_library": _C("facebook_ad_library", "facebook", "Facebook/Meta Ad Library", AccessPath.OFFICIAL_TRANSPARENCY_OR_ADS_API,
        SourceStatus.REQUIRES_API_KEY, (HistoricalMode.ADS_ARCHIVE,), "Meta access token",
        env_required=("META_ACCESS_TOKEN",),
        allowed_data_types=("ads", "page_id", "page_name", "ad_snapshot_url", "delivery_dates"),
        date_range_support="ad delivery date min/max (ads only)", max_historical_lookback="Ad Library policy dependent.",
        coverage_template="Facebook/Meta Ad Library searched ads only. Not organic public posts."),
    "licensed_provider_facebook": _C("licensed_provider_facebook", "facebook", "Licensed Provider. Facebook", AccessPath.LICENSED_PROVIDER,
        SourceStatus.REQUIRES_LICENSED_DATA_PROVIDER, (HistoricalMode.LICENSED_ARCHIVE,), "Licensed provider contract/API",
        env_required=("LICENSED_PROVIDER_API_KEY", "LICENSED_PROVIDER_URL"),
        allowed_data_types=("provider_permitted_public_fb_data", "source_url", "source_id", "engagement"),
        date_range_support="provider contract dependent", max_historical_lookback="Provider contract dependent.",
        coverage_template="Facebook public data via licensed provider under configured license scope."),

    # TikTok
    "tiktok_display_connected_account": _C("tiktok_display_connected_account", "tiktok", "TikTok Display. Connected Account", AccessPath.CONNECTED_ACCOUNT,
        SourceStatus.REQUIRES_CONNECTED_ACCOUNT, (HistoricalMode.CONNECTED_ACCOUNT_HISTORY,), "TikTok OAuth connected account",
        env_required=("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_ACCESS_TOKEN"),
        allowed_data_types=("authorized_creator_profile", "authorized_creator_videos"),
        date_range_support="authorized account videos only", max_historical_lookback="Display API/account dependent.",
        required_permissions=("user.info.basic", "video.list"),
        coverage_template="TikTok Display API coverage limited to connected account videos only."),
    "tiktok_research_api": _C("tiktok_research_api", "tiktok", "TikTok Research API", AccessPath.APPROVED_RESEARCH_API,
        SourceStatus.REQUIRES_APPROVED_RESEARCH_ACCESS, (HistoricalMode.RESEARCH_API,), "Approved TikTok Research API access",
        env_required=("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"), flag_env="TIKTOK_RESEARCH_ENABLED",
        allowed_data_types=("research_videos", "video_description", "create_time", "username", "hashtags", "engagement_counts"),
        date_range_support="research date range (chunk long ranges)", max_window_days=30,
        max_historical_lookback="Research API/project dependent.", required_permissions=("research.data.basic",),
        coverage_template="TikTok via approved Research API only; long ranges chunked into 30-day windows."),
    "tiktok_commercial_content_api": _C("tiktok_commercial_content_api", "tiktok", "TikTok Commercial Content API", AccessPath.OFFICIAL_TRANSPARENCY_OR_ADS_API,
        SourceStatus.REQUIRES_APPROVED_RESEARCH_ACCESS, (HistoricalMode.COMMERCIAL_CONTENT,), "TikTok Commercial Content API access",
        env_required=("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"), flag_env="TIKTOK_COMMERCIAL_CONTENT_ENABLED",
        allowed_data_types=("commercial_content", "brand_names", "creator", "videos", "labels", "create_date"),
        date_range_support="commercial content published date range", max_window_days=31,
        max_historical_lookback="Commercial Content API availability dependent.",
        coverage_template="TikTok Commercial Content API searched commercial content only."),
    "licensed_provider_tiktok": _C("licensed_provider_tiktok", "tiktok", "Licensed Provider. TikTok", AccessPath.LICENSED_PROVIDER,
        SourceStatus.REQUIRES_LICENSED_DATA_PROVIDER, (HistoricalMode.LICENSED_ARCHIVE,), "Licensed provider contract/API",
        env_required=("LICENSED_PROVIDER_API_KEY", "LICENSED_PROVIDER_URL"),
        allowed_data_types=("provider_permitted_public_tiktok_data", "source_url", "source_id", "engagement"),
        date_range_support="provider contract dependent", max_historical_lookback="Provider contract dependent.",
        coverage_template="TikTok public data via licensed provider under configured license scope."),

    # Manual import (always available for data the user has lawful rights to)
    "manual_import_csv": _C("manual_import_csv", "manual", "Manual CSV Import", AccessPath.MANUAL_IMPORT,
        SourceStatus.LIVE, (HistoricalMode.MANUAL_IMPORT,), "User-supplied file with lawful rights",
        allowed_data_types=("csv_mentions", "platform", "source_url", "source_id", "timestamp", "author", "content"),
        date_range_support="filtered by normalized timestamp", max_historical_lookback="Depends on uploaded data + user rights.",
        coverage_template="Manual import used only user-provided data; user is responsible for lawful upload rights."),
    "manual_import_json": _C("manual_import_json", "manual", "Manual JSON Import", AccessPath.MANUAL_IMPORT,
        SourceStatus.LIVE, (HistoricalMode.MANUAL_IMPORT,), "User-supplied file with lawful rights",
        allowed_data_types=("json_mentions", "platform", "source_url", "source_id", "timestamp", "author", "content"),
        date_range_support="filtered by normalized timestamp", max_historical_lookback="Depends on uploaded data + user rights.",
        coverage_template="Manual import used only user-provided data; user is responsible for lawful upload rights."),

    # Open / keyless live sources (genuinely compliant public APIs)
    "mastodon_public_api": _C("mastodon_public_api", "mastodon", "Mastodon Public API", AccessPath.OFFICIAL_API,
        SourceStatus.LIVE, (HistoricalMode.RECENT_ONLY,), "None (public fediverse API)",
        allowed_data_types=("public_posts", "author", "timestamp", "url"), date_range_support="recent (filter stored)",
        max_historical_lookback="Recent public timeline.", coverage_template="Mastodon public hashtag timeline (recent)."),
    "lemmy_public_api": _C("lemmy_public_api", "lemmy", "Lemmy Public API", AccessPath.OFFICIAL_API,
        SourceStatus.LIVE, (HistoricalMode.RECENT_ONLY,), "None (public API)",
        allowed_data_types=("public_posts", "author", "score", "url"), date_range_support="recent (filter stored)",
        max_historical_lookback="Recent.", coverage_template="Lemmy public post search (recent)."),
    "bluesky_public_api": _C("bluesky_public_api", "bluesky", "Bluesky Public API", AccessPath.OFFICIAL_API,
        SourceStatus.LIVE, (HistoricalMode.RECENT_ONLY,), "None (public AT Protocol AppView)",
        allowed_data_types=("public_posts", "author", "timestamp", "url"), date_range_support="recent (filter stored)",
        max_historical_lookback="Recent public posts.", coverage_template="Bluesky public post search (recent)."),
    "nostr_public_relays": _C("nostr_public_relays", "nostr", "Nostr Public Relays", AccessPath.OFFICIAL_API,
        SourceStatus.LIVE, (HistoricalMode.RECENT_ONLY,), "None (public relays)",
        allowed_data_types=("public_notes", "pubkey", "timestamp", "url"), date_range_support="recent (filter stored)",
        max_historical_lookback="Recent relay feed.", coverage_template="Nostr recent relay feed, keyword-filtered."),
    "peertube_public_search": _C("peertube_public_search", "peertube", "PeerTube Public Search", AccessPath.OFFICIAL_API,
        SourceStatus.LIVE, (HistoricalMode.RECENT_ONLY,), "None (SepiaSearch)",
        allowed_data_types=("public_videos", "channel", "views", "url"), date_range_support="recent (filter stored)",
        max_historical_lookback="Recent.", coverage_template="PeerTube federated video search (recent)."),
    "hackernews_public_api": _C("hackernews_public_api", "hackernews", "Hacker News Public API", AccessPath.OFFICIAL_API,
        SourceStatus.LIVE, (HistoricalMode.RECENT_ONLY, HistoricalMode.OFFICIAL_ARCHIVE), "None (Algolia API)",
        allowed_data_types=("stories", "author", "points", "url"), date_range_support="full text search (filter stored)",
        max_historical_lookback="Full HN archive (by relevance).", coverage_template="Hacker News public search."),
    "news_gdelt": _C("news_gdelt", "news", "News (GDELT)", AccessPath.OFFICIAL_API,
        SourceStatus.LIVE, (HistoricalMode.RECENT_ONLY,), "None (GDELT)",
        allowed_data_types=("news_articles", "domain", "lang", "url"), date_range_support="recent window",
        max_historical_lookback="Rolling recent window.", coverage_template="Global news (GDELT), recent window."),
    # Wikipedia revision monitoring: official MediaWiki API, keyless, READ-ONLY.
    # Update support is draft/talk-page recommendations only (wikipedia_monitor.py);
    # there is no code path that edits Wikipedia. Toggle: WIKIPEDIA_MONITOR_ENABLED.
    "wikipedia_public_api": _C("wikipedia_public_api", "wikipedia", "Wikipedia", AccessPath.OFFICIAL_API,
        SourceStatus.LIVE, (HistoricalMode.OFFICIAL_ARCHIVE,), "None (official MediaWiki API)",
        allowed_data_types=("revision_metadata", "page_title", "editor", "edit_summary", "diff_url", "timestamps"),
        date_range_support="revision history scoped to the range (rvstart/rvend); newest revisions first, capped per collect",
        max_historical_lookback="Any historical window via the official API (newest N revisions per page per run).",
        rate_limits="Wikimedia etiquette: low volume, maxlag, descriptive User-Agent (WIKIPEDIA_USER_AGENT).",
        coverage_template="Wikipedia page-revision monitoring via the official MediaWiki API. Read-only: "
                          "Pulse never edits Wikipedia. Update support produces neutral, cited, "
                          "disclosure-carrying talk-page request drafts for human review only."),
    # Compliant open-web discovery: finds public pages that reference a platform.
    # Gated until a search provider API is configured (SEARCH_PROVIDER + SEARCH_API_KEY).
    "open_web_social_discovery": _C("open_web_social_discovery", "open_web", "Open web social discovery", AccessPath.OFFICIAL_API,
        SourceStatus.REQUIRES_API_KEY, (HistoricalMode.RECENT_ONLY,), "Search provider API key (SEARCH_API_KEY)",
        env_required=("SEARCH_PROVIDER", "SEARCH_API_KEY"),
        allowed_data_types=("open_web_pages", "title", "url", "discussed_platform"), date_range_support="recent (filter stored)",
        max_historical_lookback="Depends on the search provider index.", can_resell=False,
        coverage_template="Finds open web pages and known URLs that reference Instagram, Facebook, or TikTok. "
                          "This is not direct platform listening. Requires a search provider API key "
                          "(SEARCH_PROVIDER, SEARCH_API_KEY). Open web mentions of a platform are not that platform's native data."),
}


# ── fetcher dispatch: maps live modes to real functions in compliant_connectors
FETCHERS = {
    "reddit_official_api": cc.collect_reddit,
    "youtube_official_api": cc.collect_youtube,
    "wikipedia_public_api": wm.collect_wikipedia,
    "x_recent_search": cc.collect_x,
    "x_full_archive": cc.collect_x,
    "mastodon_public_api": cc.collect_mastodon,
    "lemmy_public_api": cc.collect_lemmy,
    "bluesky_public_api": cc.collect_bluesky,
    "nostr_public_relays": cc.collect_nostr,
    "peertube_public_search": cc.collect_peertube,
    "hackernews_public_api": cc.collect_hn,
    "news_gdelt": cc.collect_gdelt,
    "reddit_licensed_archive": lambda term, s, e: cc._licensed("reddit", term, s, e, 40),
    "licensed_provider_meta": lambda term, s, e: cc._licensed("meta", term, s, e, 40),
    "licensed_provider_instagram": lambda term, s, e: cc._licensed("instagram", term, s, e, 40),
    "licensed_provider_facebook": lambda term, s, e: cc._licensed("facebook", term, s, e, 40),
    "licensed_provider_tiktok": lambda term, s, e: cc._licensed("tiktok", term, s, e, 40),
    "open_web_social_discovery": lambda term, s, e: cd.collect_open_web_discovery(term, s, e),
    # owned-account / research / ads / commercial modes have no public fetcher in
    # this demo. They activate with real credentials + an implementing adapter.
}

_EASE = [SourceStatus.LIVE, SourceStatus.REQUIRES_API_KEY, SourceStatus.REQUIRES_CONNECTED_ACCOUNT,
         SourceStatus.REQUIRES_APPROVED_RESEARCH_ACCESS, SourceStatus.REQUIRES_LICENSED_DATA_PROVIDER,
         SourceStatus.NOT_AVAILABLE_COMPLIANTLY]


def resolve_source(cap: ConnectorCapability) -> ResolvedSource:
    configured = (cap.default_status == SourceStatus.LIVE) or _all_env_present(cap.env_required)
    status = cap.default_status
    can_collect = False
    if cap.default_status == SourceStatus.LIVE:
        status, can_collect, configured = SourceStatus.LIVE, True, True
    elif configured and cap.flag_env and not _flag_on(cap.flag_env):
        configured = False  # creds present but the required access flag is off
        status = cap.default_status
    elif configured:
        status, can_collect = SourceStatus.LIVE, True
    return ResolvedSource(
        key=cap.key, platform=cap.platform, display_name=cap.display_name, status=status,
        access_path=cap.access_path, historical_modes=[m.value for m in cap.historical_modes],
        configured=configured, can_collect=can_collect, coverage_disclosure=cap.coverage_template,
        date_range_support=cap.date_range_support, max_window_days=cap.max_window_days,
        max_historical_lookback=cap.max_historical_lookback, auth_method=cap.auth_method,
        allowed_data_types=list(cap.allowed_data_types), required_permissions=list(cap.required_permissions),
        storage_rules=cap.storage_rules, deletion_compliance=cap.deletion_compliance,
        can_display=cap.can_display, can_cache=cap.can_cache, can_export=cap.can_export,
        can_resell=cap.can_resell, notes=cap.notes)


# Open-network sources retired from the product experience. The connector code
# stays in compliant_connectors.py, but these are no longer shown, counted, or
# collected by default. Open web social discovery replaces this group.
HIDDEN_SOURCE_KEYS = {"mastodon_public_api", "lemmy_public_api", "nostr_public_relays",
                      "peertube_public_search", "hackernews_public_api", "news_gdelt",
                      "bluesky_public_api"}
if not wm.monitor_enabled():
    # WIKIPEDIA_MONITOR_ENABLED=false removes Wikipedia from the product (cards,
    # collection, counts) exactly like the retired open-network sources.
    HIDDEN_SOURCE_KEYS = HIDDEN_SOURCE_KEYS | {"wikipedia_public_api"}


def get_source_matrix() -> List[ResolvedSource]:
    return [resolve_source(c) for c in CAPABILITY_MATRIX.values() if c.key not in HIDDEN_SOURCE_KEYS]


def get_source_by_key(key: str) -> ResolvedSource:
    if key not in CAPABILITY_MATRIX:
        raise KeyError(f"Unknown source key: {key}")
    return resolve_source(CAPABILITY_MATRIX[key])


def get_live_collectors() -> List[ResolvedSource]:
    """Sources that can actually return data now AND have an implementing fetcher."""
    return [s for s in get_source_matrix() if s.can_collect and s.key in FETCHERS]


def account_insights(start: Optional[str] = None, end: Optional[str] = None) -> Dict[str, Any]:
    """Owner-authorized account analytics (impressions / reach / engagement).
    This is separate from listening: it is NOT public mentions. It is a
    connected account's own private metrics, available only because the owner
    authorized this app. Returns a gated payload per platform until configured."""
    return {
        "instagram": cc.meta_account_insights(start, end),
        "facebook": cc.facebook_page_insights(start, end),
        "note": ("Owner-authorized account analytics only. Impressions & reach come from accounts that "
                 "connected this app via the Meta Graph API. Never third-party accounts, never scraped."),
    }


def platform_summary() -> List[Dict[str, Any]]:
    """Per-platform highest available compliant path (for compact UI badges)."""
    groups: Dict[str, List[ResolvedSource]] = {}
    for s in get_source_matrix():
        groups.setdefault(s.platform, []).append(s)
    out = []
    for platform, modes in groups.items():
        if platform == "manual":          # Manual import retired from the product experience
            continue
        best = min((m.status for m in modes), key=lambda st: _EASE.index(st))
        # LIVE only if a mode is BOTH configured to collect AND has a real listening
        # fetcher. Meta/Instagram/Facebook connected-account access is owned-account
        # ANALYTICS, not public listening, and has no listening fetcher — so even with
        # a META_ACCESS_TOKEN present it must never show as a live listening source.
        live = any(m.can_collect and m.key in FETCHERS for m in modes)
        out.append({"platform": platform, "status": best.value,
                    "live": live, "coming_soon": not live,
                    "modes": [m.key for m in modes]})
    return out


def collect(source_key: str, term: str, start: Optional[str] = None, end: Optional[str] = None) -> List[dict]:
    src = get_source_by_key(source_key)
    if not src.can_collect:
        return []
    fn = FETCHERS.get(source_key)
    if not fn:
        return []
    # Fetcher errors (e.g. a classified 429) propagate so callers can record them
    # honestly: run_job logs them into the job's errors + rate-limited status, and
    # the sync collect() path guards each call itself. Swallowing them here made a
    # rate-limited source indistinguishable from "no results in this range".
    return fn(term, start, end) or []


def build_coverage_for_run(source: ResolvedSource, requested_start: Optional[str],
                           requested_end: Optional[str], windows: List[Tuple[Optional[str], Optional[str]]],
                           kept: int) -> Dict[str, Any]:
    return {
        "source_key": source.key, "platform": source.platform, "display_name": source.display_name,
        "status": source.status.value, "access_path": source.access_path.value,
        "historical_modes": source.historical_modes, "requested_start_date": requested_start,
        "requested_end_date": requested_end, "chunked": len(windows) > 1, "windows": len(windows),
        "kept": kept, "coverage_disclosure": source.coverage_disclosure,
        "date_range_support": source.date_range_support, "max_window_days": source.max_window_days,
    }


# ── Accounts & access (per-platform setup rows for the Accounts drawer) ───────
_last_checked: Dict[str, Optional[str]] = {}
_LICENSED_OK = ("LICENSED_PROVIDER_API_KEY", "LICENSED_PROVIDER_URL")

ACCOUNTS = [
    {"key": "reddit", "name": "Reddit", "access_path": "official_api",
     "env_required": ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"),
     "env_snippet": ("REDDIT_CLIENT_ID=", "REDDIT_CLIENT_SECRET=", "REDDIT_USER_AGENT="),
     "setup": "Create a Reddit app (script type) and add REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET to demo/.env, then restart.",
     "limitation": "Official API is recency-oriented; deep history needs a licensed/archive provider.",
     "coverage": "Reddit official API (recency-oriented).", "action": "env", "testable": True},
    {"key": "youtube", "name": "YouTube", "access_path": "official_api",
     "env_required": ("YOUTUBE_API_KEY",),
     "env_snippet": ("YOUTUBE_API_KEY=",),
     "setup": "Create a free API key in Google Cloud (enable 'YouTube Data API v3'), then add YOUTUBE_API_KEY to demo/.env "
              "(local) or your host's environment (Render), and restart. No OAuth or approval needed.",
     "limitation": "Recency-oriented video search (title/description). Free quota ~100 searches/day.",
     "coverage": "YouTube official Data API v3 (recent video search).", "action": "env", "testable": True},
    {"key": "x", "name": "X / Twitter", "access_path": "official_api",
     "env_required": ("X_BEARER_TOKEN",),
     "env_snippet": ("X_BEARER_TOKEN=", "X_FULL_ARCHIVE_ENABLED=false"),
     "setup": "Add X_BEARER_TOKEN to demo/.env (X developer account), then restart.",
     "limitation": "Recent search only unless full-archive access is enabled (paid/enterprise).",
     "coverage": "X recent search (last 7 days) unless full archive enabled.", "action": "env", "testable": True},
    {"key": "meta", "name": "Meta", "access_path": "connected_account",
     "env_required": ("META_APP_ID", "META_APP_SECRET", "META_ACCESS_TOKEN"),
     "env_snippet": ("META_APP_ID=", "META_APP_SECRET=", "META_ACCESS_TOKEN=", "META_CONTENT_LIBRARY_ENABLED=false"),
     "setup": "Create a Meta app, then add META_APP_ID, META_APP_SECRET and META_ACCESS_TOKEN to demo/.env.",
     "limitation": "Owned accounts/Pages, Ad Library, or approved research access only. Not broad public listening.",
     "coverage": "Meta owned-account / Ad Library / approved research only.", "action": "meta", "testable": False},
    {"key": "instagram", "name": "Instagram", "access_path": "connected_account",
     "env_required": ("META_APP_ID", "META_APP_SECRET", "META_ACCESS_TOKEN"),
     "env_snippet": ("META_APP_ID=", "META_APP_SECRET=", "META_ACCESS_TOKEN="),
     "setup": "Connect an authorized Instagram Business/Creator account via the Meta Graph API (uses META_* values).",
     "limitation": "Not broad public Instagram listening unless a licensed provider exists. Open-web mentions of Instagram are not Instagram platform data.",
     "coverage": "Instagram owned account / limited hashtag / licensed only.", "action": "meta", "testable": False},
    {"key": "facebook", "name": "Facebook", "access_path": "connected_account",
     "env_required": ("META_APP_ID", "META_APP_SECRET", "META_ACCESS_TOKEN"),
     "env_snippet": ("META_APP_ID=", "META_APP_SECRET=", "META_ACCESS_TOKEN="),
     "setup": "Connect an owned Facebook Page via the Meta Graph API (uses META_* values).",
     "limitation": "Public Facebook listening requires approved research access or a licensed provider. Open-web mentions of Facebook are not Facebook platform data.",
     "coverage": "Facebook owned Page / Ad Library / approved research only.", "action": "meta", "testable": False},
    {"key": "tiktok", "name": "TikTok", "access_path": "connected_account",
     "env_required": ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"),
     "env_snippet": ("TIKTOK_CLIENT_KEY=", "TIKTOK_CLIENT_SECRET=", "TIKTOK_ACCESS_TOKEN=",
                     "TIKTOK_RESEARCH_ENABLED=false", "TIKTOK_COMMERCIAL_CONTENT_ENABLED=false"),
     "setup": "Add TikTok app credentials and enable the access flags you are approved for.",
     "limitation": "No public TikTok scraping. Connected account, research approval, commercial content, or licensed provider only. Open-web mentions of TikTok are not TikTok platform data.",
     "coverage": "TikTok connected account / research / commercial / licensed only.", "action": "tiktok", "testable": False},
    {"key": "licensed", "name": "Licensed provider", "access_path": "licensed_provider",
     "env_required": ("LICENSED_PROVIDER_API_KEY", "LICENSED_PROVIDER_URL"),
     "env_snippet": ("LICENSED_PROVIDER_API_KEY=", "LICENSED_PROVIDER_URL=", "LICENSED_PROVIDER_NAME=", "LICENSED_PROVIDER_PLATFORMS="),
     "setup": "Add LICENSED_PROVIDER_API_KEY and LICENSED_PROVIDER_URL to demo/.env, then restart.",
     "limitation": "Coverage depends on your provider contract.",
     "coverage": "Public listening via licensed provider under contract scope.", "action": "env", "testable": True},
    {"key": "open_web", "name": "Open web social discovery", "access_path": "official_api",
     "env_required": ("SEARCH_PROVIDER", "SEARCH_API_KEY"),
     "env_snippet": ("SEARCH_PROVIDER=brave", "SEARCH_API_KEY=  (Brave Search API key)"),
     "setup": "Open web social discovery needs a server-side search provider key. Use the Brave Search API: get a "
              "key at api-dashboard.search.brave.com (Search plan; free credit about 1,000 queries/month), then set "
              "SEARCH_PROVIDER=brave and SEARCH_API_KEY. Restart or redeploy. The key is never exposed to the browser.",
     "limitation": "Finds open web pages and known URLs that reference Instagram, Facebook, or TikTok. "
                   "This is not direct platform listening. Open web mentions of a platform are not that platform's native data.",
     "coverage": "Open web discovery via a configured search provider API.", "action": "env", "testable": True},
    {"key": "manual", "name": "Manual import", "access_path": "manual_import",
     "env_required": (), "env_snippet": (),
     "setup": "Upload CSV/JSON data you have the lawful right to use (use Import data).",
     "limitation": "User-provided data only.", "coverage": "Only user-provided imported data.",
     "action": "import", "testable": False},
]


def _account_status(key, configured):
    if key in ("reddit", "x", "youtube"):
        return SourceStatus.LIVE.value if configured else SourceStatus.REQUIRES_API_KEY.value
    if key == "meta":
        return SourceStatus.LIVE.value if configured else SourceStatus.REQUIRES_CONNECTED_ACCOUNT.value
    if key in ("instagram", "facebook"):
        return SourceStatus.LIVE.value if _all_env_present(_LICENSED_OK) else "Requires connected account or licensed provider"
    if key == "tiktok":
        return SourceStatus.LIVE.value if _all_env_present(_LICENSED_OK) else "Requires connected account, research approval, commercial content access, or licensed provider"
    if key == "licensed":
        return SourceStatus.LIVE.value if configured else SourceStatus.REQUIRES_LICENSED_DATA_PROVIDER.value
    if key == "open_web":
        return SourceStatus.LIVE.value if configured else SourceStatus.REQUIRES_API_KEY.value
    return SourceStatus.LIVE.value  # manual


def _account_can_collect(key, configured):
    if key in ("reddit", "x", "youtube", "licensed", "open_web"):
        return configured
    if key == "manual":
        return True
    # meta/instagram/facebook/tiktok owned/research/ads adapters are not
    # implemented in this demo (gated). They never silently "collect".
    return False


def accounts_status():
    out = []
    for a in ACCOUNTS:
        missing = [v for v in a["env_required"] if not _env_has(v)]
        configured = (a["key"] == "manual") or (len(missing) == 0)
        out.append({
            "platform": a["name"], "source_key": a["key"], "access_path": a["access_path"],
            "status": _account_status(a["key"], configured), "configured": configured,
            "can_collect": _account_can_collect(a["key"], configured), "missing_env_vars": missing,
            "setup_instructions": a["setup"], "limitation": a["limitation"],
            "coverage_disclosure": a["coverage"], "env_snippet": list(a["env_snippet"]),
            "action": a["action"], "testable": a["testable"], "last_checked_at": _last_checked.get(a["key"]),
        })
    return out

