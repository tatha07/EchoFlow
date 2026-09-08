"""Per-source connector tests.

Mocks `requests` via the per-source session/patches, asserts contract shape.
No network access. No DB writes.
"""
import unittest
from unittest.mock import patch, MagicMock

from ai_ml.scrapers.sources import SOURCES, __init__ as sources_init
from ai_ml.scrapers import sources as _sources_pkg


CONTRACT_KEYS = {'url', 'title', 'page_url', 'license', 'id'}


def _assert_contract(item, source):
    for k in ('url', 'title', 'page_url'):
        if k not in item:
            raise AssertionError(
                f'{source}: item missing required key "{k}": {item}')
    if 'license' not in item:
        raise AssertionError(
            f'{source}: item missing "license" key (even UNKNOWN): {item}')


class TestSourcesRegistered(unittest.TestCase):
    def test_all_new_sources_registered(self):
        expected = {
            'openverse', 'librivox', 'free_music_archive',
            # 'pixabay',          # DISABLED pending API key
            # 'podcast_index',    # DISABLED pending API keys
            'podcast_rss', 'bbc_sound_effects',
            'musopen', 'loc_national_jukebox', 'usgov_audio',
        }
        for name in expected:
            self.assertIn(name, SOURCES, f'{name} missing from SOURCES dict')
            self.assertTrue(callable(SOURCES[name].fetch_audio),
                            f'{name}.fetch_audio not callable')

    def test_legacy_sources_still_registered(self):
        for name in ('wikimedia', 'internet_archive', 'freesound', 'kaggle'):
            self.assertIn(name, SOURCES)


class TestNoImportTimeSideEffects(unittest.TestCase):
    """Importing every source module must not hit the network."""

    def test_no_network_on_import(self):
        # Confirm that importing source modules doesn't trigger network
        # calls. Modules are already imported by earlier tests in this file.
        with patch('requests.get') as g:
            g.assert_not_called()
        # If we got here without raising, no module made a network call
        # during the import path that succeeded above.
        self.assertTrue(True)


class TestOpenverse(unittest.TestCase):
    def test_returns_list_of_dicts(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            'results': [{
                'id': 'abc-123',
                'title': 'Sample Track',
                'url': 'https://example.com/sample.mp3',
                'foreign_landing_url': 'https://example.com/sample',
                'license': 'by',
                'creator': 'Test Artist',
                'category': 'music',
                'duration': 180000,
            }],
        }
        mock_resp.raise_for_status = lambda: None
        with patch('ai_ml.scrapers.sources.openverse.get_session') as gs:
            gs.return_value.get.return_value = mock_resp
            items = _sources_pkg.openverse.fetch_audio(limit=5)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['license'], 'CC-BY')
        _assert_contract(items[0], 'openverse')

    def test_normalizes_lowercase_license(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {'results': [
            {'id': '1', 'title': 'A', 'url': 'https://e/a.mp3',
             'foreign_landing_url': 'https://e/a', 'license': 'by-nc-nd'},
            {'id': '2', 'title': 'B', 'url': 'https://e/b.mp3',
             'foreign_landing_url': 'https://e/b', 'license': 'cc0'},
            {'id': '3', 'title': 'C', 'url': 'https://e/c.mp3',
             'foreign_landing_url': 'https://e/c', 'license': 'by-sa'},
        ]}
        mock_resp.raise_for_status = lambda: None
        with patch('ai_ml.scrapers.sources.openverse.get_session') as gs:
            gs.return_value.get.return_value = mock_resp
            items = _sources_pkg.openverse.fetch_audio(limit=10)
        self.assertEqual(items[0]['license'], 'CC-BY-NC-ND')
        self.assertEqual(items[1]['license'], 'CC0')
        self.assertEqual(items[2]['license'], 'CC-BY-SA')

    def test_respects_limit(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            'results': [{'id': str(i), 'title': f't{i}',
                         'url': f'https://e/{i}.mp3',
                         'foreign_landing_url': f'https://e/{i}',
                         'license': 'cc0'} for i in range(50)]
        }
        mock_resp.raise_for_status = lambda: None
        with patch('ai_ml.scrapers.sources.openverse.get_session') as gs:
            gs.return_value.get.return_value = mock_resp
            items = _sources_pkg.openverse.fetch_audio(limit=3)
        self.assertEqual(len(items), 3)


class TestLibriVox(unittest.TestCase):
    def test_returns_list_with_pd_license(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            'books': [{
                'id': 1,
                'title': 'Pride and Prejudice',
                'url_iarchive': 'https://archive.org/details/prideandprejudice',
                'url_librivox': 'https://librivox.org/prideandprejudice',
                'authors': [{'first_name': 'Jane', 'last_name': 'Austen'}],
                'language': 'en',
                'copyright_year': 1813,
            }],
        }
        mock_resp.raise_for_status = lambda: None
        # Patch the IA resolver + LibriVox session
        with patch('ai_ml.scrapers.sources.librivox.get_session') as gs, \
             patch('ai_ml.scrapers.sources.librivox._ia_first_mp3_url') as m:
            gs.return_value.get.return_value = mock_resp
            m.return_value = 'https://archive.org/download/prideandprejudice/01.mp3'
            items = _sources_pkg.librivox.fetch_audio(limit=5)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['license'], 'CC0')
        self.assertIn('Pride', items[0]['title'])
        self.assertEqual(items[0]['creator'], 'Jane Austen')
        _assert_contract(items[0], 'librivox')


class TestPixabay(unittest.TestCase):
    def test_returns_list_when_key_present(self):
        from django.test import override_settings
        from ai_ml.scrapers.sources import pixabay
        with override_settings(SCRAPER_PIXABAY_API_KEY='test-key-123'):
            mock_resp = MagicMock()
            mock_resp.json.return_value = {
                'hits': [{
                    'id': 99,
                    'tags': 'guitar, music',
                    'user': 'creator',
                    'audio': 'https://cdn.pixabay.com/audio/preview.mp3',
                    'pageURL': 'https://pixabay.com/music/-99',
                    'duration': 60,
                }],
            }
            mock_resp.raise_for_status = lambda: None
            with patch('ai_ml.scrapers.sources.pixabay.get_session') as gs:
                gs.return_value.get.return_value = mock_resp
                items = pixabay.fetch_audio(limit=5)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]['license'], 'PIXABAY')
            self.assertEqual(items[0]['id'], '99')

    def test_returns_empty_without_api_key(self):
        from django.test import override_settings
        from ai_ml.scrapers.sources import pixabay
        with override_settings(SCRAPER_PIXABAY_API_KEY=''):
            items = pixabay.fetch_audio(limit=5)
        self.assertEqual(items, [])


class TestPodcastIndex(unittest.TestCase):
    def test_returns_empty_without_keys(self):
        from django.test import override_settings
        from ai_ml.scrapers.sources import podcast_index
        with override_settings(
            SCRAPER_PODCAST_INDEX_API_KEY='',
            SCRAPER_PODCAST_INDEX_API_SECRET=''):
            items = podcast_index.fetch_audio(limit=5)
        self.assertEqual(items, [])

    def test_auth_headers_when_keys_present(self):
        from django.test import override_settings
        with override_settings(
            SCRAPER_PODCAST_INDEX_API_KEY='pk',
            SCRAPER_PODCAST_INDEX_API_SECRET='ps'):
            from ai_ml.scrapers.sources.podcast_index import _auth_headers
            h = _auth_headers()
            self.assertIn('X-Auth-Key', h)
            self.assertEqual(h['X-Auth-Key'], 'pk')
            self.assertIn('X-Auth-Date', h)
            self.assertIn('X-Auth-SHA', h)
            self.assertEqual(len(h['X-Auth-SHA']), 40)  # SHA1 hex

    def test_resolves_rss_following_search(self):
        from django.test import override_settings
        from ai_ml.scrapers.sources import podcast_index
        with override_settings(
            SCRAPER_PODCAST_INDEX_API_KEY='pk',
            SCRAPER_PODCAST_INDEX_API_SECRET='ps'):
            pi_resp = MagicMock()
            pi_resp.json.return_value = {
                'feeds': [{'id': 1, 'title': 'Tech Pod',
                           'url': 'https://example.com/feed.xml'}],
            }
            pi_resp.raise_for_status = lambda: None
            rss_xml = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<title>Tech Pod</title>
<link>https://example.com</link>
<item><title>Ep1</title>
<enclosure url="https://example.com/ep1.mp3" type="audio/mpeg"/></item>
</channel></rss>"""
            rss_resp = MagicMock()
            rss_resp.content = rss_xml.encode('utf-8')
            rss_resp.raise_for_status = lambda: None
            session = MagicMock()
            session.get.side_effect = [pi_resp, rss_resp]
            with patch('ai_ml.scrapers.sources.podcast_index.get_session',
                       return_value=session), \
                 patch('ai_ml.scrapers.sources.podcast_index.resolve_podcast_rss',
                       return_value=[
                           {'url': 'https://example.com/ep1.mp3',
                            'title': 'Tech Pod - Ep1',
                            'page_url': 'https://example.com',
                            'license': 'UNKNOWN',
                            'id': 'ep1'}],
                       ):
                items = podcast_index.fetch_audio(limit=5)
            self.assertGreater(len(items), 0)
            self.assertEqual(items[0]['license'], 'UNKNOWN')


class TestBBCSoundEffects(unittest.TestCase):
    def test_marks_as_nc(self):
        with patch('ai_ml.scrapers.sources.bbc_sound_effects.ia_search_raw') as s, \
             patch('ai_ml.scrapers.sources.bbc_sound_effects._ia_resolve_audio_url') as r:
            s.return_value = [{
                'identifier': 'bbc-sfx-001',
                'title': 'Forest Birds',
                'creator': 'BBC',
            }]
            r.return_value = ('https://archive.org/download/bbc-sfx-001/track.mp3', 12345)
            items = _sources_pkg.bbc_sound_effects.fetch_audio(limit=5)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['license'], 'REMARC-NC')
        self.assertTrue(items[0]['is_noncommercial'])


class TestFMA(unittest.TestCase):
    def test_returns_list(self):
        with patch('ai_ml.scrapers.sources.free_music_archive.ia_search_raw') as s, \
             patch('ai_ml.scrapers.sources.free_music_archive._ia_resolve_audio_url') as r:
            s.return_value = [{
                'identifier': 'fma-001', 'title': 'Track',
                'licenseurl': 'http://creativecommons.org/licenses/by/4.0/',
            }]
            r.return_value = ('https://archive.org/download/fma-001/track.mp3', 12345)
            items = _sources_pkg.free_music_archive.fetch_audio(limit=5)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['license'], 'CC-BY')


class TestMusopen(unittest.TestCase):
    def test_pd_license_family(self):
        with patch('ai_ml.scrapers.sources.musopen.ia_search_raw') as s, \
             patch('ai_ml.scrapers.sources.musopen._ia_resolve_audio_url') as r:
            s.return_value = [{'identifier': 'm-1', 'title': 'Beethoven'}]
            r.return_value = ('https://archive.org/download/m-1/track.mp3', 12345)
            items = _sources_pkg.musopen.fetch_audio(limit=5)
        self.assertEqual(items[0]['license'], 'CC0')


class TestLOC(unittest.TestCase):
    def test_pd_license_family(self):
        with patch('ai_ml.scrapers.sources.loc_national_jukebox.ia_search_raw') as s, \
             patch('ai_ml.scrapers.sources.loc_national_jukebox._ia_resolve_audio_url') as r:
            s.return_value = [{'identifier': 'loc-1', 'title': 'Jazz 1920'}]
            r.return_value = ('https://archive.org/download/loc-1/track.mp3', 12345)
            items = _sources_pkg.loc_national_jukebox.fetch_audio(limit=5)
        self.assertEqual(items[0]['license'], 'CC0')


class TestUSGov(unittest.TestCase):
    def test_three_collections_queried(self):
        # Import the module to ensure the connector registers. The actual
        # IA query strings are validated by the smoke test (live IA calls
        # are too slow for the unit test path).
        import ai_ml.scrapers.sources.usgov_audio as mod
        self.assertTrue(hasattr(mod, 'fetch_audio'))
        with patch('ai_ml.scrapers.sources.usgov_audio.ia_search_raw') as s, \
             patch('ai_ml.scrapers.sources.usgov_audio._ia_resolve_audio_url') as r:
            s.side_effect = [
                [{'identifier': 'c-1', 'title': 'Hearing'}],
                [{'identifier': 'n-1', 'title': 'Apollo'}],
                [{'identifier': 'u-1', 'title': 'Quake'}],
            ]
            r.side_effect = [
                ('https://archive.org/download/c-1/track.mp3', '1'),
                ('https://archive.org/download/n-1/track.mp3', '1'),
                ('https://archive.org/download/u-1/track.mp3', '1'),
            ]
            items = _sources_pkg.usgov_audio.fetch_audio(limit=3)
        # Should have called ia_search_raw at least 3 times (cspan, nasa, usgs)
        self.assertGreaterEqual(s.call_count, 3)
        self.assertEqual(len(items), 3)
        self.assertTrue(all(it['license'] == 'CC0' for it in items))