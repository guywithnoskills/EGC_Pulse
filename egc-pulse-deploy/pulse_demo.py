#!/usr/bin/env python3
"""
EGC Pulse. Compliant social listening (zero-infra demo, pure stdlib).

Pipeline: collect (via compliant_connectors) -> enrich (sentiment) -> dedupe ->
relevance filter -> store -> REST API -> grayscale dashboard.

Real data only. No synthetic fill. Sources, statuses, and date/historical
behavior are governed by compliant_connectors.py. See COMPLIANT_CONNECTORS.md.

Usage:
  python3 pulse_demo.py serve              # API + dashboard on :8787
  python3 pulse_demo.py collect "Jovia"    # one-shot collect (current sources)
  python3 pulse_demo.py reset              # wipe the database
"""
import hashlib
import io
import csv
import json
import os
import re
import sys
import threading
import uuid
import urllib.parse
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import platform_access_manager as pam
import ai_policy as aip
import source_truth as struth
import compliant_discovery as cd

HERE = os.path.dirname(os.path.abspath(__file__))
# DB lives next to the app by default; override with PULSE_DB to point at a
# mounted persistent disk when hosting (e.g. PULSE_DB=/data/pulse_demo.db).
DB_PATH = os.environ.get("PULSE_DB") or os.path.join(HERE, "pulse_demo.db")
KEYWORDS_PATH = os.path.join(HERE, "keywords.json")
PORT = int(os.environ.get("PORT", "8787"))
DEFAULT_KEYWORDS = []


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_env():
    for path in (os.path.join(HERE, ".env"), os.path.join(HERE, "..", ".env")):
        path = os.path.abspath(path)
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        except Exception:
            pass


# ── keywords ─────────────────────────────────────────────────────────────────
def _norm_kw(item):
    if isinstance(item, str):
        return {"label": item, "match": [item], "query": item}
    label = item.get("label") or (item.get("match") or [""])[0]
    return {"label": label, "match": item.get("match") or [label], "query": item.get("query") or label}


def tracked_keywords():
    if os.path.exists(KEYWORDS_PATH):
        try:
            with open(KEYWORDS_PATH) as f:
                return [_norm_kw(x) for x in json.load(f)]
        except Exception:
            pass
    return [_norm_kw(k) for k in DEFAULT_KEYWORDS]


def save_keywords(kws):
    out = []
    for item in kws:
        k = _norm_kw(item)
        out.append(k["label"] if (k["match"] == [k["label"]] and k["query"] == k["label"]) else k)
    with open(KEYWORDS_PATH, "w") as f:
        json.dump(out, f, indent=2)


def add_keyword(term):
    term = (term or "").strip()
    kws = tracked_keywords()
    if term and not any(k["label"].lower() == term.lower() for k in kws):
        kws.append(_norm_kw(term))
        save_keywords(kws)


def remove_keyword(label):
    save_keywords([k for k in tracked_keywords() if k["label"].lower() != (label or "").lower()])
    with db() as c:
        c.execute("DELETE FROM mentions WHERE keyword = ?", (label,))


def clear_project():
    save_keywords([])
    with db() as c:
        c.execute("DELETE FROM mentions")
        c.execute("DELETE FROM manual_insights")


def add_manual_insight(platform, period, impressions, reach, engagement=0, note=""):
    plat = (platform or "instagram").strip().lower()
    if plat not in ("instagram", "facebook"):
        plat = "instagram"
    def _int(v):
        try:
            return int(float(str(v).replace(",", "").strip() or 0))
        except Exception:
            return 0
    with db() as c:
        c.execute("INSERT INTO manual_insights (platform, period, impressions, reach, engagement, note, created_at)"
                  " VALUES (?,?,?,?,?,?,?)",
                  (plat, (period or "").strip(), _int(impressions), _int(reach), _int(engagement),
                   (note or "").strip(), now_iso()))
    return True


def get_manual_insights():
    with db() as c:
        rows = c.execute("SELECT platform, period, impressions, reach, engagement, note, created_at"
                         " FROM manual_insights ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


_re_cache = {}


def _term_re(term):
    if term not in _re_cache:
        t = term.strip()
        prefix = r"(?<!\w)#" if t.startswith("#") else r"(?<!\w)"
        body = re.escape(t[1:] if t.startswith("#") else t)
        _re_cache[term] = re.compile(prefix + body + r"(?!\w)", re.I)
    return _re_cache[term]


def is_relevant(text, terms):
    return bool(text) and any(_term_re(t).search(text) for t in terms)


# ── sentiment (lexicon, pure python) ─────────────────────────────────────────
POS = {"love": 2, "loved": 2, "great": 2, "amazing": 3, "awesome": 3, "excellent": 3, "best": 2,
       "fantastic": 3, "incredible": 3, "happy": 2, "excited": 2, "win": 2, "good": 1, "nice": 1,
       "solid": 1, "impressive": 2, "improved": 2, "fast": 1, "smooth": 2, "recommend": 2,
       "perfect": 3, "wonderful": 3, "reliable": 2, "worth": 1, "helpful": 2, "thanks": 1, "thank": 1}
NEG = {"hate": 3, "terrible": 3, "awful": 3, "worst": 3, "bad": 2, "poor": 2, "broken": 2, "buggy": 2,
       "crash": 2, "slow": 2, "disappointed": 3, "scam": 3, "ripoff": 3, "overpriced": 2, "useless": 3,
       "garbage": 3, "fail": 2, "failed": 2, "angry": 2, "annoying": 2, "frustrated": 2, "refund": 1,
       "complaint": 2, "problem": 1, "issue": 1, "issues": 2, "fraud": 3, "fee": 1, "fees": 1, "denied": 2}
NEGATORS = {"not", "no", "never", "without", "cant", "cannot", "wont", "dont", "isnt", "arent"}
TOKEN_RE = re.compile(r"[a-z']+")


def analyze_sentiment(text):
    if not text:
        return "neutral", 0.0
    toks = TOKEN_RE.findall(text.lower())
    total = 0.0
    for i, t in enumerate(toks):
        base = POS.get(t, 0) - NEG.get(t, 0)
        if base:
            if i and toks[i - 1] in NEGATORS:
                base *= -0.8
            total += base
    score = max(-1.0, min(1.0, total / 6.0))
    return ("positive" if score >= 0.05 else "negative" if score <= -0.05 else "neutral"), round(score, 3)


# ── database ─────────────────────────────────────────────────────────────────
import sqlite3


def db():
    d = os.path.dirname(DB_PATH)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)   # support PULSE_DB pointing at a mounted disk dir
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS mentions (
              id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL,
              platform_post_id TEXT NOT NULL, keyword TEXT, author TEXT, content TEXT,
              content_hash TEXT NOT NULL, url TEXT, lang TEXT DEFAULT 'en',
              posted_at TEXT NOT NULL, posted_date TEXT, ingested_at TEXT NOT NULL,
              engagement INTEGER DEFAULT 0, sentiment TEXT, sentiment_score REAL,
              is_hidden INTEGER DEFAULT 0, UNIQUE(platform, platform_post_id, keyword))""")
        c.execute("CREATE TABLE IF NOT EXISTS suppression (platform TEXT, id_hash TEXT, PRIMARY KEY (platform, id_hash))")
        # Owner-entered account analytics (impressions/reach typed from the user's
        # own Instagram/Facebook Insights). Lawful, user-provided, labeled manual.
        c.execute("""CREATE TABLE IF NOT EXISTS manual_insights (
              id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL, period TEXT,
              impressions INTEGER DEFAULT 0, reach INTEGER DEFAULT 0, engagement INTEGER DEFAULT 0,
              note TEXT, created_at TEXT NOT NULL)""")
        cols = [r["name"] for r in c.execute("PRAGMA table_info(mentions)")]
        if "posted_date" not in cols:  # migrate older DBs
            c.execute("ALTER TABLE mentions ADD COLUMN posted_date TEXT")
            c.execute("UPDATE mentions SET posted_date = substr(posted_at,1,10) WHERE posted_date IS NULL")
        # source-truth / coverage-ledger columns (safe additive migration)
        truth_cols = {"display_platform": "TEXT", "source_platform": "TEXT", "searched_platform": "TEXT",
                      "discussed_platforms": "TEXT", "direct_platform_data": "INTEGER",
                      "platform_coverage_type": "TEXT", "coverage_label": "TEXT", "coverage_note": "TEXT",
                      "access_path": "TEXT", "source_key": "TEXT", "confidence_level": "TEXT", "run_id": "TEXT"}
        need_backfill = "platform_coverage_type" not in cols
        for col, typ in truth_cols.items():
            if col not in cols:
                c.execute("ALTER TABLE mentions ADD COLUMN %s %s" % (col, typ))
        if need_backfill:
            _backfill_source_truth(c)
        c.execute("CREATE INDEX IF NOT EXISTS idx_posted ON mentions(posted_date)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_kw ON mentions(keyword)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_cov ON mentions(platform_coverage_type)")


def _backfill_source_truth(c):
    """Conservative provenance for rows that predate the source-truth layer."""
    rules = [
        ("news", "Open Web / News", "gdelt", "open web / news", 0, "open_web_reference",
         "Open-web / news result", "Open-web/news source that references a platform. Not platform-native data.", "source_verified_open_web_reference"),
        ("hackernews", "Hacker News", "hacker_news", "hacker news", 1, "open_network_public",
         "Hacker News result", "Hacker News public API result.", "source_verified_direct_platform"),
        ("mastodon", "Mastodon", "mastodon", "mastodon public api", 1, "open_network_public",
         "Mastodon public result", "Mastodon public (fediverse) result.", "source_verified_direct_platform"),
        ("lemmy", "Lemmy", "lemmy", "lemmy public api", 1, "open_network_public",
         "Lemmy public result", "Lemmy public (fediverse) result.", "source_verified_direct_platform"),
        ("nostr", "Nostr", "nostr", "nostr relays", 1, "open_network_public",
         "Nostr public result", "Nostr relay result.", "source_verified_direct_platform"),
        ("peertube", "PeerTube", "peertube", "peertube search", 1, "open_network_public",
         "PeerTube result", "PeerTube federated video result.", "source_verified_direct_platform"),
        ("reddit", "Reddit", "reddit", "reddit official api", 1, "direct_official_api",
         "Reddit official API result", "Reddit official API result. Direct platform data.", "source_verified_direct_platform"),
        ("x", "X / Twitter", "x", "x recent search", 1, "direct_official_api",
         "X official API result", "X official API result.", "source_verified_direct_platform"),
        ("manual", "Manual Import", "manual_import", "manual import", 1, "manual_import",
         "Manual import. User-provided data", "User-provided data only. User is responsible for lawful upload rights.", "user_supplied_manual_import"),
    ]
    for plat, disp, sp, searched, direct, ct, label, note, conf in rules:
        c.execute("UPDATE mentions SET display_platform=?, source_platform=?, searched_platform=?, "
                  "direct_platform_data=?, platform_coverage_type=?, coverage_label=?, coverage_note=?, "
                  "confidence_level=?, discussed_platforms=COALESCE(discussed_platforms,'[]') "
                  "WHERE platform=? AND platform_coverage_type IS NULL",
                  (disp, sp, searched, direct, ct, label, note, conf, plat))
    c.execute("UPDATE mentions SET display_platform=COALESCE(display_platform,'Limited metadata'), "
              "source_platform=COALESCE(source_platform,platform), searched_platform=COALESCE(searched_platform,platform), "
              "direct_platform_data=COALESCE(direct_platform_data,0), platform_coverage_type=COALESCE(platform_coverage_type,'limited_metadata'), "
              "coverage_label=COALESCE(coverage_label,'Limited source metadata'), coverage_note=COALESCE(coverage_note,'Limited source metadata.'), "
              "confidence_level=COALESCE(confidence_level,'limited_metadata'), discussed_platforms=COALESCE(discussed_platforms,'[]') "
              "WHERE platform_coverage_type IS NULL")


def id_hash(platform, pid):
    return hashlib.sha256(("%s:%s" % (platform, pid)).encode()).hexdigest()


def is_suppressed(platform, pid):
    with db() as c:
        return c.execute("SELECT 1 FROM suppression WHERE platform=? AND id_hash=?",
                         (platform, id_hash(platform, pid))).fetchone() is not None


def ingest(records):
    n = 0
    now = now_iso()
    with db() as c:
        for r in records:
            if is_suppressed(r["platform"], r["platform_post_id"]):
                continue
            label, score = analyze_sentiment(r.get("content", ""))
            posted = r.get("posted_at") or now
            ch = hashlib.sha256((r["platform"] + ":" + re.sub(r"\s+", " ", (r.get("content") or "").lower())).encode()).hexdigest()
            try:
                cur = c.execute(
                    "INSERT OR IGNORE INTO mentions (platform, platform_post_id, keyword, author, content, "
                    "content_hash, url, lang, posted_at, posted_date, ingested_at, engagement, sentiment, sentiment_score, "
                    "display_platform, source_platform, searched_platform, discussed_platforms, direct_platform_data, "
                    "platform_coverage_type, coverage_label, coverage_note, access_path, source_key, confidence_level, run_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (r["platform"], str(r["platform_post_id"]), r.get("keyword"), r.get("author"),
                     r.get("content"), ch, r.get("url"), r.get("lang", "en"), posted, posted[:10],
                     now, int(r.get("engagement", 0)), label, score,
                     r.get("display_platform"), r.get("source_platform"), r.get("searched_platform"),
                     r.get("discussed_platforms") or "[]", int(r.get("direct_platform_data") or 0),
                     r.get("platform_coverage_type"), r.get("coverage_label"), r.get("coverage_note"),
                     r.get("access_path"), r.get("source_key"), r.get("confidence_level"), r.get("run_id")))
                n += cur.rowcount
            except sqlite3.Error:
                pass
    return n


# ── collection (compliant connectors + date range + chunking) ───────────────
def collect(term, start=None, end=None):
    """Fan out across live collectors via the access manager. Chunks per-source
    max date-windows (e.g. TikTok Research) and merges/dedupes."""
    sources = pam.get_live_collectors()

    def run_one(src):
        windows = pam.chunk_range(start, end, src.max_window_days)
        got = []
        for ws, we in windows:
            try:
                got += pam.collect(src.key, term, ws, we)
            except Exception:
                pass
        return src, pam.dedupe(got), windows

    records, runs = [], []
    with ThreadPoolExecutor(max_workers=10) as ex:
        for src, got, windows in (ex.map(run_one, sources) if sources else []):
            rel = [r for r in got if is_relevant((r.get("content") or "") + " " + (r.get("author") or ""), [term])]
            ctx = {"source_key": src.key, "access_path": src.access_path.value, "source_name": src.display_name}
            for r in rel:
                r["keyword"] = term
                struth.normalize_mention_source(r, ctx)
            records += rel
            runs.append(pam.build_coverage_for_run(src, start, end, windows, len(rel)))
    return {"stored": ingest(records), "fetched": len(records), "runs": runs}


def backfill(keyword, start=None, end=None, historical_mode="recent_only"):
    """Run a collection and return per-source coverage disclosure across the
    whole access ladder (so the UI can show what each platform actually searched)."""
    res = collect(keyword, start, end)
    runs_by_key = {r["source_key"]: r for r in res["runs"]}
    disclosure = []
    for s in pam.get_source_matrix():
        run = runs_by_key.get(s.key)
        disclosure.append({"source": s.key, "name": s.display_name, "platform": s.platform,
                           "status": s.status.value, "access_path": s.access_path.value,
                           "kept": run["kept"] if run else 0, "windows": run["windows"] if run else 0,
                           "chunked": run["chunked"] if run else False, "coverage": s.coverage_disclosure})
    return {"keyword": keyword, "historical_mode": historical_mode, "start": start, "end": end,
            "stored": res["stored"], "ran_at": now_iso(), "disclosure": disclosure}


def manual_import(term, text="", rows=None):
    """Ingest user-supplied data the user has lawful rights to upload (CSV or
    JSON). This is the compliant 'manual_import' access path. Never scraping."""
    items = rows
    if items is None and text:
        t = text.strip()
        if t[:1] in "[{":
            try:
                j = json.loads(t)
                items = j if isinstance(j, list) else (j.get("rows") or j.get("mentions") or [])
            except Exception:
                items = None
        if items is None:
            items = list(csv.DictReader(io.StringIO(text)))
    recs = []
    for row in (items or []):
        if not isinstance(row, dict):
            continue
        content = row.get("content") or row.get("text") or row.get("snippet") or ""
        url = row.get("url") or row.get("source_url")
        pid = str(row.get("source_id") or row.get("id") or url
                  or hashlib.sha256(content.encode()).hexdigest()[:16])
        posted = str(row.get("posted_at") or row.get("timestamp") or now_iso())
        if len(posted) == 10:
            posted += "T00:00:00+00:00"
        rec = {"platform": (row.get("platform") or "manual").lower(), "platform_post_id": pid,
               "keyword": term, "author": row.get("author"), "content": content, "url": url,
               "posted_at": posted, "engagement": int(row.get("engagement") or 0)}
        struth.normalize_mention_source(rec, {"source_key": "manual_import_csv",
                                          "manual_platform": rec["platform"], "access_path": "manual_import"})
        recs.append(rec)
    return ingest(recs)


# ── metrics / feed / exports (all date-range aware) ──────────────────────────
# Retired open-network platforms are hidden from the product experience (feed,
# metrics, top sources, exports, coverage). Their connector code remains, but
# stored rows from them are not surfaced as active listening data.
_HIDDEN_PLATFORMS = ("mastodon", "lemmy", "nostr", "peertube", "hackernews", "news")


def _filters(keyword=None, start=None, end=None):
    clauses = ["is_hidden=0", "platform NOT IN (%s)" % ",".join("'%s'" % p for p in _HIDDEN_PLATFORMS)]
    params = []
    if keyword:
        clauses.append("keyword = ?"); params.append(keyword)
    if start:
        clauses.append("posted_date >= ?"); params.append(start)
    if end:
        clauses.append("posted_date <= ?"); params.append(end)
    return " AND ".join(clauses), params


STOPWORDS = set("the a an and or for to of in on at is are was be it this that with you your we our they "
                "them as from but not just have has had will can new about into over more most some any all "
                "what why how when who which their there here out up down vs amp via https http www com org net "
                "co will like dont been they that this with said were would people".split())


def metrics(keyword=None, start=None, end=None):
    where, wp = _filters(keyword, start, end)
    kwhere, kwp = _filters(None, start, end)
    with db() as c:
        total = c.execute("SELECT COUNT(*) n FROM mentions WHERE " + where, wp).fetchone()["n"]
        vol = c.execute("SELECT posted_date d, COUNT(*) n FROM mentions WHERE " + where
                        + " GROUP BY d ORDER BY d", wp).fetchall()
        sent = c.execute("SELECT sentiment s, COUNT(*) n FROM mentions WHERE " + where + " GROUP BY sentiment", wp).fetchall()
        plats = c.execute("SELECT COALESCE(display_platform, platform) p, COUNT(*) n FROM mentions WHERE " + where
                          + " GROUP BY p ORDER BY n DESC", wp).fetchall()
        authors = c.execute("SELECT author a, platform p, COUNT(*) n FROM mentions WHERE " + where
                            + " AND author IS NOT NULL GROUP BY author ORDER BY n DESC, SUM(engagement) DESC LIMIT 6", wp).fetchall()
        titles = c.execute("SELECT content FROM mentions WHERE " + where + " ORDER BY posted_at DESC LIMIT 600", wp).fetchall()
        kw_rows = c.execute("SELECT keyword k, COUNT(*) n FROM mentions WHERE " + kwhere
                            + " AND keyword IS NOT NULL GROUP BY keyword ORDER BY n DESC", kwp).fetchall()
        cov_rows = c.execute("SELECT display_platform, platform_coverage_type, direct_platform_data, "
                             "discussed_platforms FROM mentions WHERE " + where + " LIMIT 2000", wp).fetchall()
    sc = {r["s"]: r["n"] for r in sent}
    pos, neg, neu = sc.get("positive", 0), sc.get("negative", 0), sc.get("neutral", 0)
    denom = max(pos + neg + neu, 1)
    freq = {}
    kwlow = (keyword or "").lower()
    for r in titles:
        for tok in TOKEN_RE.findall(re.sub(r"https?://\S+|www\.\S+", " ", (r["content"] or "").lower())):
            if len(tok) > 3 and tok not in STOPWORDS and tok != kwlow:
                freq[tok] = freq.get(tok, 0) + 1
    topics = sorted(freq.items(), key=lambda kv: kv[1], reverse=True)[:8]
    return {"kpis": {"totalMentions": total, "netSentiment": round(100 * (pos - neg) / denom),
                     "platforms": len(plats), "positivePct": round(100 * pos / denom)},
            "volume": [{"t": r["d"], "value": r["n"]} for r in vol],
            "sentiment": {"positive": pos, "neutral": neu, "negative": neg},
            "platforms": [{"platform": r["p"], "value": r["n"]} for r in plats],
            "topics": [{"label": k, "count": v} for k, v in topics],
            "authors": [{"author": r["a"], "platform": r["p"], "mentions": r["n"]} for r in authors],
            "keywords": [{"keyword": r["k"], "count": r["n"]} for r in kw_rows],
            "coverage": struth.coverage_summary_for_mentions([dict(r) for r in cov_rows]),
            "tracked": [k["label"] for k in tracked_keywords()]}


def recent(limit=25, keyword=None, platform=None, sentiment=None, start=None, end=None):
    where, args = _filters(keyword, start, end)
    if platform:
        where += " AND platform = ?"; args.append(platform)
    if sentiment:
        where += " AND sentiment = ?"; args.append(sentiment)
    args.append(limit)
    with db() as c:
        return [dict(r) for r in c.execute(
            "SELECT platform, keyword, author, content, url, posted_at, sentiment, engagement, "
            "display_platform, source_platform, searched_platform, discussed_platforms, direct_platform_data, "
            "platform_coverage_type, coverage_label, coverage_note, source_key, confidence_level "
            "FROM mentions WHERE " + where + " ORDER BY posted_at DESC LIMIT ?", args).fetchall()]


def coverage_ledger(keyword=None, start=None, end=None):
    """What was actually searched vs only discussed vs gated, for the range/term."""
    m = metrics(keyword, start, end)
    gated = [{"platform": a["platform"], "status": a["status"], "note": a["limitation"]}
             for a in pam.accounts_status() if a["source_key"] != "manual" and not a["can_collect"]]
    return {"range": {"start": start, "end": end}, "term": keyword or "all terms",
            "coverage": m["coverage"], "sources_searched": m["platforms"], "gated": gated}


def export_csv(keyword=None, start=None, end=None):
    where, params = _filters(keyword, start, end)
    with db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT keyword, display_platform, source_platform, searched_platform, discussed_platforms, "
            "direct_platform_data, platform_coverage_type, source_key, coverage_label, coverage_note, url, posted_at, "
            "author, sentiment, engagement, content FROM mentions WHERE " + where
            + " ORDER BY posted_at DESC", params).fetchall()]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(struth.EXPORT_FIELDS + ["content"])
    for r in rows:
        safe = struth.export_safe_mention(r)
        w.writerow([safe.get(f) for f in struth.EXPORT_FIELDS] + [(r.get("content") or "").replace("\n", " ")])
    return buf.getvalue().encode()


def export_insights(keyword=None, start=None, end=None):
    m = metrics(keyword, start, end)
    k = m["kpis"]
    return {"generated_at": now_iso(), "keyword": keyword or "all", "start": start, "end": end,
            "summary": "%s mentions in range. Net sentiment %+d (%d%% positive)." % (
                "{:,}".format(k["totalMentions"]), k["netSentiment"], k["positivePct"]),
            "sources": [s.to_dict() for s in pam.get_source_matrix()], **m}


def report_html(keyword=None, start=None, end=None):
    m = metrics(keyword, start, end)
    k = m["kpis"]
    rows = recent(20, keyword, None, None, start, end)
    s = m["sentiment"]
    tot = max(s["positive"] + s["neutral"] + s["negative"], 1)

    def e(x):
        return (str(x) if x is not None else "").replace("&", "&amp;").replace("<", "&lt;")
    cov = m["coverage"]
    plat = "".join("<tr><td>%s</td><td style='text-align:right'>%d</td></tr>" % (e(p["platform"]), p["value"]) for p in m["platforms"]) or "<tr><td>none</td></tr>"
    feed = "".join("<li><a href='%s'>%s</a> <span style='color:#777'>. %s · %s</span></li>"
                   % (e(r["url"] or "#"), e((r["content"] or "")[:140]), e(r.get("coverage_label") or r.get("display_platform")), e(r["sentiment"])) for r in rows) or "<li>none</li>"
    searched = ", ".join("%s (%d)" % (e(d["platform"]), d["count"]) for d in cov["platforms_searched"]) or "none"
    disc = ", ".join("%s (%d)" % (e(d["platform"]), d["count"]) for d in cov["platforms_discussed"]) or "none"
    gated = [a for a in pam.accounts_status() if a["source_key"] != "manual" and not a["can_collect"]]
    gated_html = "".join("<li>%s. %s</li>" % (e(a["platform"]), e(a["status"])) for a in gated) or "<li>none</li>"
    cov_html = ("<h3>Coverage &amp; source truth</h3><ul>"
                "<li>Direct platform data included: <b>%d</b></li>"
                "<li>Open-web references included: <b>%d</b></li>"
                "<li>Manual imports: <b>%d</b></li>"
                "<li>Licensed-provider feeds: <b>%d</b></li>"
                "<li>Platforms directly searched: %s</li>"
                "<li>Platforms discussed but not directly searched: %s</li></ul>"
                "<p class='muted'>Gated sources (not searched):</p><ul>%s</ul>"
                "<p class='muted'>Caveat: open-web references mention a platform but are not that platform's native "
                "data. Closed-platform listening requires connected accounts, approved research access, or a licensed "
                "provider.</p>") % (cov["direct"], cov["open_web_references"], cov["manual_imports"], cov["licensed"],
                                    searched, disc, gated_html)
    return ("<!doctype html><meta charset='utf-8'><title>EGC Pulse Report</title>"
            "<style>@import url('https://fonts.googleapis.com/css2?family=Lato:wght@400;700&family=Noto+Serif:wght@400;600&display=swap');"
            "body{font-family:'Lato',system-ui,Arial,sans-serif;color:#111;max-width:820px;margin:32px auto;padding:0 20px}"
            "h1,h3{font-family:'Noto Serif',Georgia,serif;font-weight:600}.kpi{display:inline-block;border:1px solid #ddd;border-radius:8px;padding:8px 14px;margin:4px 8px 4px 0}"
            ".kpi b{font-size:22px;display:block}td{padding:3px 12px;border-bottom:1px solid #eee}.muted{color:#777}ul{line-height:1.6}</style>"
            "<h1>EGC Pulse. Internal Listening Report</h1><p class='muted'>Tracked term: <b>%s</b> · %s → %s · Generated %s</p>"
            "<div><span class='kpi'><b>%s</b>mentions</span><span class='kpi'><b>%+d</b>net sentiment</span>"
            "<span class='kpi'><b>%d%%</b>positive</span><span class='kpi'><b>%d</b>sources</span></div>"
            "<h3>Sentiment</h3><p>Positive %d%% · Neutral %d%% · Negative %d%%</p>"
            "%s"
            "<h3>Sources searched</h3><table>%s</table><h3>Top mentions</h3><ol>%s</ol>"
            "<p class='muted'>Internal use only. Not for resale or redistribution. Source access depends on "
            "configured APIs, connected accounts, licensed providers, approved research access, or lawful manual "
            "import. Generated by EGC Pulse · Print → Save as PDF.</p>") % (
        e(keyword or "all keywords"), e(start or "earliest"), e(end or "now"), now_iso(),
        "{:,}".format(k["totalMentions"]), k["netSentiment"], k["positivePct"], k["platforms"],
        round(100 * s["positive"] / tot), round(100 * s["neutral"] / tot), round(100 * s["negative"] / tot),
        cov_html, plat, feed)


def internal_use():
    return os.getenv("INTERNAL_USE_ONLY", "true").strip().lower() in {"1", "true", "yes", "on"}


# ── async collection jobs (UI never freezes; progress + cancel) ──────────────
_jobs = {}
_jobs_lock = threading.Lock()

SKIP_MSG = {
    "reddit": "Skipped Reddit. API key not configured.",
    "x": "Skipped X. API key not configured.",
    "meta": "Skipped Meta. Connected account, Ad Library token, or approved research access required.",
    "instagram": "Skipped Instagram. Connected account or licensed provider required.",
    "facebook": "Skipped Facebook. Connected account, approved research access, or licensed provider required.",
    "tiktok": "Skipped TikTok. Research approval, connected account, commercial content access, or licensed provider required.",
    "licensed": "Skipped Licensed provider. LICENSED_PROVIDER_API_KEY/URL not configured.",
}


def _skipped_sources():
    out = []
    for a in pam.accounts_status():
        if a["source_key"] == "manual" or a["can_collect"]:
            continue
        out.append({"source": a["platform"], "reason": SKIP_MSG.get(a["source_key"], a["status"])})
    return out


def _clamp_recent(start, end, days=30):
    end = (end or now_iso()[:10])[:10]
    try:
        floor = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")
        return floor if (not start or start < floor) else start
    except Exception:
        return start


def job_public(jid):
    with _jobs_lock:
        j = _jobs.get(jid)
        return {k: v for k, v in j.items() if not k.startswith("_")} if j else None


def cancel_job(jid):
    with _jobs_lock:
        j = _jobs.get(jid)
    if not j or j["status"] in ("complete", "failed", "cancelled"):
        return False
    j["_cancel"].set()
    return True


def start_job(terms, start, end, mode):
    jid = uuid.uuid4().hex[:12]
    j = {"job_id": jid, "status": "queued", "progress_pct": 0, "current_source": None,
         "current_chunk": 0, "total_chunks": 0, "stored_count": 0, "skipped_sources": [],
         "errors": [], "coverage": [], "terms": terms, "start": start, "end": end, "mode": mode,
         "started_at": now_iso(), "_cancel": threading.Event()}
    with _jobs_lock:
        _jobs[jid] = j
    threading.Thread(target=run_job, args=(jid,), daemon=True).start()
    return jid


def run_job(jid):
    with _jobs_lock:
        j = _jobs.get(jid)
    if not j:
        return
    try:
        j["status"] = "running"
        start = _clamp_recent(j["start"], j["end"], 30) if j["mode"] == "fast" else j["start"]
        end = j["end"]
        live = pam.get_live_collectors()
        j["skipped_sources"] = _skipped_sources()
        units = [(t, s) for t in j["terms"] for s in live]
        total = len(units) or 1
        done = 0
        rate = False
        for term, src in units:
            if j["_cancel"].is_set():
                j["status"] = "cancelled"
                return
            j["current_source"] = "%s. %s" % (term, src.display_name)
            windows = pam.chunk_range(start, end, src.max_window_days)
            j["total_chunks"] = len(windows)
            got = []
            for ci, (ws, we) in enumerate(windows):
                if j["_cancel"].is_set():
                    j["status"] = "cancelled"
                    return
                j["current_chunk"] = ci + 1
                try:
                    got += pam.collect(src.key, term, ws, we)
                except Exception as e:
                    msg = str(e)
                    rate = rate or ("429" in msg or "rate" in msg.lower())
                    j["errors"].append({"source": src.key, "error": msg[:140]})
            got = pam.dedupe(got)
            rel = [r for r in got if is_relevant((r.get("content") or "") + " " + (r.get("author") or ""), [term])]
            ctx = {"source_key": src.key, "access_path": src.access_path.value, "run_id": jid, "source_name": src.display_name}
            for r in rel:
                r["keyword"] = term
                struth.normalize_mention_source(r, ctx)
            j["stored_count"] += ingest(rel)  # partial results land in the DB immediately
            j["coverage"].append(pam.build_coverage_for_run(src, start, end, windows, len(rel)))
            done += 1
            j["progress_pct"] = round(100 * done / total)
        j["status"] = "rate_limited" if (rate and j["stored_count"] == 0) else "complete"
    except Exception as e:
        j["status"] = "failed"
        j["errors"].append({"source": "job", "error": str(e)[:140]})
    finally:
        j["finished_at"] = now_iso()


# ── connection tests (never leak secret VALUES. Only variable names) ────────
def test_connection(key):
    g = os.environ.get
    now = now_iso()

    def done(ok, msg, missing=()):
        pam._last_checked[key] = now
        status = next((a["status"] for a in pam.accounts_status() if a["source_key"] == key), "")
        return {"source_key": key, "ok": ok, "status": status, "message": msg,
                "missing_env_vars": list(missing), "checked_at": now}

    if key == "manual":
        return done(True, "Manual import is always available for data you have lawful rights to upload.")
    if key == "reddit":
        miss = [v for v in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET") if not g(v)]
        if miss:
            return done(False, "Missing %s. Add them to demo/.env, restart the server, then test again." % " and ".join(miss), miss)
        try:
            import base64
            auth = base64.b64encode(("%s:%s" % (g("REDDIT_CLIENT_ID"), g("REDDIT_CLIENT_SECRET"))).encode()).decode()
            req = urllib.request.Request("https://www.reddit.com/api/v1/access_token", data=b"grant_type=client_credentials",
                                         headers={"Authorization": "Basic " + auth, "User-Agent": g("REDDIT_USER_AGENT") or "egc-pulse/0.2",
                                                  "Content-Type": "application/x-www-form-urlencoded"})
            tok = json.loads(urllib.request.urlopen(req, timeout=10).read()).get("access_token")
            return done(bool(tok), "Credentials valid. Reddit API reachable." if tok
                        else "Credentials found, but the platform rejected the request. Check app type, permissions, or API tier.")
        except Exception:
            return done(False, "Credentials found, but the platform rejected the request. Check app type, token validity, or API tier.")
    if key == "x":
        if not g("X_BEARER_TOKEN"):
            return done(False, "Missing X_BEARER_TOKEN. Add it to demo/.env, restart the server, then test again.", ["X_BEARER_TOKEN"])
        try:
            req = urllib.request.Request("https://api.x.com/2/tweets/search/recent?query=egc%20-is%3Aretweet&max_results=10",
                                         headers={"Authorization": "Bearer " + g("X_BEARER_TOKEN")})
            urllib.request.urlopen(req, timeout=10)
            return done(True, "Credentials valid. X API reachable.")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                return done(True, "Credentials valid (rate-limited right now. HTTP 429).")
            return done(False, "Credentials found, but the platform rejected the request (HTTP %d). Check token validity or API tier." % e.code)
        except Exception:
            return done(False, "Could not reach the X API. Check the token and network.")
    if key in ("meta", "instagram", "facebook"):
        miss = [v for v in ("META_APP_ID", "META_APP_SECRET", "META_ACCESS_TOKEN") if not g(v)]
        if not g("META_ACCESS_TOKEN"):
            return done(False, "Missing %s. Add them to demo/.env, restart the server, then test again." % " and ".join(miss or ["META_ACCESS_TOKEN"]), miss)
        try:
            req = urllib.request.Request("https://graph.facebook.com/v19.0/me?fields=id&access_token=" + urllib.parse.quote(g("META_ACCESS_TOKEN")))
            urllib.request.urlopen(req, timeout=10)
            return done(True, "Meta token accepted by the Graph API. Owned-account / Ad Library scope depends on app review.")
        except urllib.error.HTTPError as e:
            return done(False, "Token found, but Meta rejected the request (HTTP %d). Check token validity, app review, or permissions." % e.code)
        except Exception:
            return done(False, "Could not reach the Meta Graph API. Check the token and network.")
    if key == "licensed":
        miss = [v for v in ("LICENSED_PROVIDER_API_KEY", "LICENSED_PROVIDER_URL") if not g(v)]
        if miss:
            return done(False, "Missing %s. Add them to demo/.env, restart the server, then test again." % " and ".join(miss), miss)
        try:
            req = urllib.request.Request(g("LICENSED_PROVIDER_URL"), headers={"Authorization": "Bearer " + g("LICENSED_PROVIDER_API_KEY")})
            urllib.request.urlopen(req, timeout=10)
            return done(True, "Licensed provider endpoint reachable.")
        except Exception:
            return done(False, "Credentials found, but the provider endpoint did not respond as expected. Check the URL and key.")
    if key == "tiktok":
        miss = [v for v in ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET") if not g(v)]
        if miss:
            return done(False, "Missing %s. Add them to demo/.env, restart the server, then test again." % " and ".join(miss), miss)
        return done(False, "Credentials found. Live verification needs approved Research/Display/Commercial access. Enable the matching access flag.")
    if key == "open_web":
        miss = [v for v in ("SEARCH_PROVIDER", "SEARCH_API_KEY") if not g(v)]
        if miss:
            return done(False, "Open web social discovery requires a search provider API key. Add %s (and SEARCH_API_ENDPOINT) to your environment, then test again."
                        % " and ".join(miss), miss)
        endpoint = g("SEARCH_API_ENDPOINT")
        if not endpoint:
            return done(True, "Search provider key found. Set SEARCH_API_ENDPOINT for your provider to complete setup.")
        try:
            urllib.request.urlopen(urllib.request.Request(endpoint, headers={"User-Agent": "egc-pulse/0.2"}), timeout=8)
            return done(True, "Search provider endpoint reachable. Open web discovery is configured.")
        except Exception:
            return done(True, "Search provider key and endpoint found. The endpoint did not answer a bare request, which is normal for query-only APIs.")
    return done(False, "Unknown source.")


def _coverage_from_data(mentions):
    from collections import Counter
    by_plat = Counter(m.get("platform") for m in mentions)
    return [{"display_name": s.display_name, "platform": s.platform, "status": s.status.value,
             "kept": by_plat.get(s.platform, 0)} for s in pam.get_live_collectors()]


def app_mode():
    return os.getenv("APP_MODE") or ("production" if os.getenv("RENDER") else "local")


def public_config():
    """Safe, non-secret config the deployed frontend may read. NEVER contains
    secret VALUES. Only the API base URL (for split hosting), the app mode,
    feature flags, and per-platform source statuses (already public elsewhere).
    The frontend defaults to same-origin relative paths; api_base is only set
    when PUBLIC_API_BASE_URL is configured (e.g. a separate static frontend)."""
    return {
        "api_base": (os.getenv("PUBLIC_API_BASE_URL", "") or "").rstrip("/"),
        "app_mode": app_mode(),
        "internal_use": internal_use(),
        "ai_enabled": aip.enabled(),
        "sources": pam.platform_summary(),
        "live_sources": [s.key for s in pam.get_live_collectors()],
        "discovery": cd.discovery_status(),
    }


# ── HTTP ─────────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, status=200, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _download(self, body, ctype, filename):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % filename)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        one = lambda k: qs.get(k, [None])[0]
        st, en = one("start"), one("end")
        if u.path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
                    return self._send(f.read(), ctype="text/html; charset=utf-8")
            except FileNotFoundError:
                return self._send(b"dashboard.html missing", 404, "text/plain")
        if u.path == "/api/health":
            return self._send({"status": "ok", "live": [s.key for s in pam.get_live_collectors()],
                               "tracked": [k["label"] for k in tracked_keywords()]})
        if u.path == "/api/config":
            return self._send(public_config())
        if u.path == "/api/sources":
            return self._send({"sources": [s.to_dict() for s in pam.get_source_matrix()],
                               "platforms": pam.platform_summary(),
                               "live": [s.key for s in pam.get_live_collectors()],
                               "historical_modes": pam.HISTORICAL_MODES})
        if u.path == "/api/accounts/status":
            return self._send({"accounts": pam.accounts_status(), "env_path": "demo/.env",
                               "internal_use": internal_use(), "ai_enabled": aip.enabled(),
                               "app_mode": app_mode(),
                               "env_targets": {
                                   "local": "demo/.env (then restart the server)",
                                   "production": "your host's environment variables. E.g. Render → Environment (then redeploy)"},
                               "deploy_note": ("Credentials are server-side environment variables and never touch the "
                                               "browser. Set them locally in demo/.env, or in production in your hosting "
                                               "provider's environment settings.")})
        if u.path.startswith("/api/jobs/"):
            jid = u.path[len("/api/jobs/"):].strip("/")
            j = job_public(jid)
            return self._send(j or {"error": "job not found"}, 200 if j else 404)
        if u.path == "/api/metrics":
            return self._send(metrics(one("keyword"), st, en))
        if u.path == "/api/coverage":
            return self._send(coverage_ledger(one("keyword"), st, en))
        if u.path == "/api/insights/account":
            data = pam.account_insights(st, en)
            data["manual"] = get_manual_insights()
            return self._send(data)
        if u.path == "/api/mentions":
            return self._send(recent(int(one("limit") or 25), one("keyword"), one("platform"),
                                     one("sentiment"), st, en))
        if u.path == "/api/export/mentions.csv":
            return self._download(export_csv(one("keyword"), st, en), "text/csv", "pulse_mentions.csv")
        if u.path == "/api/export/insights.json":
            return self._download(json.dumps(export_insights(one("keyword"), st, en), indent=2).encode(),
                                  "application/json", "pulse_insights.json")
        if u.path == "/api/report":
            return self._send(report_html(one("keyword"), st, en).encode(), ctype="text/html; charset=utf-8")
        return self._send({"error": "not found"}, 404)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        one = lambda k: qs.get(k, [None])[0]
        st, en = one("start"), one("end")
        if u.path == "/api/collect":
            terms = [one("q")] if one("q") else [k["label"] for k in tracked_keywords()]
            terms = [t for t in terms if t]
            if not terms:
                return self._send({"error": "Add a tracked term first."}, 400)
            jid = start_job(terms, st, en, one("mode") or "fast")
            return self._send({"job_id": jid, "status": "queued"})
        if u.path.startswith("/api/jobs/") and u.path.endswith("/cancel"):
            jid = u.path[len("/api/jobs/"):-len("/cancel")].strip("/")
            ok = cancel_job(jid)
            return self._send({"job_id": jid, "cancelled": ok, "status": (job_public(jid) or {}).get("status")})
        if u.path == "/api/accounts/test":
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8", "ignore")) if length else {}
            except Exception:
                payload = {}
            return self._send(test_connection(payload.get("source_key") or one("source_key") or ""))
        if u.path == "/api/ai/insights":
            if not aip.enabled():
                return self._send({"error": "AI insights are disabled."}, 403)
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8", "ignore")) if length else {}
            except Exception:
                payload = {}
            terms = payload.get("tracked_terms") or []
            term = terms[0] if terms else None
            plats = payload.get("platforms") or []
            sd, ed = payload.get("start_date"), payload.get("end_date")
            mentions = recent(200, term, plats[0] if plats else None, None, sd, ed)
            if not mentions:
                return self._send({"summary": "No stored mentions in this range. Nothing to summarize.",
                                   "key_themes": [], "sentiment_caveat": "",
                                   "coverage_note": aip._coverage_note(struth.coverage_summary_for_mentions([])),
                                   "source_count": 0, "source_urls": []})
            cov = struth.coverage_summary_for_mentions(mentions)
            insight = aip.deterministic_insight(term, mentions, cov, sd, ed)
            insight["method"] = "deterministic"
            return self._send(insight)
        if u.path == "/api/refresh":
            terms = [k["label"] for k in tracked_keywords()]
            total = sum(collect(t, st, en)["stored"] for t in terms)
            return self._send({"refreshed": len(terms), "stored": total, "tracked": terms})
        if u.path == "/api/backfill":
            return self._send(backfill(one("q") or "", st, en, one("mode") or "recent_only"))
        if u.path == "/api/keywords/add":
            term = one("q") or ""
            add_keyword(term)
            r = collect(term, st, en)
            return self._send({"added": term, "stored": r["stored"], "tracked": [k["label"] for k in tracked_keywords()]})
        if u.path == "/api/keywords/remove":
            remove_keyword(one("kw") or "")
            return self._send({"tracked": [k["label"] for k in tracked_keywords()]})
        if u.path == "/api/keywords/clear":
            clear_project()
            return self._send({"cleared": True})
        if u.path == "/api/import":
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length).decode("utf-8", "ignore") if length else ""
            try:
                payload = json.loads(body) if body else {}
            except Exception:
                payload = {}
            term = (payload.get("term") or one("q") or "import").strip()
            n = manual_import(term, payload.get("text", ""), payload.get("rows"))
            if n:
                add_keyword(term)
            return self._send({"imported": n, "term": term})
        if u.path == "/api/enrich/urls":
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8", "ignore")) if length else {}
            except Exception:
                payload = {}
            term = (payload.get("term") or one("q") or "url enrichment").strip()
            res = cd.enrich_known_urls(payload.get("urls"), term)
            recs = res.get("records", [])
            ctx = {"source_key": "tiktok_oembed_known_url", "access_path": "official_api", "source_name": "TikTok oEmbed"}
            for r in recs:
                r["keyword"] = term
                struth.normalize_mention_source(r, ctx)
            stored = ingest(recs)
            if stored:
                add_keyword(term)
            return self._send({"enriched": stored, "accepted": len(res.get("accepted", [])),
                               "rejected": res.get("rejected", []), "term": term})
        if u.path == "/api/insights/manual":
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8", "ignore")) if length else {}
            except Exception:
                payload = {}
            add_manual_insight(payload.get("platform"), payload.get("period"), payload.get("impressions"),
                               payload.get("reach"), payload.get("engagement"), payload.get("note"))
            return self._send({"ok": True, "manual": get_manual_insights()})
        return self._send({"error": "not found"}, 404)


def serve():
    init_db()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("EGC Pulse on http://localhost:%d  · live sources: %s"
          % (PORT, ", ".join(s.key for s in pam.get_live_collectors()) or "none"))
    srv.serve_forever()


def main():
    load_env()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "serve":
        serve()
    elif cmd == "collect":
        init_db()
        print(collect(sys.argv[2] if len(sys.argv) > 2 else "", sys.argv[3] if len(sys.argv) > 3 else None,
                      sys.argv[4] if len(sys.argv) > 4 else None))
    elif cmd == "reset":
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
        print("database reset")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
