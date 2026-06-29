# EGC Pulse — Social Listening Analytics Methodology

This document defines exactly how every metric in EGC Pulse is derived, the
cumulative model that rolls individual mentions up into dashboard figures, the
scoring logic behind the Visibility Score, and an honest account of the
limitations and recommended improvements.

Guiding principle: **real data only, no fabrication.** Every number traces back
to a value returned by an official, compliant API (or to an owner-authorized
account). Where a true metric is not obtainable from public data, Pulse either
omits it or labels it an **estimate** with the formula shown below — it never
invents a figure.

---

## 1. Data sources and what each actually provides

| Source | Status | Mentions | Engagement | Views / Impressions | Notes |
|---|---|---|---|---|---|
| **YouTube Data API v3** | Live (primary) | Video search match | `likeCount + commentCount` | `viewCount` (real) | Views are the strongest public reach signal Pulse has. Collected first. |
| **Open web (Brave Search)** | Live (primary) | Public page references, TikTok-first | none | none | Presence/mention signal only; no engagement or views. TikTok query runs first. |
| **TikTok oEmbed (known URL)** | Live | Known video URL enrichment | none | none | Title/author only; no public engagement or view metric. |
| **Owned accounts (Meta Graph)** | Connect to enable | n/a | post engagements | **impressions + unique reach (real)** | The only compliant source of *true* impressions and unique reach. |
| Reddit, X, Instagram, Facebook, TikTok, Licensed | Coming soon | — | — | — | Connectors exist but are gated; not counted until enabled. |

YouTube and TikTok (via the TikTok-first open-web query) are prioritized as the
primary sources: collection sorts them ahead of everything else.

Per-source field mapping (from `compliant_connectors.py`):

- **YouTube:** `engagement = likeCount + commentCount`, `reach = viewCount`.
- **Open web / News & TikTok references:** `engagement = 0`, `reach = 0` (no public metric).
- **Reddit (gated):** `engagement = score + num_comments`.
- **X (gated):** `engagement = like + retweet + reply + quote counts`.
- **Owned Meta accounts:** `impressions`, `reach`, `engagement` come directly from
  the Insights API and are the user's own private, authorized metrics.

---

## 2. Metric definitions and exact calculations

All figures are computed **within the active query scope**: the selected tracked
term (or all terms) and the start/end date range. The backend reference is
`metrics()` in `pulse_demo.py`.

### 2.1 Mentions
`totalMentions` = count of stored mention rows in scope. A "mention" is one
public item (a video, post, or page) that matched a tracked term through a
compliant search. De-duplicated on ingest by a content hash so the same item is
not counted twice.

### 2.2 Engagement
`totalEngagement` = `SUM(engagement)` across in-scope mentions, where each row's
engagement was set at collection time using the per-source mapping in section 1
(public interaction counts: likes, comments, reposts, replies, etc.).

### 2.3 Impressions
`totalImpressions` = `SUM(reach_column)` across in-scope mentions — i.e. the sum
of **measured views/displays** we actually received from a platform
(YouTube `viewCount`; owned-account impressions when a Meta account is connected).
Sources that expose no view count (open web / News, TikTok references) contribute
0 impressions, so impressions reflect only what was genuinely measured.

> Naming note: internally the column is `reach`, but it stores **measured views**,
> which is conceptually *impressions*. The UI now surfaces it as **Impressions**
> for accuracy.

### 2.4 Reach (estimated audience reached)
Public listening APIs do **not** expose unique reach (unique people reached), so
Pulse reports an explicit **estimate of the audience actually reached** — not
engagement, and never follower count. It is computed **per item** and summed, so
each mention contributes the best estimate its data supports:

```
per item:
  if measured views > 0:   reach = views * VIEW_TO_REACH          # VIEW_TO_REACH = 0.75
  elif engagement  > 0:   reach = engagement * ENGAGEMENT_TO_REACH # ENGAGEMENT_TO_REACH = 22.0  (~1 / 4.5%)
  else:                   reach = 0                                # news/open web: unknown, never fabricated
estimatedReach = round( SUM(per-item reach) )
```

- **Views → unique viewers** (`VIEW_TO_REACH = 0.75`): views are the strongest
  public exposure signal; the factor discounts repeat views to approximate unique
  people. This is the main driver, since YouTube is a primary source.
- **Engagement → reach** (`ENGAGEMENT_TO_REACH = 22.0`): for items that have
  engagement but no view count, reach is backed out from a documented ~4.5%
  engagement rate (reach ≈ engagement ÷ 0.045). This estimates audience from
  interaction without ever using raw engagement *as* reach.
- **No signal → 0:** open-web/News references with neither views nor engagement
  contribute nothing. They are never assigned a fabricated number.

Both factors are single, documented assumptions rather than hidden fudges, and the
metric is labeled **"est."** in the UI. When an **owned Meta account** is
connected, true unique reach from the Insights API is shown instead of the estimate.

### 2.5 Sentiment
Per-mention sentiment is classified at ingest by a transparent weighted lexicon
(`analyze_sentiment()`):

1. Tokenize the content to lowercase words.
2. Each token scores from a positive lexicon (`good +1` … `amazing/awesome +3`)
   minus a negative lexicon (`issue/fee +1` … `hate/scam/fraud +3`).
3. A preceding **negator** (`not`, `no`, `never`, `cant`, …) flips and dampens the
   token's weight (`× -0.8`).
4. `score = clamp(sum / 6, -1 … +1)`.
5. Label: `positive` if `score ≥ 0.05`, `negative` if `score ≤ -0.05`, else `neutral`.

Aggregate sentiment over the scope:

```
positivePct  = round(100 * positive / (positive + neutral + negative))
netSentiment = round(100 * (positive - negative) / (positive + neutral + negative))   # -100 … +100
```

### 2.6 Topics
`topics` = the most frequent meaningful tokens across in-scope content: tokens of
length > 3, excluding a stopword list and the tracked term itself, URL-stripped,
ranked by frequency (top 8).

### 2.7 Top authors / Top content
- **Top authors:** group by author, rank by mention count then summed engagement.
- **Top content:** in-scope mentions ranked by `reach` then `engagement` (the
  highest-visibility items), with their platform, engagement, reach, and link.

### 2.8 Volume and engagement-by-platform
- **Volume:** mention count grouped by day across the range (the time series). In
  the PowerPoint export this is aggregated to a readable, chronological axis:
  daily up to 16 points, then weekly, then monthly, labeled YY/MM/DD (or YY/MM).
- **Engagement by platform / top platforms:** grouped into the three product
  platforms (TikTok, YouTube, News) — the same buckets as the platform buttons.

### 2.9 Platform-button metrics (TikTok / YouTube / News)
The three platform buttons summarize each platform with mentions, reach, engagement,
estimated audience, and visibility. Mentions bucket by display classification
(`TikTok URL` -> TikTok, `youtube` -> YouTube, everything else -> News) so the
buttons always reconcile to total mentions.

YouTube uses its **measured** views/likes/comments. TikTok references and News/open-web
articles expose **no per-post public metric**, so their button-level reach and
engagement are an explicit **estimate** (flagged "est." in the UI and deck):

```
reach_est      = mentions * BASE_REACH[platform]       # TikTok 4500, News 1500 per surfaced item
engagement_est = round(reach_est * ENG_RATE[platform]) # TikTok 5.5%, News 1.5%
estimated audience = round(reach_est * 0.75)
```

These are deterministic from the collected mention volume (not arbitrary), keep the
buttons informative, and are clearly labeled as estimates. **Per-mention feed rows
and the headline KPIs remain on measured data only** — a row with no public metric
still shows "no public metric"; nothing is fabricated at the item level.

---

## 3. Visibility Score (cumulative scoring logic)

The Visibility Score is a single **0–100 composite index** that rolls volume,
amplification, engagement, and sentiment into one comparable number
(`visibility_score()` in `pulse_demo.py`).

```
lg(v)  = log10(v + 1)                          # log scale: diminishing returns

volume        = min( lg(mentions)    / 3, 1 )   #   ~1,000 mentions      -> 1.0
amplification = min( lg(impressions) / 6, 1 )   #   ~1,000,000 views     -> 1.0
engagement    = min( lg(engagement)  / 5, 1 )   #   ~100,000 interactions-> 1.0
sentiment     = (clamp(netSentiment, -100, 100) + 100) / 200   # -100..100 -> 0..1

VisibilityScore = round( 100 * ( 0.30*volume + 0.30*amplification
                               + 0.25*engagement + 0.15*sentiment ) )
```

Design rationale:

- **Log scaling** prevents one viral video from saturating the score and keeps it
  meaningful across brands of very different sizes.
- **Weights** (volume 30 / amplification 30 / engagement 25 / sentiment 15)
  balance *how much* is being said and seen against *how people feel* — presence
  and reach dominate, sentiment modulates.
- **Normalization anchors** (the `/3`, `/6`, `/5` divisors) are explicit and
  tunable; they define what "full marks" means on each axis.
- **Empty scope returns 0** (no mentions → no visibility), avoiding a misleading
  non-zero baseline from the neutral-sentiment term.

The score is **cumulative and range-scoped**: change the date range or term and
every input recomputes over exactly that scope, so two periods or two brands are
directly comparable.

---

## 4. Reporting methodology (how it rolls up)

1. **Collection** fetches public items per live source, de-dupes on content hash,
   classifies sentiment, and stores per-row engagement and views.
2. **Scope filter** applies the term + date range to every query.
3. **Aggregation** computes the section-2 metrics in a single pass over the
   in-scope rows.
4. **Composite** derives the Visibility Score from those aggregates.
5. **Coverage ledger** records what was *directly searched* vs *only discussed* vs
   *gated*, so every figure is auditable back to its source — exported alongside
   the data (`/api/export/*`, `/api/report`).

---

## 5. Limitations (stated honestly)

- **Unique reach is estimated**, not measured, for all public sources (views ×
  0.75, or engagement × 22 where views are absent). Only connected owned accounts
  yield true reach.
- **Impressions exist only where a platform reports views** (today: YouTube).
  Open-web / News and TikTok references contribute presence, not impressions.
- **Sentiment is lexicon-based** — fast, transparent, and offline, but it does not
  capture sarcasm, emoji-only sentiment, slang, or non-English text well.
- **Engagement is not normalized across platforms** — counts are summed as-is, so
  cross-platform comparisons are directional.
- **Topics are surface n-grams**, not entity/aspect extraction.
- **Coverage depends on quotas** — the Brave Search free tier allows roughly 1,000
  queries/month; exhaustion is surfaced, not silently treated as "no results."

---

## 6. Recommendations for improvement

1. **Connect owned Meta accounts** to replace estimated reach with true
   impressions and unique reach for the brand's own channels.
2. **Per-platform reach factors** instead of one global 0.75, calibrated as real
   reach/impression ratios become available.
3. **Follower-weighted reach** for text platforms (X, Reddit) once author audience
   size is captured, giving a defensible impressions estimate where views are absent.
4. **Upgrade sentiment** to a model that handles negation, emoji, and sarcasm, and
   add a confidence score and language detection.
5. **Aspect/entity topics** (products, features, complaints) beyond raw token
   frequency.
6. **Share of Voice** once competitor terms are tracked, plus period-over-period
   deltas on every KPI.
7. **Configurable score weights** so the Visibility Score can be tuned per client
   objective (awareness vs. engagement vs. sentiment).

---

*Backend references: `metrics()`, `visibility_score()`, `analyze_sentiment()`,
`REACH_FACTOR` in `pulse_demo.py`; per-source field mapping in
`compliant_connectors.py`; provenance in `source_truth.py`.*
