"""Tests for the retry-aware downloader."""
import unittest
from unittest.mock import patch, MagicMock

from ai_ml.scrapers import downloader


class DownloadRetryTests(unittest.TestCase):
    def test_success_first_try(self):
        with patch.object(downloader, '_download_once',
                           return_value=('/tmp/x', 1000)) as m:
            path, attempts, size = downloader.download_with_retries(
                'https://example.com/x', max_bytes=50_000_000)
        self.assertEqual(path, '/tmp/x')
        self.assertEqual(attempts, 1)
        self.assertEqual(size, 1000)
        self.assertEqual(m.call_count, 1)

    def test_success_after_retries(self):
        # First two fail, third succeeds.
        side_effects = [RuntimeError('boom'), RuntimeError('ssl'), ('/tmp/y', 500)]
        with patch.object(downloader, '_download_once', side_effect=side_effects) as m, \
             patch.object(downloader.time, 'sleep') as sleep:
            path, attempts, size = downloader.download_with_retries(
                'https://example.com/y', max_bytes=50_000_000,
                max_attempts=5, backoff=2.0)
        self.assertEqual(path, '/tmp/y')
        self.assertEqual(attempts, 3)
        self.assertEqual(size, 500)
        self.assertEqual(m.call_count, 3)
        # Sleep was called twice (between attempts 1-2 and 2-3) with
        # exponential backoff.
        self.assertEqual(sleep.call_count, 2)
        sleep.assert_any_call(2.0)
        sleep.assert_any_call(4.0)

    def test_all_attempts_fail(self):
        side_effects = [RuntimeError('a'), RuntimeError('b'), RuntimeError('c')]
        with patch.object(downloader, '_download_once', side_effect=side_effects) as m, \
             patch.object(downloader.time, 'sleep'):
            with self.assertRaises(downloader.DownloadError) as ctx:
                downloader.download_with_retries(
                    'https://example.com/z', max_bytes=50_000_000,
                    max_attempts=3, backoff=1.0)
        self.assertEqual(ctx.exception.attempts, 3)
        self.assertEqual(m.call_count, 3)
        # Last exception preserved
        self.assertIn('c', str(ctx.exception.last_exception))
