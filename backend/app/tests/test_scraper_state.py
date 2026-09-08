"""Tests for the resumable-scraper state file + CSV log."""
import csv
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_ml.scrapers import state as scraper_state
from ai_ml.scrapers import log as scraper_log


class StateFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='scraper_test_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_empty_state_load(self):
        s = scraper_state.load_state('nonexistent', state_dir=self.tmp)
        self.assertEqual(s['source'], 'nonexistent')
        self.assertEqual(s['counts']['imported'], 0)
        self.assertEqual(s['fetched_ids'], [])

    def test_save_and_load_roundtrip(self):
        s = scraper_state._empty_state('librivox')
        s['counts']['imported'] = 5
        s['fetched_ids'] = ['1', '2', '3']
        s['processed_ids'] = ['1', '2', '3']
        scraper_state.save_state('librivox', s, state_dir=self.tmp)
        loaded = scraper_state.load_state('librivox', state_dir=self.tmp)
        self.assertEqual(loaded['counts']['imported'], 5)
        self.assertEqual(loaded['fetched_ids'], ['1', '2', '3'])
        self.assertEqual(loaded['processed_ids'], ['1', '2', '3'])

    def test_atomic_write_no_partial_on_overwrite(self):
        # First save
        s = scraper_state._empty_state('a')
        s['counts']['imported'] = 1
        scraper_state.save_state('a', s, state_dir=self.tmp)
        # Second save
        s['counts']['imported'] = 99
        scraper_state.save_state('a', s, state_dir=self.tmp)
        loaded = scraper_state.load_state('a', state_dir=self.tmp)
        self.assertEqual(loaded['counts']['imported'], 99)

    def test_reset_removes_file(self):
        s = scraper_state._empty_state('a')
        scraper_state.save_state('a', s, state_dir=self.tmp)
        self.assertTrue(scraper_state.reset_state('a', state_dir=self.tmp))
        self.assertFalse(scraper_state.reset_state('a', state_dir=self.tmp))  # 2nd call: no-op

    def test_corrupt_file_treated_as_fresh(self):
        path = scraper_state.state_path('a', state_dir=self.tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('this is not json{{{')
        s = scraper_state.load_state('a', state_dir=self.tmp)
        self.assertEqual(s['counts']['imported'], 0)

    def test_params_match(self):
        # Empty state (first run or post-reset) always matches.
        self.assertTrue(scraper_state.params_match({}, {'a': 1}))
        # Saved state has every key the run cares about, with same value.
        self.assertTrue(scraper_state.params_match(
            {'a': 1, 'b': 2, 'c': 3}, {'a': 1, 'b': 2}))  # extra keys OK
        # Saved state has different value for a key the run cares about.
        self.assertFalse(scraper_state.params_match(
            {'a': 1}, {'a': 2}))
        # Saved state is missing a key the run cares about.
        self.assertFalse(scraper_state.params_match(
            {'a': 1}, {'a': 1, 'b': 2}))
        # None values in run_params are skipped.
        self.assertTrue(scraper_state.params_match(
            {'a': 1}, {'a': 1, 'b': None}))

    def test_mark_functions(self):
        s = scraper_state._empty_state('a')
        scraper_state.mark_fetched(s, 'id1')
        scraper_state.mark_imported(s, 'id1')
        scraper_state.mark_skipped(s, 'id2', 'license:CC-BY-NC')
        scraper_state.mark_failed(s, 'id3', 'SSL EOF', retries=3)
        self.assertEqual(s['counts']['fetched'], 1)
        self.assertEqual(s['counts']['imported'], 1)
        self.assertEqual(s['counts']['skipped'], 1)
        self.assertEqual(s['counts']['failed'], 1)
        self.assertEqual(s['counts']['retried'], 3)
        self.assertIn('id1', s['processed_ids'])
        self.assertEqual(s['skipped'][0]['reason'], 'license:CC-BY-NC')
        self.assertEqual(s['failed'][0]['retries'], 3)

    def test_items_already_handled(self):
        s = scraper_state._empty_state('a')
        scraper_state.mark_imported(s, 'x')
        scraper_state.mark_skipped(s, 'y', 'license')
        # Failed items are NOT considered "fully handled" — the
        # management command will retry them on the next run.
        scraper_state.mark_failed(s, 'z', 'ssl')
        handled = scraper_state.items_already_handled(s)
        self.assertEqual(handled, {'x', 'y'})

    def test_fetched_ids_capped(self):
        s = scraper_state._empty_state('a')
        for i in range(scraper_state._FETCHED_IDS_CAP + 100):
            scraper_state.mark_fetched(s, f'id{i}')
        self.assertEqual(len(s['fetched_ids']),
                         scraper_state._FETCHED_IDS_CAP)
        # The most recent IDs are kept
        self.assertIn(f'id{scraper_state._FETCHED_IDS_CAP + 99}',
                      s['fetched_ids'])


class CsvLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='log_test_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_open_log_writes_header(self):
        path = Path(self.tmp) / 'test.csv'
        with scraper_log.ScraperCsvLog(path) as log:
            log.write_row(source='x', item_id='1', status='imported')
        with open(path, 'r') as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['source'], 'x')
        self.assertEqual(rows[0]['item_id'], '1')
        self.assertEqual(rows[0]['status'], 'imported')
        # auto-filled timestamp
        self.assertTrue(rows[0]['timestamp'])

    def test_boolean_stringification(self):
        path = Path(self.tmp) / 'test.csv'
        with scraper_log.ScraperCsvLog(path) as log:
            log.write_row(source='x', item_id='1', is_nc=True, is_sa=False)
        with open(path, 'r') as f:
            content = f.read()
        self.assertIn('true', content)
        self.assertIn('false', content)

    def test_open_log_with_relative_path(self):
        # Relative --log=foo.csv should be resolved under log_dir.
        log = scraper_log.open_log('source', log_path='rel.csv',
                                    log_dir=self.tmp)
        try:
            self.assertEqual(log.path.parent, Path(self.tmp))
            self.assertEqual(log.path.name, 'rel.csv')
        finally:
            log.close()

    def test_default_log_path_uses_timestamp(self):
        p1 = scraper_log.default_log_path('source', log_dir=self.tmp)
        self.assertIn('source-', p1.name)
        self.assertTrue(p1.name.endswith('.csv'))
