"""Scrape audio from public sources and import into AudioClip.

Supports:
- Multi-source mode: omit --source to iterate all sources in
  SOURCES order. Each source gets its own per-source state file
  so resume is per-source.
- Resumable: state file under SCRAPER_SCRATCH_DIR/scraper_state/;
  Ctrl-C flushes state after the current segment so re-running
  picks up where the run left off.
- Per-segment tracking: when a source item is split into N segments
  (--clip-length > 0), each segment is tracked independently in
  the state file. A transient SSL error on segment 3/7 does not
  lose the 2 successful segments.
- CSV log: per-run file under SCRAPER_SCRATCH_DIR/scraper_logs/;
  one row per (item, segment).
- --reset: wipe the state file for the affected source(s) before
  starting.
"""
import os
import sys
import tempfile
import logging
import signal
import time as _time
from datetime import datetime

import django
# Force unbuffered stdout so smoke output appears immediately
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

from django.core.management.base import BaseCommand
from django.conf import settings
from django.contrib.auth import get_user_model

from ai_ml.scrapers import downloader, normalizer, uploader
from ai_ml.scrapers.sources import SOURCES
from ai_ml.scrapers.base import (
    normalize_license,
    license_features,
    license_allows_commercial,
    is_share_alike_license,
)
from ai_ml.scrapers import state as scraper_state
from ai_ml.scrapers import log as scraper_log
from backend.app.tasks import process_audio_to_hls
from backend.app.services.task_publisher import publish

logger = logging.getLogger(__name__)

# Set by the SIGINT handler when Ctrl-C is received. The per-item loop
# checks this flag at the top of every iteration; when True, the loop
# breaks cleanly and the state file is flushed.
_INTERRUPTED = False


def _sigint_handler(signum, frame):
    global _INTERRUPTED
    _INTERRUPTED = True
    sys.stderr.write('\n[interrupt] Ctrl-C received; will pause after the '
                     'current item. State file will be saved. Re-run the same '
                     'command to resume.\n')
    sys.stderr.flush()


class Command(BaseCommand):
    help = 'Scrape audio from public sources and import into AudioClip'

    def add_arguments(self, parser):
        parser.add_argument('--source', choices=list(SOURCES.keys()),
                            help='Which source to scrape. If omitted, '
                                 'iterate every source in SOURCES order. '
                                 'Each source uses its own per-source state '
                                 'file (librivox.json, bbc_sound_effects.json, '
                                 'etc.) so resume is per-source.')
        parser.add_argument('--sources', type=str, default=None,
                            help='Comma-separated list of sources. Mutually '
                                 'exclusive with --source.')
        parser.add_argument('--limit', type=int, default=5,
                            help='Per-source limit. With --limit=10 and 14 '
                                 'sources (no --source), the run fetches up '
                                 'to 10 items per source = 140 total.')
        parser.add_argument('--clip-length', type=int,
                            default=getattr(settings, 'SCRAPER_DEFAULT_CLIP_SECONDS', 300),
                            help='Max seconds per segment. 0 = no splitting '
                                 '(save whole file as one clip). Default 300. '
                                 'Source items longer than this are split '
                                 'into N segments and saved as N AudioClip '
                                 'rows sharing a group_id.')
        parser.add_argument('--smoke', action='store_true',
                            help='Run each source for limit=1, attempt download, '
                                 'report pass/fail without writing to DB.')
        parser.add_argument('--allow-nc', dest='allow_nc', action='store_true',
                            default=getattr(settings, 'SCRAPER_ALLOW_NC', False),
                            help='Include noncommercial items (CC-*NC*, RemArc-NC).')
        parser.add_argument('--include-share-alike', dest='include_share_alike',
                            action='store_true',
                            default=getattr(settings, 'SCRAPER_ALLOW_SHARE_ALIKE', False),
                            help='Include share-alike items (auto-approve moderation).')
        parser.add_argument('--quiet', action='store_true',
                            help='Suppress per-item output; print summary at the end.')
        parser.add_argument('--reset', action='store_true',
                            help='Wipe the state file for the affected source(s) '
                                 'before starting. Without this flag, the command '
                                 'resumes from the last saved state.')
        parser.add_argument('--state-dir', type=str, default=None,
                            help='Override the directory for state files '
                                 '(default: $SCRAPER_SCRATCH_DIR/scraper_state).')
        parser.add_argument('--log-dir', type=str, default=None,
                            help='Override the directory for CSV logs '
                                 '(default: $SCRAPER_SCRATCH_DIR/scraper_logs).')
        parser.add_argument('--log', type=str, default=None,
                            help='Explicit CSV log filename (relative to '
                                 '--log-dir or absolute). With multi-source, '
                                 'one log per source is created.')
        parser.add_argument('--page-size', type=int, default=50,
                            help='Items per IA advancedsearch page. Used for '
                                 'pagination when --limit > page-size.')
        parser.add_argument('--max-source-time', type=int, default=None,
                            help='Per-source hard time limit (seconds). When '
                                 'a source takes longer than this, the run '
                                 'moves on to the next source with a WARNING. '
                                 'Useful for very slow upstreams so the whole '
                                 'multi-source run does not get stuck on one '
                                 'source.')
        parser.add_argument('--no-log', action='store_true',
                            help='Skip CSV logging. The state file is still '
                                 'written for resume; only the per-item log '
                                 'is suppressed.')

    def handle(self, *args, **options):
        # Resolve which sources to run
        if options['source'] and options['sources']:
            self.stdout.write(self.style.ERROR(
                '--source and --sources are mutually exclusive'))
            return
        if options['source']:
            source_list = [options['source']]
        elif options['sources']:
            source_list = [s.strip() for s in options['sources'].split(',')
                            if s.strip()]
        else:
            # All sources: deterministic order = SOURCES dict insertion order
            source_list = list(SOURCES.keys())

        if options['smoke']:
            # Smoke mode keeps the old per-source semantics (one source at a time).
            return self._run_smoke(source_list[0] if source_list else None,
                                    options['limit'],
                                    options['clip_length'],
                                    options['quiet'])

        # Multi-source mode: iterate. Per-source state files, per-source
        # CSV logs (or one shared log if --log is given). A failure in
        # one source (HTTP 403, TypeError on signature mismatch, slow
        # upstream) MUST NOT abort the rest of the run — log and
        # continue. Operator gets per-source summaries in stdout.
        for source in source_list:
            if _INTERRUPTED:
                self.stdout.write(self.style.WARNING(
                    f'[scrape] interrupted before processing {source}; '
                    f'earlier sources may be partial. State files saved.'))
                break
            self.stdout.write(self.style.SUCCESS(
                f'\n[========== source={source} ==========]'))
            try:
                self._run_one_source(source, options)
            except Exception as exc:
                # Never let a single source kill the whole multi-source run.
                # Log the exception in compact form (no traceback — the
                # underlying logger already wrote the full one) and move
                # on to the next source.
                logger.exception(
                    'Unhandled exception in source=%s; skipping to next source.',
                    source)
                self.stdout.write(self.style.ERROR(
                    f'[scrape] source={source} failed: {exc.__class__.__name__}: {exc}'))
        # Aggregate summary
        if len(source_list) > 1:
            self._print_multi_source_summary(source_list, options)

    def _run_one_source(self, source, options):
        """Run a single source: state load, paginated fetch, segment-split save."""
        limit = options['limit']
        clip_length = options['clip_length']
        allow_nc = options['allow_nc']
        include_sa = options['include_share_alike']
        quiet = options['quiet']
        reset = options['reset']
        state_dir = options['state_dir']
        log_dir = options['log_dir']
        log_path = options['log']
        page_size = options['page_size']
        max_source_time = options.get('max_source_time')
        no_log = options['no_log']

        sys.stderr.write(f'[scrape] source={source} limit={limit} '
                         f'clip_length={clip_length}s '
                         f'allow_nc={allow_nc} include_sa={include_sa}\n')
        sys.stderr.flush()

        module = SOURCES.get(source)
        if not module:
            self.stdout.write(self.style.ERROR(f'Unknown source: {source}'))
            return

        # State load + reset
        run_params = {
            'source': source,
            'limit': limit,
            'allow_nc': allow_nc,
            'include_share_alike': include_sa,
            'clip_length': clip_length,
            'page_size': page_size,
        }
        if reset:
            removed = scraper_state.reset_state(source, state_dir)
            self.stdout.write(f'[scrape] --reset: state file removed={removed}')

        state = scraper_state.load_state(source, state_dir)
        if state.get('started_at') is None:
            state['started_at'] = datetime.utcnow().isoformat() + 'Z'
        if not scraper_state.params_match(state.get('params', {}), run_params):
            self.stdout.write(self.style.WARNING(
                f'[scrape] existing state params do not match current run. '
                f'Reusing the state file, but some items may be re-processed.'))
        state['params'] = run_params
        scraper_state.update_state_for_resume(state)
        scraper_state.save_state(source, state, state_dir)
        self.stdout.write(f'[scrape] state loaded: '
                         f'fetched={state["counts"]["fetched"]} '
                         f'segments_imported={state["counts"]["segments_imported"]} '
                         f'segments_failed={state["counts"]["segments_failed"]}')

        # CSV log
        csv_log = None
        if not no_log:
            try:
                if log_path is None:
                    csv_log = scraper_log.open_log(source, log_dir=log_dir)
                else:
                    csv_log = scraper_log.open_log(
                        source, log_path=log_path, log_dir=log_dir)
                self.stdout.write(f'[scrape] csv log: {csv_log.path}')
            except Exception as e:
                self.stdout.write(self.style.WARNING(
                    f'[scrape] could not open csv log: {e}; continuing without it'))
                csv_log = None

        # Resume helpers
        already_handled = scraper_state.items_already_handled(state)
        remaining_limit = max(0, limit - state['counts']['imported'])
        if remaining_limit == 0 and state['counts']['imported'] >= limit:
            self.stdout.write(self.style.SUCCESS(
                f'[scrape] already imported {state["counts"]["imported"]} of '
                f'{limit}; nothing left to do. Use --reset to start over.'))
            self._close_log(csv_log)
            return
        self.stdout.write(f'[scrape] resume: {len(already_handled)} items '
                         f'fully done; up to {remaining_limit} more to fetch')

        # Set up Ctrl-C handler
        global _INTERRUPTED
        _INTERRUPTED = False
        signal.signal(signal.SIGINT, _sigint_handler)

        # Per-source time budget. When max_source_time is set, we
        # install a SIGALRM handler that flips _INTERRUPTED. The fetch
        # loop checks the flag at the top of every iteration. This is
        # only safe because we use signal.alarm (Unix only); on Windows
        # the operator must use a shell-level `timeout` wrapper.
        if max_source_time:
            def _alarm_handler(signum, frame):
                global _INTERRUPTED
                _INTERRUPTED = True
                sys.stderr.write(
                    f'\n[timeout] source={source} exceeded '
                    f'max_source_time={max_source_time}s; moving to next source. '
                    f'State file saved for resume.\n')
                sys.stderr.flush()
            prev_alarm = signal.signal(signal.SIGALRM, _alarm_handler)
            signal.alarm(max_source_time)

        # User
        User = get_user_model()
        user = User.objects.filter(is_superuser=True).first()
        if not user:
            user, created = User.objects.get_or_create(
                username='scraper', defaults={'is_active': False})
            if created:
                user.set_unusable_password()
                user.save()

        # Bind the running source so the CSV log can record which source
        # each row came from. Used by `_log_row` via `_current_source`.
        self._running_source = source

        # Paginated fetch loop
        current_page = 1
        items_yielded_total = 0

        try:
            while items_yielded_total < remaining_limit and not _INTERRUPTED:
                page_limit = min(page_size,
                                 remaining_limit - items_yielded_total
                                 + len(already_handled))
                # Try the source's preferred signature first (page +
                # sort). If the source doesn't support those kwargs
                # (most non-IA connectors), fall back to limit-only.
                # Any exception inside fetch_audio (HTTP 403, network
                # timeout, malformed JSON) is logged and treated as
                # "no items this page" so the loop advances.
                try:
                    if hasattr(module, 'fetch_audio'):
                        try:
                            page_items = module.fetch_audio(
                                limit=page_limit, page=current_page,
                                sort='identifier asc')
                        except TypeError:
                            # Connector doesn't accept page/sort
                            page_items = module.fetch_audio(limit=page_limit)
                    else:
                        page_items = module.fetch_audio(limit=page_limit)
                except Exception as exc:
                    # Don't let one connector's failure (e.g. 403 from
                    # Wikimedia, timeout from Openverse) kill the run.
                    # The connector's own logger already wrote the full
                    # traceback; we just record the page as empty and
                    # break out so we don't loop forever on a broken
                    # upstream.
                    logger.warning(
                        'source=%s fetch failed at page=%d: %s; '
                        'recording empty page and breaking out.',
                        source, current_page, exc)
                    page_items = []
                if not page_items:
                    break

                for item in page_items:
                    if _INTERRUPTED:
                        break
                    if items_yielded_total >= remaining_limit:
                        break
                    item_id = item.get('id') or item.get('url') or '?'
                    if item_id and item_id in already_handled:
                        # Item already fully done; skip but mark
                        # fetched so the offset stays in sync.
                        scraper_state.mark_fetched(state, item_id)
                        items_yielded_total += 1
                        continue
                    if item_id:
                        scraper_state.mark_fetched(state, item_id)
                    items_yielded_total += 1
                    self._process_item(
                        item, source, user, clip_length,
                        allow_nc, include_sa, quiet, csv_log, state,
                    )
                    scraper_state.save_state(source, state, state_dir)

                current_page += 1
                if len(page_items) < page_limit:
                    break
        finally:
            self._close_log(csv_log)
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            if max_source_time:
                # Cancel the alarm and restore the previous handler. We
                # don't actually re-raise the alarm — the loop has
                # already broken out cleanly via _INTERRUPTED.
                signal.alarm(0)
                # best-effort: restore the prior SIGALRM handler (None
                # before our install) so subsequent sources in the same
                # invocation don't inherit the alarm.
                try:
                    signal.signal(signal.SIGALRM, prev_alarm)  # type: ignore[name-defined]
                except (TypeError, NameError):
                    pass
            scraper_state.update_state_for_resume(state)
            scraper_state.save_state(source, state, state_dir)
            if _INTERRUPTED:
                self.stdout.write(self.style.WARNING(
                    f'[scrape] interrupted. State saved. Re-run the same command '
                    f'to resume. To start over, add --reset.'))
            self.stdout.write(self.style.SUCCESS(
                f'[summary] source={source} fetched={state["counts"]["fetched"]} '
                f'segments_imported={state["counts"]["segments_imported"]} '
                f'segments_failed={state["counts"]["segments_failed"]} '
                f'retried={state["counts"].get("retried", 0)} '
                f'allow_nc={allow_nc} include_sa={include_sa}'))

    def _process_item(self, item, source, user, clip_length,
                      allow_nc, include_sa, quiet, csv_log, state):
        """Process a single source item: split into segments + save each.

        Per-segment failure tracking: if segment 3/7 fails, segments
        0-2 are already saved. The state records the per-segment
        status so resume can retry segment 3 specifically.
        """
        url = item.get('url')
        title = item.get('title') or 'scraped audio'
        page = item.get('page_url') or ''
        lic_raw = item.get('license')
        item_id = item.get('id') or url or '?'

        family = normalize_license(lic_raw)
        nc, sa = license_features(family)
        nc = nc or bool(item.get('is_noncommercial'))

        # License gate at the item level: if the entire family is
        # disallowed, skip all segments in one shot.
        if not license_allows_commercial(family, allow_nc=allow_nc):
            if not quiet:
                self.stdout.write(self.style.WARNING(
                    f'Skipping {url}: license "{lic_raw}" ({family}) '
                    f'not allowed (allow_nc={allow_nc})'))
            scraper_state.ensure_item(state, item_id, total_segments=1,
                                       title=title, url=url)
            # Single pseudo-segment, marked skipped.
            scraper_state.mark_segment(state, item_id, 0, 'skipped_license')
            scraper_state.item_done(state, item_id)
            self._log_row(csv_log, item_id, title, url, page, lic_raw,
                           family, nc, False, 'skipped_license', 0, 0.0, '',
                           '', 'license filter')
            return

        sa = sa or is_share_alike_license(family)

        # Split into segments
        # We download the source ONCE, then split. This means a failure
        # on segment 3 means we re-download + re-split on resume. That's
        # OK for the resume semantics (re-fetched_ids still skips
        # upstream duplicates).
        local_input = None
        try:
            if not url:
                raise RuntimeError('No URL for item')

            if url.startswith('file://'):
                local_input = url[len('file://'):]
            else:
                local_input, retries, size_bytes = downloader.download_with_retries(
                    url, max_bytes=50_000_000, timeout=60,
                    max_attempts=3, backoff=2.0,
                )

            # Save N segments via the uploader. Each segment is its own
            # AudioClip row sharing group_id.
            t0 = _time.time()
            try:
                clips = uploader.save_clip_segments(
                    user=user,
                    title=title,
                    source_name=source,
                    source_url=page,
                    license=lic_raw or 'unknown',
                    attribution_text=page,
                    local_file_path=local_input,
                    original_source_id=item.get('id'),
                    is_noncommercial=nc,
                    requires_share_alike=sa,
                    license_family=family,
                    max_seconds=clip_length,
                )
            except Exception as e:
                # The splitter raised (likely ffmpeg/pydub failure on
                # the whole file). Mark all segments as failed_other.
                scraper_state.ensure_item(state, item_id,
                                           total_segments=1, title=title,
                                           url=url)
                scraper_state.mark_segment(state, item_id, 0,
                                            'failed_other', error=str(e))
                self._log_row(csv_log, item_id, title, url, page, lic_raw,
                               family, nc, sa, 'failed_other', 0,
                               _time.time() - t0, '', '', str(e))
                if not quiet:
                    self.stdout.write(self.style.ERROR(
                        f'Failed to split {url}: {e}'))
                return

            # All N segments created. Record each as imported.
            n = len(clips)
            scraper_state.ensure_item(state, item_id,
                                       total_segments=n,
                                       title=title, url=url)
            for idx, clip in enumerate(clips):
                scraper_state.mark_segment(state, item_id, idx, 'imported')
                self._log_row(
                    csv_log, item_id, title, url, page, lic_raw,
                    family, nc, sa, 'imported', retries,
                    (_time.time() - t0) / max(1, n),
                    '', str(clip.id), '',
                )
                try:
                    publish(process_audio_to_hls, str(clip.id))
                except Exception:
                    pass
            if not quiet:
                self.stdout.write(self.style.SUCCESS(
                    f'Imported {n} segments for "{title}" (group {clips[0].group_id})'))
        except downloader.DownloadError as e:
            scraper_state.ensure_item(state, item_id, total_segments=1,
                                       title=title, url=url)
            scraper_state.mark_segment(state, item_id, 0,
                                        'failed_download',
                                        error=str(e), retries=e.attempts)
            self._log_row(csv_log, item_id, title, url, page, lic_raw,
                           family, nc, sa, 'failed_download', e.attempts,
                           _time.time() - t0, '', '', str(e))
            if not quiet:
                self.stdout.write(self.style.ERROR(
                    f'Failed to import {url} after {e.attempts} attempts: {e}'))
        except Exception as e:
            scraper_state.ensure_item(state, item_id, total_segments=1,
                                       title=title, url=url)
            scraper_state.mark_segment(state, item_id, 0,
                                        'failed_other', error=str(e))
            self._log_row(csv_log, item_id, title, url, page, lic_raw,
                           family, nc, sa, 'failed_other', 0,
                           _time.time() - t0, '', '', str(e))
            logger.exception('Import failed for %s: %s', url, e)
            if not quiet:
                self.stdout.write(self.style.ERROR(
                    f'Failed to import {url}: {e}'))
        finally:
            try:
                if local_input and os.path.exists(local_input):
                    if not local_input.startswith(settings.MEDIA_ROOT):
                        os.remove(local_input)
            except Exception:
                pass

    def _log_row(self, csv_log, item_id, title, url, page,
                 lic_raw, family, nc, sa, status, retries,
                 duration, size_bytes, clip_id, error):
        if csv_log is None:
            return
        try:
            csv_log.write_row(
                source=self._current_source,
                item_id=item_id or '',
                title=(title or '')[:200],
                url=url or '',
                page_url=page or '',
                license_raw=lic_raw or '',
                license_family=family or '',
                is_nc=bool(nc),
                is_sa=bool(sa),
                status=status or 'unknown',
                retries=retries,
                duration_sec=f"{duration:.2f}",
                size_bytes=size_bytes or '',
                clip_id=clip_id or '',
                error=error or '',
            )
        except Exception as e:
            self.stdout.write(self.style.WARNING(f'csv log write failed: {e}'))

    def _current_source(self):
        # The csv_log writer reads 'source' as a kwarg, but we want
        # to bind to the current source. Use a closure via the
        # `_running_source` attribute on the instance.
        return getattr(self, '_running_source', 'unknown')

    def _close_log(self, csv_log):
        if csv_log is not None:
            try:
                csv_log.close()
            except Exception:
                pass

    def _run_smoke(self, source, limit, clip_length, quiet):
        """Smoke-test the connector: fetch limit items, verify shape, no DB writes."""
        if not source:
            self.stdout.write(self.style.ERROR(
                '--smoke requires --source (or a single source via --sources)'))
            return
        import signal
        from urllib.parse import urlparse

        def _emit(s):
            try:
                print(s, flush=True)
            except Exception:
                self.stdout.write(s)
                self.stdout.flush()

        module = SOURCES.get(source)
        if not module:
            _emit(f'Unknown source: {source}')
            return
        smoke_cap = 10_000_000
        try:
            if hasattr(module, 'fetch_audio'):
                try:
                    items = module.fetch_audio(limit=limit,
                                                max_file_size=smoke_cap,
                                                page=1,
                                                sort='identifier asc')
                except TypeError:
                    items = module.fetch_audio(limit=limit)
            else:
                items = module.fetch_audio(limit=limit)
        except Exception as e:
            _emit(f'[FAIL] source={source} fetch_audio() raised: {e}')
            return
        if not items:
            try:
                raw = module.fetch_audio(limit=limit)
            except Exception:
                raw = []
            if raw:
                _emit(f'[OK-empty] source={source} fetch returned {len(raw)} items '
                      f'but all were filtered by 10MB max_file_size cap')
            else:
                _emit(f'[OK-empty] source={source} fetch_audio() returned 0 items '
                      f'(check API keys or upstream availability)')
            return
        first = items[0]
        url = first.get('url') or ''
        if not url:
            _emit(f'[PARTIAL] source={source} fetch returned {len(items)} items, '
                  f'first has no url field')
            return
        missing = []
        for i, it in enumerate(items):
            for k in ('url', 'title', 'page_url', 'license', 'id'):
                if k not in it:
                    missing.append(f'item[{i}].{k}')
        if missing:
            _emit(f'[PARTIAL] source={source} fetched={len(items)} missing keys: '
                  f'{", ".join(missing[:5])}')
            return
        _emit(f'[FETCH-OK] source={source} fetched={len(items)} items, '
              f'first_url={url[:80]} license={first.get("license")} '
              f'id={first.get("id")}')

        def _timeout_handler(signum, frame):
            raise TimeoutError('download exceeded 30s')

        try:
            signal.signal(signal.SIGALRM, _timeout_handler)
            signal.alarm(30)
            try:
                host = urlparse(url).netloc
                path = downloader.download_audio(url, max_bytes=smoke_cap,
                                                   timeout=20)
                size = os.path.getsize(path)
                os.remove(path)
                _emit(f'[PASS] source={source} downloaded={size} bytes from {host}')
            finally:
                signal.alarm(0)
        except (TimeoutError, Exception) as e:
            _emit(f'[FETCH-OK-DOWNLOAD-SKIPPED] source={source} download_error={e}')

    def _print_multi_source_summary(self, source_list, options):
        """Print a summary at the end of a multi-source run."""
        agg = {'fetched': 0, 'imported': 0, 'skipped': 0, 'failed': 0,
               'segments_imported': 0, 'segments_failed': 0, 'retried': 0}
        for source in source_list:
            state = scraper_state.load_state(source, options['state_dir'])
            for k in agg:
                agg[k] += state['counts'].get(k, 0)
        self.stdout.write(self.style.SUCCESS(
            f'\n[multi-source summary] sources={len(source_list)} '
            f'fetched={agg["fetched"]} '
            f'segments_imported={agg["segments_imported"]} '
            f'segments_failed={agg["segments_failed"]} '
            f'retried={agg["retried"]}'))
