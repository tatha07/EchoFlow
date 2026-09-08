"""Tests for the license-normalization helpers and the PodcastRssResolver.

Pure unit tests with no DB or network access.
"""
import unittest
from xml.etree import ElementTree as ET

from ai_ml.scrapers.base import (
    normalize_license,
    license_features,
    license_allows_commercial,
    is_noncommercial_license,
    is_share_alike_license,
    resolve_podcast_rss,
)


class TestNormalizeLicense(unittest.TestCase):
    def test_cc0_variants(self):
        self.assertEqual(normalize_license('CC0'), 'CC0')
        self.assertEqual(normalize_license('cc0'), 'CC0')
        self.assertEqual(normalize_license('CC0 1.0'), 'CC0')
        self.assertEqual(normalize_license('Public Domain'), 'CC0')
        self.assertEqual(normalize_license('PD'), 'CC0')
        self.assertEqual(normalize_license('pdm'), 'CC0')

    def test_by_variants(self):
        self.assertEqual(normalize_license('by'), 'CC-BY')
        self.assertEqual(normalize_license('CC BY'), 'CC-BY')
        self.assertEqual(normalize_license('CC-BY'), 'CC-BY')
        self.assertEqual(normalize_license('Attribution'), 'CC-BY')

    def test_sa_variants(self):
        self.assertEqual(normalize_license('by-sa'), 'CC-BY-SA')
        self.assertEqual(normalize_license('CC BY-SA'), 'CC-BY-SA')
        self.assertEqual(normalize_license('CC-BY-SA 4.0'), 'CC-BY-SA')

    def test_nc_variants(self):
        self.assertEqual(normalize_license('by-nc'), 'CC-BY-NC')
        self.assertEqual(normalize_license('CC BY-NC'), 'CC-BY-NC')

    def test_nc_nd_variants(self):
        self.assertEqual(normalize_license('by-nc-nd'), 'CC-BY-NC-ND')
        self.assertEqual(normalize_license('CC BY-NC-ND'), 'CC-BY-NC-ND')

    def test_nc_sa_variants(self):
        self.assertEqual(normalize_license('by-nc-sa'), 'CC-BY-NC-SA')
        self.assertEqual(normalize_license('CC BY-NC-SA'), 'CC-BY-NC-SA')

    def test_ia_licenseurl(self):
        # IA returns licenseurl like "http://creativecommons.org/licenses/by-nc/3.0/"
        self.assertEqual(
            normalize_license('http://creativecommons.org/licenses/by-nc/3.0/'),
            'CC-BY-NC')
        self.assertEqual(
            normalize_license('https://creativecommons.org/licenses/by-sa/4.0/'),
            'CC-BY-SA')

    def test_remarc_variants(self):
        self.assertEqual(normalize_license('RemArc-NC'), 'REMARC-NC')
        self.assertEqual(normalize_license('remarc nc'), 'REMARC-NC')

    def test_pixabay(self):
        self.assertEqual(normalize_license('Pixabay'), 'PIXABAY')
        self.assertEqual(normalize_license('Pixabay License'), 'PIXABAY')

    def test_unknown(self):
        self.assertEqual(normalize_license(None), 'UNKNOWN')
        self.assertEqual(normalize_license(''), 'UNKNOWN')
        self.assertEqual(normalize_license('unknown'), 'UNKNOWN')
        self.assertEqual(normalize_license('not-a-real-license'), 'OTHER')

    def test_case_insensitive(self):
        self.assertEqual(normalize_license('cc-by'), 'CC-BY')
        self.assertEqual(normalize_license('CC-BY'), 'CC-BY')
        self.assertEqual(normalize_license('CC-by'), 'CC-BY')


class TestLicenseFeatures(unittest.TestCase):
    def test_cc0(self):
        nc, sa = license_features('CC0')
        self.assertFalse(nc)
        self.assertFalse(sa)

    def test_cc_by(self):
        nc, sa = license_features('CC-BY')
        self.assertFalse(nc)
        self.assertFalse(sa)

    def test_cc_by_nc(self):
        nc, sa = license_features('CC-BY-NC')
        self.assertTrue(nc)
        self.assertFalse(sa)

    def test_cc_by_sa(self):
        nc, sa = license_features('CC-BY-SA')
        self.assertFalse(nc)
        self.assertTrue(sa)

    def test_cc_by_nc_sa(self):
        nc, sa = license_features('CC-BY-NC-SA')
        self.assertTrue(nc)
        self.assertTrue(sa)

    def test_cc_by_nc_nd(self):
        # ND doesn't qualify as SA (no-derivatives, not share-alike)
        nc, sa = license_features('CC-BY-NC-ND')
        self.assertTrue(nc)
        self.assertFalse(sa)

    def test_remarc_nc(self):
        nc, sa = license_features('REMARC-NC')
        self.assertTrue(nc)
        self.assertFalse(sa)


class TestLicenseAllowsCommercial(unittest.TestCase):
    def test_cc_by_allows_commercial(self):
        self.assertTrue(license_allows_commercial('CC-BY'))

    def test_cc_by_nc_blocked_by_default(self):
        self.assertFalse(license_allows_commercial('CC-BY-NC'))
        self.assertFalse(license_allows_commercial('CC-BY-NC', allow_nc=False))

    def test_cc_by_nc_allowed_when_allow_nc(self):
        self.assertTrue(license_allows_commercial('CC-BY-NC', allow_nc=True))

    def test_remarc_nc_allowed_when_allow_nc(self):
        self.assertFalse(license_allows_commercial('REMARC-NC'))
        self.assertTrue(license_allows_commercial('REMARC-NC', allow_nc=True))

    def test_other_blocked(self):
        self.assertFalse(license_allows_commercial('OTHER'))
        self.assertFalse(license_allows_commercial('UNKNOWN'))


class TestPodcastRss(unittest.TestCase):
    def test_parses_rss2_with_audio_enclosures(self):
        xml = """<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Test Podcast</title>
    <link>https://example.com</link>
    <item>
      <title>Episode 1</title>
      <link>https://example.com/ep1</link>
      <enclosure url="https://example.com/ep1.mp3" type="audio/mpeg" length="12345"/>
    </item>
    <item>
      <title>Episode 2 (image — should be skipped)</title>
      <enclosure url="https://example.com/ep2.jpg" type="image/jpeg"/>
    </item>
  </channel>
</rss>"""
        root = ET.fromstring(xml)
        # Resolve via raw parse to skip network — call internal helper
        from unittest.mock import patch
        with patch('ai_ml.scrapers.base.get_session') as sess:
            sess.return_value.get.return_value.content = xml.encode('utf-8')
            sess.return_value.get.return_value.raise_for_status = lambda: None
            items = resolve_podcast_rss('https://example.com/feed.xml', limit=10)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['url'], 'https://example.com/ep1.mp3')
        self.assertIn('Episode 1', items[0]['title'])
        self.assertIn('Test Podcast', items[0]['title'])

    def test_skips_non_audio_url_extension(self):
        xml = """<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>X</title>
    <item>
      <title>Video</title>
      <enclosure url="https://example.com/v.mp4" type="video/mp4"/>
    </item>
  </channel>
</rss>"""
        from unittest.mock import patch
        with patch('ai_ml.scrapers.base.get_session') as sess:
            sess.return_value.get.return_value.content = xml.encode('utf-8')
            sess.return_value.get.return_value.raise_for_status = lambda: None
            items = resolve_podcast_rss('https://example.com/feed.xml')
        self.assertEqual(items, [])

    def test_respects_limit(self):
        items_xml = ''.join(
            f'<item><title>Ep{i}</title>'
            f'<enclosure url="https://example.com/{i}.mp3" type="audio/mpeg"/></item>'
            for i in range(20)
        )
        xml = f"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>X</title>{items_xml}</channel></rss>"""
        from unittest.mock import patch
        with patch('ai_ml.scrapers.base.get_session') as sess:
            sess.return_value.get.return_value.content = xml.encode('utf-8')
            sess.return_value.get.return_value.raise_for_status = lambda: None
            items = resolve_podcast_rss('https://example.com/feed.xml', limit=5)
        self.assertEqual(len(items), 5)

    def test_parses_atom_entry(self):
        xml = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Feed</title>
  <link href="https://example.com"/>
  <entry>
    <title>Atom Ep 1</title>
    <link rel="enclosure" type="audio/ogg" href="https://example.com/atom1.ogg"/>
  </entry>
</feed>"""
        from unittest.mock import patch
        with patch('ai_ml.scrapers.base.get_session') as sess:
            sess.return_value.get.return_value.content = xml.encode('utf-8')
            sess.return_value.get.return_value.raise_for_status = lambda: None
            items = resolve_podcast_rss('https://example.com/atom.xml')
        self.assertEqual(len(items), 1, items)
        self.assertEqual(items[0]['url'], 'https://example.com/atom1.ogg')