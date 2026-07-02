"""Wikipedia monitoring + compliant update support for EGC Pulse.

MONITORING (read-only): uses the official MediaWiki Action API on
en.wikipedia.org only. A tracked term (brand keyword, exact page title,
wikipedia.org URL, or Wikidata Q-id) is resolved to pages, then revisions in
the requested date window are pulled with full metadata: revision id, previous
revision id, timestamp, editor, edit summary, working diff URL, changed section
(when the summary carries the standard /* Section */ marker), and honest,
clearly-labeled heuristic risk flags.

UPDATE SUPPORT (draft/review-first ONLY): build_update_recommendation() turns a
requested change into a neutral, cited TALK-PAGE request draft with mandatory
paid/COI disclosure per Wikipedia's paid-contribution policies. This module
contains NO code that edits Wikipedia, and WIKIPEDIA_UPDATE_MODE only supports
"draft_only": any other value is refused. Automated posting/editing would
additionally require explicit admin configuration and Wikipedia bot-policy
approval and is intentionally not implemented.

Env (all optional):
  WIKIPEDIA_MONITOR_ENABLED   "true"/"false" (default true; keyless official API)
  WIKIPEDIA_USER_AGENT        descriptive UA per Wikimedia policy (safe default)
  WIKIPEDIA_UPDATE_MODE       "draft_only" (default and only supported value)
"""
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import compliant_discovery as cd

API = "https://en.wikipedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
_DEFAULT_UA = ("EGC-Pulse/1.0 (compliant read-only Wikipedia revision monitoring; "
               "set WIKIPEDIA_USER_AGENT to add operator contact info)")


def monitor_enabled():
    return os.getenv("WIKIPEDIA_MONITOR_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def update_mode():
    return (os.getenv("WIKIPEDIA_UPDATE_MODE") or "draft_only").strip().lower()


def _ua():
    return os.getenv("WIKIPEDIA_USER_AGENT") or _DEFAULT_UA


_last_call = [0.0]


def _get(endpoint, params):
    """One official API call. Read-only (action=query/wbgetentities only).
    Wikimedia etiquette: descriptive UA, maxlag, a small inter-call throttle, and
    one polite Retry-After-respecting retry on HTTP 429."""
    import time
    params = dict(params)
    params.setdefault("format", "json")
    params.setdefault("formatversion", "2")
    params.setdefault("maxlag", "5")
    url = endpoint + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": _ua(), "Accept": "application/json"})
    for attempt in (1, 2):
        wait = _last_call[0] + 0.25 - time.time()   # >=250ms between calls
        if wait > 0:
            time.sleep(wait)
        try:
            _last_call[0] = time.time()
            with urllib.request.urlopen(req, timeout=12) as resp:
                return json.loads(resp.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 1:
                try:
                    retry = min(float(e.headers.get("Retry-After") or 2), 5.0)
                except Exception:
                    retry = 2.0
                time.sleep(retry)
                continue
            raise RuntimeError("wikipedia HTTP %d (official MediaWiki API%s)." %
                               (e.code, "; rate limited, retry later" if e.code == 429 else ""))
        except Exception as e:
            raise RuntimeError("wikipedia request failed: %s" % (str(e)[:80]))


# ── page resolution: URL | exact title | Wikidata Q-id | brand keyword ────────
def _title_from_url(term):
    m = re.match(r"https?://(?:[a-z]+\.)?wikipedia\.org/wiki/([^?#]+)", (term or "").strip(), re.I)
    return urllib.parse.unquote(m.group(1)).replace("_", " ") if m else None


def explicit_intent(term):
    """True when the tracked term IS a specific Wikipedia page (URL or Wikidata
    Q-id): the page was chosen explicitly, so records need no term-text gating."""
    t = (term or "").strip()
    return bool(re.fullmatch(r"Q\d+", t)) or bool(_title_from_url(t))


def _title_from_wikidata(qid):
    try:
        data = _get(WIKIDATA_API, {"action": "wbgetentities", "ids": qid,
                                   "props": "sitelinks", "sitefilter": "enwiki"})
        ent = (data.get("entities") or {}).get(qid) or {}
        return ((ent.get("sitelinks") or {}).get("enwiki") or {}).get("title")
    except RuntimeError:
        return None


def _page_exists(title):
    data = _get(API, {"action": "query", "titles": title, "redirects": 1})
    pages = (data.get("query") or {}).get("pages") or []
    for p in pages:
        if not p.get("missing") and p.get("title"):
            return p["title"]                    # canonical (redirect-resolved) title
    return None


def resolve_pages(term, max_pages=3):
    """Resolve a monitor input to canonical en.wikipedia page titles.
    Accepts: a wikipedia.org URL, a Wikidata Q-id, an exact page title, or a
    brand/entity keyword (official search API; only titles that pass the shared
    whole-word brand-context gate are kept)."""
    term = (term or "").strip()
    if not term:
        return []
    t = _title_from_url(term)
    if t:
        exact = _page_exists(t)
        return [exact] if exact else []
    if re.fullmatch(r"Q\d+", term):
        t = _title_from_wikidata(term)
        exact = _page_exists(t) if t else None
        return [exact] if exact else []
    # Bare keyword: an exact/redirect title match must still be about the term
    # (MediaWiki redirects can land on an unrelated title, e.g. "Jovia" -> "Iovia",
    # a wasp genus), and a same-name title can be a DIFFERENT entity (e.g.
    # "Patagonia" the region vs "Patagonia, Inc."), so the exact match is unioned
    # with gated search hits rather than trusted alone. For precise monitoring,
    # track the exact page title, URL, or Q-id.
    out = []
    exact = _page_exists(term)
    if exact and cd.result_has_brand_context({"title": exact, "url": ""}, term):
        out.append(exact)
    data = _get(API, {"action": "query", "list": "search", "srsearch": term,
                      "srlimit": 8, "srnamespace": 0})
    hits = ((data.get("query") or {}).get("search")) or []
    for h in hits:
        title = h.get("title") or ""
        if title not in out and cd.result_has_brand_context({"title": title, "url": ""}, term):
            out.append(title)
        if len(out) >= max_pages:
            break
    return out[:max_pages]


# ── revision monitoring (date-scoped, official API) ──────────────────────────
_NEG_RE = re.compile(r"lawsuit|scandal|controvers|fraud|arrest|bankrupt|lay ?offs?|recall|"
                     r"fine[sd]?\b|penalt|investigat|allegat|misconduct|breach", re.I)
_CITE_RE = re.compile(r"\bref\b|\brefs\b|citation|cite\b|source[sd]?\b", re.I)
_VANDAL_TAGS = {"mw-reverted", "mw-rollback", "mw-undo", "mw-manual-revert", "mw-blanking", "mw-replace"}
_COI_SECTIONS = re.compile(r"controvers|criticism|legal|lawsuit|reception|awards", re.I)
_SECTION_RE = re.compile(r"^/\*\s*(.+?)\s*\*/")


def _risk_flags(rev, prev_size, term):
    """Heuristic, clearly-labeled risk flags for one revision. These are review
    prompts, never asserted facts."""
    flags = []
    comment = rev.get("comment") or ""
    tags = set(rev.get("tags") or [])
    size = rev.get("size") or 0
    delta = (size - prev_size) if prev_size is not None else None
    section = _section_of(comment) or ""
    if tags & _VANDAL_TAGS or re.search(r"\brvv?\b|vandal", comment, re.I):
        flags.append("vandalism-like edit")
    if delta is not None and delta <= -500:
        flags.append("major content removal")
    if delta is not None and delta < 0 and _CITE_RE.search(comment):
        flags.append("possible citation removal")
    if delta is not None and delta >= 300 and not _CITE_RE.search(comment):
        flags.append("possible unsourced addition")
    if _NEG_RE.search(comment) or _NEG_RE.search(section):
        flags.append("negative claim")
    if _COI_SECTIONS.search(section) or (term and re.search(re.escape(term), rev.get("user") or "", re.I)):
        flags.append("COI-sensitive change")
    if rev.get("anon"):
        flags.append("anonymous editor")
    return flags


def _section_of(comment):
    m = _SECTION_RE.match(comment or "")
    return m.group(1) if m else None


def fetch_revisions(title, start=None, end=None, limit=30):
    """Revisions for one page inside [start, end] (YYYY-MM-DD, inclusive), newest
    first, via prop=revisions with rvstart/rvend so the API itself enforces the
    window (no stale revisions are ever fetched)."""
    params = {"action": "query", "prop": "revisions", "titles": title, "redirects": 1,
              "rvprop": "ids|timestamp|user|comment|size|tags|flags",
              "rvlimit": max(1, min(int(limit or 30), 50)), "rvdir": "older"}
    if end:
        params["rvstart"] = end[:10] + "T23:59:59Z"    # newer bound (rvdir=older)
    if start:
        params["rvend"] = start[:10] + "T00:00:00Z"    # older bound
    data = _get(API, params)
    pages = (data.get("query") or {}).get("pages") or []
    revs = (pages[0].get("revisions") or []) if pages else []
    return revs


def _record(title, rev, prev_size, term):
    revid, parentid = rev.get("revid") or 0, rev.get("parentid") or 0
    comment = (rev.get("comment") or "").strip()
    user = rev.get("user") or "unknown"
    ts = rev.get("timestamp") or ""
    section = _section_of(comment)
    flags = _risk_flags(rev, prev_size, term)
    if parentid:
        diff_url = "https://en.wikipedia.org/w/index.php?diff=%d&oldid=%d" % (revid, parentid)
    else:
        diff_url = "https://en.wikipedia.org/w/index.php?oldid=%d" % revid   # page creation
    delta = (rev.get("size") or 0) - prev_size if prev_size is not None else None
    bits = ["Revision %d (prev %d) by %s." % (revid, parentid, user),
            ("Summary: %s." % comment) if comment else "Summary: none.",
            ("Section: %s." % section) if section else "",
            ("Size change: %+d bytes." % delta) if delta is not None else "",
            ("Risk flags: %s." % ", ".join(flags)) if flags else "Risk flags: none detected."]
    content = '%s · %s' % (title, (comment[:110] or ("edited by " + user)))
    return {"platform": "wikipedia", "platform_post_id": "%s#%d" % (title, revid),
            "author": user, "content": content,
            "description": " ".join(b for b in bits if b),
            "url": diff_url, "posted_at": ts, "engagement": 0, "reach": 0,
            "result_type": "wikipedia_revision", "hashtags": [],
            "_risk": len(flags), "_page": title}


def collect_wikipedia(term, start=None, end=None, limit=40):
    """FETCHERS-compatible collector: resolve the tracked term to pages, pull
    date-scoped revisions, and rank exact-match + high-risk + Long Island/NYC
    relevant + newest first. Read-only; returns [] when monitoring is disabled."""
    if not monitor_enabled():
        return []
    pages = resolve_pages(term)
    if not pages:
        return []
    exact = pages[0]     # first resolved title = strongest entity match (ranking boost)
    out = []
    per = max(5, limit // len(pages))
    for title in pages:
        revs = fetch_revisions(title, start, end, per)
        for i, rev in enumerate(revs):
            prev_size = revs[i + 1].get("size") if i + 1 < len(revs) else None
            out.append(_record(title, rev, prev_size, term))
    out.sort(key=lambda r: (1 if (exact and r["_page"] == exact) else 0,
                            min(r["_risk"], 2),
                            min(cd.local_score(r["content"] + " " + r["description"]), 2),
                            r["posted_at"] or ""), reverse=True)
    for r in out:
        r.pop("_risk", None); r.pop("_page", None)
    return out[:limit]


# ── compliant update support: draft/review-first, never a live edit ──────────
# Heuristic promotional-language screen (keyword-based, NOT exhaustive; human
# NPOV review is always still required before any talk-page posting).
_PROMO_RE = re.compile(r"\b(award[- ]winning|world[- ]class|industry[- ]leading|market[- ]lead(?:ing|er)|"
                       r"best[- ]in[- ]class|leading provider|premier|top[- ]rated|revolutionary|"
                       r"unparalleled|cutting[- ]edge|state[- ]of[- ]the[- ]art|number[- ]one|"
                       r"globally recognized|(?:widely )?acclaimed|celebrated|iconic|foremost|"
                       r"innovat(?:or|ive)|world[- ]renowned|renowned|prestigious|beloved|"
                       r"fastest[- ]growing|trusted name)\b|(?<!\w)#\s*1\b"
                       r"|\b(?:most|best|largest|finest|greatest)\b[^.]{0,40}\b(?:brand|company|provider|firm|retailer)\b",
                       re.I)
_WEAK_SOURCE_RE = re.compile(r"(facebook\.com|instagram\.com|tiktok\.com|x\.com|twitter\.com|linkedin\.com|"
                             r"youtube\.com|reddit\.com|medium\.com|blogspot\.|wordpress\.com|"
                             r"prnewswire\.com|businesswire\.com|prweb\.com|globenewswire\.com|"
                             r"issuewire|einpresswire|openai\.com/chat|chatgpt|claude\.ai|gemini\.google|"
                             r"wikipedia\.org|wikimedia\.org|wikidata\.org|fandom\.com)", re.I)
# Generic/legal words that carry no brand identity when matching client vs domain.
_CLIENT_STOP = {"inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co",
                "company", "group", "holdings", "financial", "credit", "union", "bank",
                "agency", "the", "and", "of", "usa", "us"}


def _source_check(url, client, client_domains=()):
    """Classify one supplied source URL. We NEVER invent citations: only URLs the
    requester supplies are used, and each must still be human-verified. Wikipedia
    itself is weak (WP:CIRCULAR); client-owned domains are primary, matched by
    distinctive brand tokens (legal suffixes stripped) so 'Patagonia, Inc.'
    correctly flags patagonia.com, plus any explicitly declared client_domains."""
    u = (url or "").strip()
    if not re.match(r"^https?://", u):
        return "invalid", "not a valid http(s) URL"
    if _WEAK_SOURCE_RE.search(u):
        return "weak", ("Wikipedia/social/user-generated, press-release, or AI-chat link "
                        "(not a Wikipedia reliable source; Wikipedia cannot cite itself)")
    host = urllib.parse.urlparse(u).netloc.lower()
    host_norm = re.sub(r"[^a-z0-9]", "", host)
    for d in client_domains:
        if re.sub(r"[^a-z0-9]", "", str(d).lower()) in host_norm:
            return "primary", "declared client-owned domain (primary source; independent coverage required)"
    tokens = [w for w in re.split(r"[^a-z0-9]+", (client or "").lower())
              if len(w) >= 4 and w not in _CLIENT_STOP]
    if any(t in host_norm for t in tokens):
        return "primary", "client-owned domain (primary source; independent coverage required)"
    return "independent", ""


def build_update_recommendation(payload):
    """Produce a Wikipedia UPDATE RECOMMENDATION object: a neutral, cited,
    disclosure-carrying TALK-PAGE request draft for human review. Never edits
    Wikipedia and never fabricates citations. Rejects weak/primary-only sourcing
    and promotional wording."""
    if update_mode() != "draft_only":
        return {"rejected": True, "errors": [
            "WIKIPEDIA_UPDATE_MODE '%s' is not supported. Only draft_only exists: automated "
            "posting/editing would require explicit admin configuration and Wikipedia "
            "bot-policy approval, and is intentionally not implemented." % update_mode()]}
    page = (payload.get("page") or "").strip()
    section = (payload.get("section") or "").strip()
    text = (payload.get("proposed_text") or "").strip()
    reason = (payload.get("reason") or "").strip()
    client = (payload.get("client") or "").strip()
    sources = [s for s in (payload.get("sources") or []) if str(s).strip()]
    errors, warnings = [], []
    if not page:
        errors.append("page is required (exact Wikipedia article title).")
    if not text:
        errors.append("proposed_text is required.")
    if not reason:
        errors.append("reason is required (why the change improves the article).")
    # Every requester-supplied field that reaches the draft is screened, not just
    # the proposed text (heuristic keyword screen; human NPOV review still required).
    for field, val in (("proposed_text", text), ("reason", reason), ("section", section)):
        m = _PROMO_RE.search(val or "")
        if m:
            errors.append("%s contains promotional language (%s). Wikipedia requires a "
                          "neutral point of view; rewrite factually." % (field, m.group(0)))
    if not sources:
        errors.append("at least one reliable, independent source URL is required. Unsourced "
                      "claims are rejected; citations are never invented.")
    client_domains = [d for d in (payload.get("client_domains") or []) if str(d).strip()]
    checked, independents = [], 0
    for s in sources:
        kind, why = _source_check(s, client, client_domains)
        checked.append({"url": s, "assessment": kind, "note": why})
        if kind == "independent":
            independents += 1
        elif kind == "invalid":
            errors.append("source rejected: %s (%s)." % (s, why))
    if sources and independents == 0 and not any(c["assessment"] == "invalid" for c in checked):
        errors.append("all supplied sources are primary/weak (client-owned, social, press-release, "
                      "or AI-generated). At least one reliable INDEPENDENT source is required.")
    canonical = None
    if page and not errors:
        try:
            canonical = _page_exists(page)
            if not canonical:
                errors.append("page '%s' was not found on en.wikipedia.org (checked via the "
                              "official API)." % page)
        except RuntimeError as e:
            warnings.append("could not verify the page exists (%s); verify manually before use." % e)
    if errors:
        return {"rejected": True, "errors": errors, "sources_checked": checked, "mode": "draft_only"}
    page = canonical or page
    disclosure = ("Disclosure: this request is made on behalf of %s as a paid/COI contribution "
                  "under Wikipedia's paid-contribution disclosure and conflict-of-interest "
                  "guidelines (WP:PAID, WP:COI). Per those guidelines, no direct article edit "
                  "will be made; this is a talk-page request for independent editor review."
                  % (client or "a client of EGC Group"))
    src_lines = "\n".join("* %s" % c["url"] for c in checked)
    draft = ("== Requested edit: %s ==\n"
             "{{request edit}}\n"
             "%s\n\n"
             "Reason: %s\n\n"
             "Sources (to be verified by reviewing editors):\n%s\n\n"
             "%s ~~~~\n" % (section or page, text, reason, src_lines, disclosure))
    return {"rejected": False, "mode": "draft_only", "path": "talk_page_request",
            "page": page, "talk_page": "Talk:" + page, "section": section or None,
            "proposed_text": text, "reason": reason, "sources_checked": checked,
            "disclosure": disclosure, "draft_wikitext": draft, "warnings": warnings,
            "note": ("Draft for HUMAN review and manual posting to the article's talk page. "
                     "EGC Pulse never edits Wikipedia articles, automatically or otherwise.")}


def status():
    """Non-secret module status for /api config surfaces."""
    return {"enabled": monitor_enabled(), "update_mode": update_mode(),
            "update_modes_supported": ["draft_only"], "auto_edit": False,
            "user_agent_configured": bool(os.getenv("WIKIPEDIA_USER_AGENT"))}


# ── validation CLI: python3 wikipedia_monitor.py "Term" [start] [end] ─────────
if __name__ == "__main__":
    import sys
    term = sys.argv[1] if len(sys.argv) > 1 else "Patagonia, Inc."
    end = sys.argv[3] if len(sys.argv) > 3 else datetime.utcnow().strftime("%Y-%m-%d")
    start = sys.argv[2] if len(sys.argv) > 2 else (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
    print("pages for %r -> %s" % (term, resolve_pages(term)))
    recs = collect_wikipedia(term, start, end)
    print("revisions in %s..%s: %d" % (start, end, len(recs)))
    for r in recs[:3]:
        print("  -", r["posted_at"], r["platform_post_id"][:60], "|", r["description"][:90])
    bad = [r for r in recs if r["posted_at"] and not (start <= r["posted_at"][:10] <= end)]
    print("out-of-range: %d (must be 0)" % len(bad))
    # offline checks for the update-recommendation guardrails
    r1 = build_update_recommendation({"page": "X", "proposed_text": "An award-winning leader",
                                      "reason": "r", "sources": ["https://news.example.com/a"]})
    assert r1["rejected"] and any("promotional" in e for e in r1["errors"])
    r2 = build_update_recommendation({"page": "X", "proposed_text": "Neutral fact.", "reason": "r",
                                      "sources": ["https://www.facebook.com/post"]})
    assert r2["rejected"]
    r3 = build_update_recommendation({"page": "X", "proposed_text": "Neutral fact.", "reason": "r",
                                      "sources": []})
    assert r3["rejected"]
    print("update-recommendation guardrails: OK (promotional, weak-source, unsourced all rejected)")
