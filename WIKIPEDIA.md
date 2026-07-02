# Wikipedia monitoring and compliant update support

## Insights, findings, and the draft action (v11 refinements)
- **Wikipedia insights** render on each revision row: who edited, what section, size
  change, and a risk level (No risk flags / Review / High-risk) derived from
  heuristic flags (vandalism-like, major removal, citation removal, negative/
  unsourced, COI-sensitive, anonymous). Timestamps show as "Jul 2, 2026, 3:45 PM".
- **The "edit" wording is gone.** The revision chip now reads "Wikipedia change"
  (a monitored change, not an edit action). The compliant draft action is a
  clearly-labeled "Draft talk-page request" (per row) and "Prepare talk-page
  request" (on the Wikipedia source card). Both open a draft-only composer with the
  microcopy "Draft-only. Does not publish to Wikipedia." It calls
  `/api/wikipedia/recommendation`; `update_mode` stays `draft_only`, `auto_edit`
  stays false, and no code path edits Wikipedia.
- **Findings bar** (top of Mentions): total mentions, high-risk count, Wikipedia
  changes, source mix (TikTok / YouTube / News), newest item, and a recommended
  action. It is computed from the deduped, filtered result set (no fabricated or
  double-counted numbers) and updates with date/source/search filters.

## Other v11 refinements
- **Performance:** search input is debounced; the wasteful 25s idle full-refresh
  was removed (live updates during a collection are driven by job polling);
  `refresh()` coalesces rapid filter/preset/project switches so calls don't stack.
- **Default Project:** a starter workspace bootstrapped on a fresh database, now
  labeled "starter" with helper text; create your own from scratch with the +
  button and delete Default anytime (the app keeps at least one project).
- **EGC logo:** the stepped-block EGC brand mark renders in the rail and on the
  PPT title slide (crisp inline SVG / drawn shapes; replace with the official EGC
  asset if desired).
- **PowerPoint:** clearer 10-slide deck with an Executive summary (key findings +
  recommendations), a Wikipedia-watch slide (high-risk first) when revisions exist,
  the EGC mark on the title, and human-readable dates throughout.
- **Dates:** human-friendly everywhere ("Jul 2, 2026", ranges "Jun 2 to Jul 2,
  2026"); no ambiguous numeric formats.

## What monitoring does
Pulse monitors Wikipedia pages for client-relevant changes using ONLY the
official MediaWiki Action API (keyless, read-only). A tracked term can be:

* a brand/entity keyword (resolved via the official search API; only titles that
  actually contain the term are kept)
* an exact page title (redirects resolved, but never to an unrelated title)
* a full `wikipedia.org/wiki/...` URL
* a Wikidata Q-id (e.g. `Q1384`), resolved through its enwiki sitelink

For each revision inside the selected date range Pulse stores: page title, diff
URL (clickable, working), revision id, previous revision id, edit timestamp,
editor username/IP, edit summary, changed section (from the standard
`/* Section */` summary marker), and heuristic **risk flags**: vandalism-like
edit, major content removal, possible citation removal, possible unsourced
addition, negative claim, COI-sensitive change, anonymous editor. Flags are
review prompts, never asserted facts.

Wikipedia rows flow through the normal pipeline: per-project storage, the
selected date range (enforced at the API call via `rvstart`/`rvend`, so stale
revisions are never even fetched; the newest ~dozens of in-range revisions per
page are kept per run), dedup per revision id (re-running a monitor never
double-counts, including the same revision under two tracked terms), and the
standard ranking (exact page matches, high-risk changes, Long Island/NYC
relevance, newest first). Coverage is labeled honestly as direct official-API
platform data; revisions are edit events, so they carry neutral sentiment and
never feed the estimated reach/engagement numbers.

## What "updating" means here (and what it never means)
Update support = **draft/review-first recommendations only**:
`POST /api/wikipedia/recommendation` produces a *Wikipedia update
recommendation* object: neutral proposed wording, exact page/section, the
supplied source URLs (each assessed independent / primary / weak), the reason
for the change, a mandatory paid/COI disclosure, and a ready-to-review
**talk-page request draft** (`{{request edit}}`).

Requests are rejected when: the proposed text, reason, or section contains
common promotional wording (a heuristic keyword screen - not exhaustive, so
human WP:NPOV review is always still required), no sources are supplied, all
sources are primary/weak (client-owned domains including declared
`client_domains`, Wikipedia itself, social, press-release, or AI-generated
links), a source is not a valid URL, or the page does not exist. Citations are
never invented; supplied sources still require human verification.

**Why draft/review-only:** Wikipedia's conflict-of-interest and
paid-contribution policies (WP:COI, WP:PAID) require disclosure and strongly
discourage direct article editing by paid contributors; undisclosed promotional
editing can get content reverted and accounts blocked, and harms the client.
Pulse therefore contains **no code that edits Wikipedia** - not manually
triggered, not automated. `WIKIPEDIA_UPDATE_MODE` supports only `draft_only`;
any other value is refused. If posting automation were ever wanted, it would
additionally require explicit admin configuration and Wikipedia bot-policy
approval, and would be a separate, deliberately gated build.

## Configuration
```
WIKIPEDIA_MONITOR_ENABLED=true    # default true; false removes the source entirely
WIKIPEDIA_USER_AGENT=             # recommended: add operator contact per Wikimedia UA policy
WIKIPEDIA_UPDATE_MODE=draft_only  # default and only supported value
```

## Client-facing answer
"Yes - we support Wikipedia monitoring, and we support compliant Wikipedia
update recommendations and talk-page request drafting with full COI/paid
disclosure. We do not do covert or automatic promotional Wikipedia editing."
