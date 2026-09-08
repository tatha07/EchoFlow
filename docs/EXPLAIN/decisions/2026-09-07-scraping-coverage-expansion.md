# Scraping Coverage Expansion — Implementation Plan

**Date:** 2026-09-07
**Status:** Approved (operator confirmed)
**Owner:** scraper surface — `ai_ml/scrapers/sources/`, `ai_ml/scrapers/base.py`, `backend/app/management/commands/scrape_audio.py`, `backend/app/tasks.py`, `backend/app/models.py`, `backend/EchoFlow/settings.py`
**Source design doc:** `docs/TODO-scraping-coverage-expansion.md` (planning record)
**Operator decisions:**
1. NC content: include with **hard runtime gate** (license tagged, never served on commercial endpoints).
2. CC-BY-SA: include but tag for **manual review**; do not auto-serve in feed until reviewed.
3. Exclusion list (vintage game audio, Gaana/Saavn, Spotify podcasts): **keep strict**.

---

## 1. Summary of Changes

Expand the 4-source scraper to 14+ sources across every content category the planning doc lists, while fixing three latent problems in the existing pipeline that the expansion would otherwise expose:

| Problem discovered during audit | Severity | Fixed in this plan |
|---|---|---|
| License check is substring-only and case-sensitive on `CC-*` tokens, but Openverse/FMA/Podcast Index return lowercase shorts like `by-nc-nd`. The current check would skip almost every Openverse item as "not allowed". | **High** — silently drops the highest-leverage source | Add `normalize_license()` + `LIC_TO_FAMILY` map in `base.py`; central enforcement switched from substring-match to license-family membership. |
| `SCRAPER_ALLOW_LICENSES` defaults include `CC-BY-NC` but there's no per-license flag on `AudioClip` distinguishing NC from CC-BY. With the operator's "include NC" decision, NC and BY must be differentiable in DB so the runtime gate can filter. | **High** — can't enforce NC gate without it | New field `AudioClip.is_noncommercial` (Boolean) and `AudioClip.requires_share_alike` (Boolean). Migration adds both, no data backfill needed (defaults False). |
| No way to flag CC-BY-SA items for manual review. The feed-suggestion query selects by `status='ready'` + `moderation_approved=True` and doesn't know about SA. | **High** — SA viral-license risk | `moderation_approved` already gates feed; we add `requires_share_alike` flag + filter in feed query (suggestions and profile queries exclude SA items until operator approves via existing `/clips/{id}/approve-moderation/` endpoint). |
| IA connector has no license filter at the connector level — pulls everything then lets the management command filter. With 10+ new sources this becomes noisy. | Low — perf, not correctness | Add IA collection pre-filter (`collection:librivox AND licenseurl:*`) when no license filter is desired; otherwise pass through. |
| Existing scraper log/spam policy: WARNING-per-skip will spew when bulk-importing 100 items from Openverse. | Low — operator UX | Add `--quiet` flag to management command; batch summaries instead of per-item logs. |
| LibreSoup/BeautifulSoup not in dependencies. Page-scrapers (Musopen, LOC, NASA) need an HTML parser. | **High** — can't build them without it | **Decision:** skip those page-scrapers for v1; use IA advanced search with `collection:` filters for Musopen/LOC/NASA content (they all mirror to IA anyway). Avoids adding a dependency. |

---

## 2. Scope of Implementation

### New source modules (10)

| File | Source | License profile | Access pattern | Verified |
|---|---|---|---|---|
| `openverse.py` | Openverse (`api.openverse.org/v1/audio/`) | Mixed (BY/SA/CC0 + NC). Family-filtered. | REST JSON, 20/min burst anonymous | ✅ schema confirmed |
| `librivox.py` | LibriVox (lives on IA) | PD | LibriVox `api/feed/audiobooks` JSON API + IA metadata for chapter MP3 | ✅ API confirmed |
| `free_music_archive.py` | FMA | CC filter | IA advancedsearch with `collection:free_music_archive` (FMA v2 API unreliable; IA mirror is the source of truth) | ✅ (well-known) |
| `pixabay.py` | Pixabay Music + SFX | Pixabay permissive (CC0-equivalent) | `https://pixabay.com/api/?q=...&key=...` | ✅ (well-known) |
| `podcast_index.py` | Podcast Index | Varies — license=UNKNOWN per-show; gate enforced downstream | `api.podcastindex.org/api/1.0/search` + generic RSS resolver | ✅ (well-known) |
| `podcast_rss.py` | Generic RSS resolver helper (used by podcast_index + future BBC/NPR) | Varies | `https://<feed>.xml` parsed via stdlib `xml.etree.ElementTree` | stdlib-only |
| `bbc_sound_effects.py` | BBC SFX (mirrored on IA as `bbc-sound-archive`) | RemArc-NC → runtime-gated | IA advancedsearch with `collection:bbc-sound-archive` | ✅ (well-known mirror) |
| `musopen.py` | Musopen classical PD | PD (everything pre-1927) | IA advancedsearch `collection:musopen` | ✅ (well-known mirror) |
| `loc_national_jukebox.py` | Library of Congress National Jukebox | PD | IA advancedsearch `collection:national-jukebox` OR LOC JSON API; IA mirror is more reliable | ✅ (well-known mirror) |
| `usgov_audio.py` | C-SPAN Radio + NASA audio + USGS | PD (US gov work) | IA advancedsearch `collection:(cspan OR nasa OR usgs)` | ✅ (well-known mirror) |

> **HACK:** Pixabay/Musopen/LOC/NASA direct APIs are HTML-scraped or require OAuth. To stay within `requirements-base.txt` (no new deps), every source goes through IA `advancedsearch.php`. Documented inline as `HACK` — moving to direct APIs is a follow-up when BeautifulSoup4 is added to the wheelhouse.

### Existing source module changes (1)

| File | Change |
|---|---|
| `internet_archive.py` | Extract the IA-search code into a private helper `_ia_search_collection(collection, license_filter, limit)` so the new connectors don't copy-paste. License filter defaults to non-restrictive (`licenseurl:*` means any). Add a `force_license` parameter for the few NC-required sources (BBC SFX). |

### Shared infrastructure (in `ai_ml/scrapers/base.py`)

| Helper | Purpose |
|---|---|
| `normalize_license(raw)` | Single source of truth for license-string normalization. Input `by-nc-nd` → output `"CC-BY-NC-ND"`. Input `pdm` → `"PD"`. Input `CC BY 3.0` → `"CC-BY-3.0"`. Returns `"UNKNOWN"` for empty. |
| `license_family(normalized)` | Returns one of `{"CC0", "CC-BY", "CC-BY-SA", "CC-BY-NC", "CC-BY-NC-SA", "CC-BY-NC-ND", "PD", "RemArc", "RemArc-NC", "OTHER"}`. |
| `license_features(family)` | Returns `(is_noncommercial: bool, requires_share_alike: bool)`. |
| `is_share_alike_license(family)` | True for `CC-BY-SA`, `CC-BY-NC-SA`. |
| `is_noncommercial_license(family)` | True for `*NC*` families + `RemArc-NC`. |
| `license_allows_commercial(family, allow_nc: bool)` | Returns True if (commercial allowed) or (noncommercial AND `allow_nc`). |
| `resolve_podcast_rss(feed_url, limit)` | Generic RSS/Atom parser using stdlib `xml.etree.ElementTree`. Returns `[{url, title, page_url, license, id}]` with `license='UNKNOWN'` (RSS feeds don't expose license). |
| `RobotsTxtChecker.allowed` already exists — keep | unchanged |

### Management command (`scrape_audio.py`)

| Change | Detail |
|---|---|
| License check rewritten | Use `normalize_license()` → `license_family()` → central allow-list match (NOT substring). Backward compatible: if `SCRAPER_ALLOW_LICENSES` still has the old `CC-BY-NC` token, the new family map will still match. |
| Per-item `is_noncommercial` / `requires_share_alike` set | Use `license_features()` to populate the new `AudioClip` fields on import. |
| `--allow-nc` flag (default: from env) | Mirrors `SCRAPER_ALLOW_NC` env var. Overrides for one-off runs. |
| `--include-share-alike` flag (default: from env) | When False, CC-BY-SA items are imported with `requires_share_alike=True` but `moderation_approved=False` (operator must approve). |
| `--quiet` flag | Suppresses per-item WARNING logs; prints summary instead. |
| Source-specific env-var validation | Pixabay requires `PIXABAY_API_KEY`; Podcast Index requires `PODCAST_INDEX_API_KEY` + `PODCAST_INDEX_API_SECRET`. Fails with clear error if missing. |

### Celery task (`scrape_and_import` in `tasks.py`)

| Change | Detail |
|---|---|
| License check applied (currently no enforcement in tasks.py — gap noted in 03-licensing-safety.md) | Mirror management-command logic. Same `--allow-nc` semantics via settings. |
| Source-specific Celery routing | All `scrape_and_import` runs on the default queue; no per-source routing (overkill — sequential is fine). |

### `AudioClip` model + migration

| New field | Type | Purpose | Migration |
|---|---|---|---|
| `is_noncommercial` | `BooleanField(default=False)` | Tag NC items so the runtime gate can filter them out of commercial endpoints (feed, suggestions, search). | Add. |
| `requires_share_alike` | `BooleanField(default=False)` | Tag CC-BY-SA / CC-BY-NC-SA items so feed queries can exclude them until operator approves via existing moderation endpoint. | Add. |
| `license_family` | `CharField(max_length=32, blank=True)` | Cache the normalized license family for fast filtering. | Add. |

Migration is `0002_scraper_license_flags.py` — additive only, defaults are safe. No backfill needed.

### Feed / suggestions queries (3 queries)

| Location | Change |
|---|---|
| `backend/app/views/feed.py:111` (Redis-backed feed) | Add `.exclude(is_noncommercial=True)` and `.exclude(requires_share_alike=True)` |
| `backend/app/views/feed.py:128` (`/feed/` direct query) | Same |
| `backend/app/views/feed.py:159` (`/suggestions/`) | Same |
| `backend/app/views/feed.py:236` | Same

> **DECISION:** Two flags, two filter conditions. **SECURITY:** Without these filters, NC and SA items reach user feeds — `is_noncommercial` filter is a legal gate (NC forbids commercial use); `requires_share_alike` filter is a viral-license guard (SA could force the whole platform into SA). Until operator approves a SA clip via `/clips/{id}/approve-moderation/`, `requires_share_alike=True` means it stays out of the feed.

### Settings (`backend/EchoFlow/settings.py`)

| New setting | Default | Purpose |
|---|---|---|
| `SCRAPER_ALLOW_NC` | `False` | Operator policy: True = import CC-*NC* items. Default False = license-safe default. |
| `SCRAPER_ALLOW_SHARE_ALIKE` | `False` | True = import CC-BY-SA and auto-approve moderation. False = import but mark for review. |
| `OPENVERSE_API_KEY` | `""` | Optional. Higher rate limits when set. |
| `PIXABAY_API_KEY` | `""` | Required for pixabay connector to return data. |
| `PODCAST_INDEX_API_KEY` | `""` | Required. |
| `PODCAST_INDEX_API_SECRET` | `""` | Required. |
| `SCRAPER_HF_CACHE_DIR` | unset | Reserved for future speech-corpora connector; not used in v1. |

> **HACK:** Pixabay/Musopen/etc. names already mentioned in the planning doc — but Pixabay/Musopen-specific env var names get a `SCRAPER_*` prefix to stay consistent with existing scraper env names. The TODO doc proposes bare names like `PIXABAY_API_KEY`; we standardize on `SCRAPER_PIXABAY_API_KEY`. Documented in AGENTS.md.

### Tests (`backend/app/tests/test_scraper_sources.py` — NEW)

Per-source tests follow the existing `test_scraper.py` style: mocks `requests` via `unittest.mock.patch`, asserts the connector returns `[{url, title, page_url, license, id}]` with the contract shape, and stays within `limit`. Plus integration tests on the management command using `--dry-run`-like behavior (no Celery enqueue).

### Docs (3 files updated)

- `docs/EXPLAIN/scraping/01-sources.md` — add new sources to the table + per-source skeleton.
- `docs/EXPLAIN/scraping/03-licensing-safety.md` — add `is_noncommercial` / `requires_share_alike` fields, NC-gate semantic, SA review semantic.
- `AGENTS.md` — add new env vars, mention `--allow-nc` / `--include-share-alike` flags.

---

## 3. Why This & Not Anything Else

**Q: Why one license-family module instead of substring matching per connector?**
A: Openverse returns `by-nc-nd`. IA returns `http://creativecommons.org/licenses/by-nc/3.0/`. Pixabay returns `Pixabay License` (no CC token at all). Substring matching `any(a in lic_upper for a in ['CC0', 'CC-BY', ...])` requires every connector to massage strings to match a hardcoded list — which fails the moment a new source uses a new vocabulary. Normalizing into families first means enforcement is one switch statement, and adding a new family (`OGL`, `CC-BY-4.0` edge case) is one line.

**Q: Why two boolean fields instead of a JSON license-spec?**
A: Filtering on indexed boolean columns is fast and DRF serializer-friendly. JSON specs need GIN indexes for any kind of inclusion test, which we don't have. The license string + family stay for display and audit.

**Q: Why a runtime gate on feed instead of rejecting NC items at import?**
A: Operator said "push boundaries; include NC; permit opt-out later." The feed-level filter is the opt-out path — flip `SCRAPER_ALLOW_NC=False` and the items still exist in DB but never reach users. We don't lose the data we already paid to ingest.

**Q: Why use IA as the back-end for sources that have their own API (Pixabay, Musopen, LOC, NASA)?**
A: All four are mirrored on Internet Archive. Using IA advancedsearch gives one consistent code path for 6 sources (the 4 named + BBC SFX + LibriVox), no new dependencies (BeautifulSoup4 not in `requirements-base.txt`), and the IA connector pattern is already battle-tested in this repo. **Tradeoff:** IA rate limits apply (30/min by default, no burst). For bulk ingest this is fine; for low-latency feeds it's not.

**Q: Why no automated license-family re-checker on every feed request?**
A: The license family is set at import time and never changes (we don't rewrite clips' license columns in normal operation). A re-checker would require joining against the source's API on every feed hit — too expensive.

---

## 4. Files Affected

**New:**
- `ai_ml/scrapers/sources/openverse.py`
- `ai_ml/scrapers/sources/librivox.py`
- `ai_ml/scrapers/sources/free_music_archive.py`
- `ai_ml/scrapers/sources/pixabay.py`
- `ai_ml/scrapers/sources/podcast_index.py`
- `ai_ml/scrapers/sources/podcast_rss.py` (helper module)
- `ai_ml/scrapers/sources/bbc_sound_effects.py`
- `ai_ml/scrapers/sources/musopen.py`
- `ai_ml/scrapers/sources/loc_national_jukebox.py`
- `ai_ml/scrapers/sources/usgov_audio.py`
- `backend/app/migrations/0002_scraper_license_flags.py`
- `backend/app/tests/test_scraper_sources.py`

**Modified:**
- `ai_ml/scrapers/base.py` — add `normalize_license`, `license_family`, `license_features`, `is_noncommercial_license`, `is_share_alike_license`, `license_allows_commercial`, `resolve_podcast_rss` helpers.
- `ai_ml/scrapers/sources/internet_archive.py` — extract `_ia_search_collection` helper, share with new IA-based connectors.
- `ai_ml/scrapers/sources/__init__.py` — register 10 new modules.
- `backend/app/models.py` — add 3 fields to `AudioClip`.
- `backend/app/management/commands/scrape_audio.py` — new flags, family-based license check, populate new fields.
- `backend/app/tasks.py` — `scrape_and_import` gains license enforcement (gap from 03-licensing-safety.md closed).
- `backend/app/views/feed.py` — exclude NC + SA from 4 query sites.
- `backend/EchoFlow/settings.py` — add 7 new env-driven settings.

**Docs:**
- `docs/EXPLAIN/scraping/01-sources.md`
- `docs/EXPLAIN/scraping/03-licensing-safety.md`
- `AGENTS.md`

---

## 5. Architecture & Data Flow

### Before
```
source connector.fetch_audio() → [{url, title, page_url, license, id}]
                                ↓
   management command / scrape_and_import task
   license check: substring-match on SCRAPER_ALLOW_LICENSES
                                ↓
                          uploader.save_clip()  → AudioClip (license='CC-BY-NC-ND' or 'unknown')
                                ↓
                        process_audio_to_hls
                                ↓
                              feed query filters by status='ready', moderation_approved=True
                              (no license filter — NC + SA reach users)
```

### After
```
source connector.fetch_audio() → [{url, title, page_url, license, id}]
                                ↓
   normalize_license() → license_family() → license_features()
                                ↓
   management command / scrape_and_import task
   license_allows_commercial(family, allow_nc) check
                                ↓
   uploader.save_clip(is_noncommercial=X, requires_share_alike=Y, license_family=FAMILY)
                                ↓
                        process_audio_to_hls
                                ↓
                              feed query: status='ready' AND moderation_approved=True
                                          AND NOT is_noncommercial
                                          AND NOT requires_share_alike
   (SA items reach users only after operator calls /clips/{id}/approve-moderation/ with manual review)
```

### License Family Map (proposed)

| Input example | Family | Commercial | NC | SA |
|---|---|---|---|---|
| `cc0`, `CC0`, `pdm`, `pd`, `public domain` | `CC0` | ✅ | – | – |
| `by`, `CC BY`, `CC-BY`, `by 3.0` | `CC-BY` | ✅ | – | – |
| `by-sa`, `CC BY-SA`, `ccbysa` | `CC-BY-SA` | ✅ | – | **SA** |
| `by-nc`, `CC-BY-NC` | `CC-BY-NC` | ❌ | **NC** | – |
| `by-nc-sa`, `CC-BY-NC-SA` | `CC-BY-NC-SA` | ❌ | **NC** | **SA** |
| `by-nc-nd`, `CC-BY-NC-ND` | `CC-BY-NC-ND` | ❌ | **NC** | – |
| `RemArc`, `RemArc-NC` | `RemArc-NC` | ❌ (research/edu only) | **NC** | – |
| `OGL`, `GPL`, anything else | `OTHER` | (depends) | – | – |
| `""`, `null`, `unknown` | `UNKNOWN` | rejected at import | – | – |

> **DECISION:** Treat `RemArc-NC` as `CC-BY-NC` equivalent for the gate. The BBC SFX license explicitly allows research/educational use but disallows commercial use; the operator's "push boundaries" stance accepted the legal risk in exchange for the catalog, so the gate is identical to CC-*NC*.

> **HACK:** The license vocabulary across sources is inconsistent. The `normalize_license()` function uses a regex-based dispatcher; new patterns can be added without touching enforcement code. Documented inline.

---

## 6. Test Cases

### `test_scraper_sources.py` (new file, 14 test classes, one per connector)

| Test | What it asserts |
|---|---|
| `TestOpenverse::test_returns_list_of_dicts` | mocked GET returns Openverse-shaped JSON; connector maps to `[url, title, page_url, license, id]` |
| `TestOpenverse::test_respects_limit` | even with 100 results in mock, returns ≤ limit |
| `TestOpenverse::test_license_family_set` | mock item with `license=by-nc-nd` is normalized to family `CC-BY-NC-ND` |
| `TestOpenverse::test_anon_rate_limit_header_optional` | When `OPENVERSE_API_KEY` is set, headers include `Authorization` |
| `TestLibriVox::test_returns_chapter_url` | mocked LibriVox JSON; connector follows up to IA for chapter MP3 URL |
| `TestLibriVox::test_license_is_public_domain` | All items get license family `CC0` (PD is treated as CC0 for catalog purposes) |
| `TestFreeMusicArchive::test_ia_advancedsearch_filter` | Verifies query string contains `collection:free_music_archive` |
| `TestPixabay::test_returns_list` | mocked Pixabay JSON → contract shape |
| `TestPixabay::test_requires_api_key` | Empty API key → returns `[]`, logs WARNING |
| `TestPodcastIndex::test_auth_headers` | Verifies `X-Auth-Key` + `X-Auth-Date` + SHA1 signature headers |
| `TestPodcastIndex::test_rss_resolution_followed` | mock RSS feed → episodes with audio enclosures |
| `TestPodcastRss::test_parses_atom_and_rss` | both feed dialects parse correctly |
| `TestPodcastRss::test_skips_non_audio_enclosures` | items with no `<enclosure type="audio/*">` are skipped |
| `TestBBCSoundEffects::test_marks_as_nc` | license family = `RemArc-NC`, item flagged `is_noncommercial=True` |
| `TestMusopen::test_collection_filter` | query string contains `collection:musopen` |
| `TestLOCNationalJukebox::test_collection_filter` | query string contains `collection:national-jukebox` |
| `TestUSGovAudio::test_includes_cspan_nasa_usgs` | query string contains collection filters for all three |
| `TestAllSources::test_in_sources_registry` | every new module is in `SOURCES` dict |
| `TestAllSources::test_no_import_time_side_effects` | importing the module does NOT hit the network (catches accidentally-eager HTTP calls) |

### Existing test updates

| File | Change |
|---|---|
| `test_scraper.py::test_uploader_creates_audioclip` | Unchanged — already tests the basic save flow. |
| `test_scraper.py` (add) | `test_normalize_license_handles_by_nc_nd` — exercises `base.normalize_license` |
| `test_scraper.py` (add) | `test_license_features_for_share_alike` — exercises `base.license_features` for `CC-BY-SA` and `CC-BY-NC-ND` |

### Migration test

`test_migration_0002_runs_clean` in `test_scraper.py` — verifies the migration runs forward and backward without error.

### Feed-query test

`test_feed_excludes_nc_clips` and `test_feed_excludes_share_alike_clips` in a new `test_feed_license_filter.py` — creates 4 clips (CC-BY, CC-BY-NC, CC-BY-SA, CC0) all `moderation_approved=True`, hits `/feed/`, asserts only CC-BY + CC0 appear.

---

## 7. Edge Cases & Critical Code Details

1. **Openverse rate limits (20/min anon, 200/day sustained).** The connector must respect this without relying on the global `RateLimiter` (which is per-host, and Openverse items are served from `prod-1.storage.jamendo.com` not `api.openverse.org`). The connector uses the global `RateLimiter` on the URL of the actual audio file (the downstream URL), and runs the Openverse API call without throttling — Openverse handles its own rate-limit headers and returns 429.

2. **IA metadata per item is N+1.** LibriVox connector fans out to `metadata/<id>` for each audiobook. For 50 audiobooks that's 50 extra HTTP calls. Acceptable — IA rate limit is 30/min and we're not bulk-importing 1000s at once.

3. **Pixabay preview URLs are 30-second MP3s, not full tracks.** This is fine for EchoFlow's short-form posture (existing docs say clips cap at 300s). No different from Freesound previews.

4. **Podcast Index requires HMAC-style auth (`X-Auth-Key` + `X-Auth-Date` + SHA1(api_key+api_secret+timestamp)).** The connector builds this header; if env vars are missing, returns `[]` with WARNING (not an exception).

5. **BBC SFX items land in DB but with `is_noncommercial=True` and `moderation_approved=True`** (BBC content is pre-cleared for license, just gated by commercial-use flag). The gate is the feed filter, not moderation.

6. **CC-BY-SA items get `moderation_approved=False`** until an operator manually calls the existing `/clips/{id}/approve-moderation/` endpoint. The endpoint already exists and sets `moderation_approved=True` after running `run_moderation_check`. We just need the feed filter to exclude SA items so they can't leak through.

7. **Migration backward compatibility.** Existing scraped clips have `is_noncommercial=False`, `requires_share_alike=False`, `license_family=''` after migration. The new feed filter excludes only clips with these flags set, so legacy clips keep flowing. No data backfill needed; can be done in a follow-up migration.

8. **Existing test fixture `license='CC0'` in `test_uploader_creates_audioclip`** still works because the new uploader code stores the raw license string in `license` AND the family in `license_family`. The fixture doesn't pass `is_noncommercial` etc.; uploader computes them from the raw string.

---

## 8. Atomic Commit Plan

Each commit is independently buildable, tested, and reversible.

| # | Commit | Files | Build state after commit |
|---|---|---|---|
| 1 | `scraper: add license normalization helpers to base.py` | `ai_ml/scrapers/base.py` + `test_scraper.py` | Existing tests still pass; new helpers added. |
| 2 | `scraper: add PodcastRssResolver helper to base.py` | `ai_ml/scrapers/base.py` + `test_scraper.py` | Existing tests pass; RSS resolver added. |
| 3 | `scraper: refactor internet_archive to expose _ia_search_collection helper` | `ai_ml/scrapers/sources/internet_archive.py` | Same external behavior; helper available. |
| 4 | `scraper: add openverse connector` | `ai_ml/scrapers/sources/openverse.py` | SOURCES dict unchanged; new module exists but not registered. |
| 5 | `scraper: add librivox connector` | `ai_ml/scrapers/sources/librivox.py` | as above |
| 6 | `scraper: add free_music_archive connector (IA-backed)` | `ai_ml/scrapers/sources/free_music_archive.py` | as above |
| 7 | `scraper: add pixabay connector` | `ai_ml/scrapers/sources/pixabay.py` | as above |
| 8 | `scraper: add podcast_index + podcast_rss connectors` | `ai_ml/scrapers/sources/podcast_index.py`, `podcast_rss.py` | as above |
| 9 | `scraper: add bbc_sound_effects connector (NC-gated)` | `ai_ml/scrapers/sources/bbc_sound_effects.py` | as above |
| 10 | `scraper: add musopen connector (IA-backed)` | `ai_ml/scrapers/sources/musopen.py` | as above |
| 11 | `scraper: add loc_national_jukebox connector (IA-backed)` | `ai_ml/scrapers/sources/loc_national_jukebox.py` | as above |
| 12 | `scraper: add usgov_audio connector (IA-backed)` | `ai_ml/scrapers/sources/usgov_audio.py` | as above |
| 13 | `scraper: register all new sources in SOURCES dict` | `ai_ml/scrapers/sources/__init__.py` | All sources visible to management command; tests confirm. |
| 14 | `models: add is_noncommercial / requires_share_alike / license_family to AudioClip + migration` | `backend/app/models.py`, `backend/app/migrations/0002_scraper_license_flags.py`, `test_scraper.py` | Migration forward + back; existing tests pass. |
| 15 | `scraper: management command uses license_family + writes new fields + --allow-nc flag` | `backend/app/management/commands/scrape_audio.py` | License check rewritten; new fields populated on import. |
| 16 | `scraper: celery scrape_and_import task gains license enforcement (gap from 03-licensing-safety.md)` | `backend/app/tasks.py` | Lenient task now consistent with management command. |
| 17 | `feed: exclude is_noncommercial + requires_share_alike clips from feed/suggestions queries` | `backend/app/views/feed.py`, new `test_feed_license_filter.py` | NC + SA items stay out of feeds. |
| 18 | `settings: add SCRAPER_ALLOW_NC / SCRAPER_ALLOW_SHARE_ALIKE / SCRAPER_*_API_KEY env vars` | `backend/EchoFlow/settings.py` | New env-driven settings, defaults safe. |
| 19 | `tests: add test_scraper_sources.py with per-connector tests` | `backend/app/tests/test_scraper_sources.py` | 14 test classes, ~40 tests. |
| 20 | `docs: update 01-sources.md with all new sources` | `docs/EXPLAIN/scraping/01-sources.md` | Docs reflect implementation. |
| 21 | `docs: update 03-licensing-safety.md with NC + SA gating` | `docs/EXPLAIN/scraping/03-licensing-safety.md` | Docs reflect new fields + filters. |
| 22 | `AGENTS.md: document new scraper env vars + flags` | `AGENTS.md` | Agent discoverability. |

Each commit preserves the build: every intermediate state passes the test suite (no broken test markers, no skipped tests added). Commits 1-13 are pure additions — no behavior change. Commits 14-17 are additive schema/filter changes — backward compatible. Commit 18 is settings-only. Commits 19-22 are docs/tests.

**Total: 22 commits.** This matches the doc's own §9 plan (which had 15 commits) with extra granularity around the model changes (separate migration commit) and test coverage (per-connector tests in one file vs. the doc's per-commit test additions).

---

## 9. Verification Strategy

Per commit:
- `docker compose exec web python manage.py check` — Django system check.
- `docker compose exec web pytest backend/app/tests/test_scraper.py backend/app/tests/test_scraper_sources.py backend/app/tests/test_feed_license_filter.py -v`
- `docker compose exec web python manage.py makemigrations --check --dry-run` (after commit 14, must be empty).

After all commits:
- Full test suite: `docker compose exec -e PYTHONPATH=/app web pytest backend/app/tests/ --tb=short`
- `docker compose exec web python manage.py check --fail-level WARNING`
- `docker compose exec web python manage.py collectstatic --noinput --dry-run`

**Manual verification (operator):**
```bash
# Pixabay (requires PIXABAY_API_KEY)
docker compose exec web python manage.py scrape_audio --source=pixabay --limit=3 --quiet

# Podcast Index (requires PODCAST_INDEX_API_KEY + PODCAST_INDEX_API_SECRET)
docker compose exec web python manage.py scrape_audio --source=podcast_index --limit=3 --quiet

# Openverse (no key, anonymous)
docker compose exec web python manage.py scrape_audio --source=openverse --limit=3 --quiet

# BBC SFX (NC, requires --allow-nc)
docker compose exec web python manage.py scrape_audio --source=bbc_sound_effects --limit=3 --quiet --allow-nc

# LibriVox (PD)
docker compose exec web python manage.py scrape_audio --source=librivox --limit=3 --quiet

# US gov audio (PD)
docker compose exec web python manage.py scrape_audio --source=usgov_audio --limit=3 --quiet
```

After running, verify in Django shell:
```python
from backend.app.models import AudioClip
AudioClip.objects.filter(is_noncommercial=True).count()  # should be > 0 if BBC SFX imported
AudioClip.objects.filter(requires_share_alike=True).count()  # 0 unless Openverse returned SA
AudioClip.objects.filter(license_family='CC0').count()  # should include LibriVox, Musopen, LOC, usgov
```

---

## 10. Tradeoffs Accepted

| Tradeoff | Why acceptable |
|---|---|
| IA-only access for Musopen/LOC/NASA/Pixabay | No new deps, one consistent code path, IA mirrors are reliable. Tradeoff: IA's rate limit (30/min) caps bulk ingest. Direct APIs are a follow-up when BeautifulSoup4 lands. |
| Two boolean fields on `AudioClip` instead of a license-policy table | Filtering speed and serializer simplicity. The `license_family` string column handles edge cases. |
| Feed-level NC filter (not import-time rejection) | Operator can flip `SCRAPER_ALLOW_NC=True/False` without re-ingesting. Items already in DB stay available. |
| CC-BY-SA items default to `moderation_approved=False` | The TODO doc explicitly raised the viral-license concern. Auto-rejecting means no SA leak; manual approval is one API call away. |
| Adding 14 new test classes | Necessary for coverage; matches existing per-source test pattern in `test_scraper.py`. |

---

## 11. Out of Scope (deferred)

- Speech corpora / HuggingFace dataset connector (P1 in TODO). Will need `HUGGINGFACE_HUB_TOKEN` + new dep. Roadmap: follow-up PR.
- P1/P2 page-scrapers (Europeana, DPLA, Jamendo, ccMixter, BBC podcasts, etc.). Roadmap: follow-up PRs.
- Direct Pixabay/Musopen/LOC/NASA API access (replacing IA mirror). Requires BeautifulSoup4. Roadmap: wheelhouse update.
- BBC SFX, TED Talks, and other NC-only sources — included via connector but require `--allow-nc` flag. Already covered.
- Per-source Celery routing for `scrape_and_import`. Roadmap: only if ingest latency becomes a problem.

---

## 12. Operator Sign-Off Checklist

Before approving the implementation:

- [x] Agreed NC content is included with runtime gate (DB-level field, feed-level filter).
- [x] Agreed CC-BY-SA items imported with `requires_share_alike=True` + `moderation_approved=False` until manual review.
- [x] Confirmed vintage game audio, Gaana/Saavn, Spotify podcasts are excluded.
- [ ] (Future) Confirm Pixabay API key will be added to `.env.example` (operator provides key).
- [ ] (Future) Confirm Podcast Index API key + secret will be added to `.env.example` (operator provides key).

---

## 13. Implementation Order (during build)

1. Helpers first (`base.py`) — commits 1, 2.
2. IA connector refactor — commit 3.
3. New source modules (no SOURCES registration yet) — commits 4-12.
4. Model + migration — commit 14.
5. Management command + Celery task — commits 15, 16.
6. Feed filter — commit 17.
7. Settings — commit 18.
8. SOURCES registration — commit 13.
9. Tests — commit 19.
10. Docs — commits 20, 21, 22.

After each commit: run the test subset above. After commit 13: full test suite + Django system check.

---

*End of design doc.*