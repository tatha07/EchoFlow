"""Tests for per-segment state tracking in the new state.py schema."""
import os
import shutil
import tempfile
import unittest

from ai_ml.scrapers import state as scraper_state


class PerSegmentStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='state_seg_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_empty_state_has_per_segment_skeleton(self):
        s = scraper_state._empty_state('test')
        self.assertIn('items', s)
        self.assertIn('segments_imported', s['counts'])
        self.assertIn('segments_failed', s['counts'])
        self.assertEqual(s['counts']['segments_imported'], 0)
        self.assertEqual(s['items'], {})

    def test_load_legacy_state_promotes_to_per_segment(self):
        """Old state files (no 'items' key) should be promoted on load."""
        legacy_path = scraper_state.state_path('legacy', state_dir=self.tmp)
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text(
            '{"source": "legacy", "params": {}, '
            '"fetch_offset": 0, "fetched_ids": [], '
            '"counts": {"fetched": 5, "imported": 3, "skipped": 1, "failed": 1, "retried": 0}}'
        )
        s = scraper_state.load_state('legacy', state_dir=self.tmp)
        self.assertIn('items', s)
        self.assertEqual(s['items'], {})
        self.assertEqual(s['counts']['segments_imported'], 0)
        self.assertEqual(s['counts']['fetched'], 5)

    def test_ensure_item_creates_record(self):
        s = scraper_state._empty_state('test')
        item = scraper_state.ensure_item(s, 'item-1', total_segments=5,
                                          title='My Item', url='http://x')
        self.assertEqual(item['title'], 'My Item')
        self.assertEqual(item['url'], 'http://x')
        self.assertEqual(item['total_segments'], 5)
        self.assertIn('fetched_at', item)

    def test_mark_segment_increments_counts(self):
        s = scraper_state._empty_state('test')
        scraper_state.mark_segment(s, 'item-1', 0, 'imported')
        scraper_state.mark_segment(s, 'item-1', 1, 'imported')
        scraper_state.mark_segment(s, 'item-1', 2, 'failed_download',
                                    error='SSL', retries=3)
        self.assertEqual(s['counts']['segments_imported'], 2)
        self.assertEqual(s['counts']['segments_failed'], 1)
        self.assertEqual(s['counts']['retried'], 3)
        # Item's segment_status map should reflect this
        item = s['items']['item-1']
        self.assertEqual(item['segment_status']['0'], 'imported')
        self.assertEqual(item['segment_status']['2'], 'failed_download')
        self.assertIn('last_error', item)

    def test_item_done_true_when_all_segments_decided(self):
        s = scraper_state._empty_state('test')
        scraper_state.ensure_item(s, 'item-1', total_segments=3)
        for i in range(3):
            scraper_state.mark_segment(s, 'item-1', i, 'imported')
        self.assertTrue(scraper_state.item_done(s, 'item-1'))
        self.assertEqual(s['counts']['imported'], 1)

    def test_item_done_false_when_pending_segments(self):
        s = scraper_state._empty_state('test')
        scraper_state.ensure_item(s, 'item-1', total_segments=3)
        scraper_state.mark_segment(s, 'item-1', 0, 'imported')
        # 1, 2 still pending
        self.assertFalse(scraper_state.item_done(s, 'item-1'))

    def test_item_segments_to_process_resets_failures_for_retry(self):
        s = scraper_state._empty_state('test')
        scraper_state.ensure_item(s, 'item-1', total_segments=3)
        scraper_state.mark_segment(s, 'item-1', 0, 'imported')
        scraper_state.mark_segment(s, 'item-1', 1, 'imported')
        scraper_state.mark_segment(s, 'item-1', 2, 'failed_download',
                                    error='SSL')
        # 2 is failed → should be in the "to process" list (so resume retries it)
        to_do = scraper_state.item_segments_to_process(s, 'item-1')
        self.assertEqual(to_do, [2])

    def test_items_already_handled_only_counts_complete(self):
        s = scraper_state._empty_state('test')
        # item-A: complete
        scraper_state.ensure_item(s, 'A', total_segments=2)
        scraper_state.mark_segment(s, 'A', 0, 'imported')
        scraper_state.mark_segment(s, 'A', 1, 'imported')
        # item-B: partial
        scraper_state.ensure_item(s, 'B', total_segments=3)
        scraper_state.mark_segment(s, 'B', 0, 'imported')
        # item-C: failed mid-way
        scraper_state.ensure_item(s, 'C', total_segments=2)
        scraper_state.mark_segment(s, 'C', 0, 'imported')
        scraper_state.mark_segment(s, 'C', 1, 'failed_download', error='x')
        handled = scraper_state.items_already_handled(s)
        # Only A has all segments decided
        self.assertEqual(handled, {'A'})

    def test_mark_segment_skipped_license(self):
        s = scraper_state._empty_state('test')
        scraper_state.ensure_item(s, 'item-1', total_segments=1)
        scraper_state.mark_segment(s, 'item-1', 0, 'skipped_license')
        # Skipped doesn't count as imported or failed
        self.assertEqual(s['counts']['segments_imported'], 0)
        self.assertEqual(s['counts']['segments_failed'], 0)
        self.assertEqual(s['items']['item-1']['segment_status']['0'],
                         'skipped_license')
