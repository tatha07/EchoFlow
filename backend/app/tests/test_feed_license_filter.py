"""Feed license-filter tests.

Verifies that NC and SA clips are excluded from /feed/ and /suggestions/
even when `moderation_approved=True`. Regression coverage for the runtime
gate added in the scraper coverage expansion.
"""
import unittest
from unittest.mock import patch, MagicMock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient


class FeedLicenseFilterTests(TestCase):
    def setUp(self):
        from backend.app.models import AudioClip
        User = get_user_model()
        self.user = User.objects.create_user(
            username='feedtester', password='x')
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.url = '/feed/'

        # Four clips: CC-BY (allowed), CC0 (allowed), CC-BY-NC (gated),
        # CC-BY-SA (gated). All moderation_approved=True.
        self.clip_cc_by = AudioClip.objects.create(
            creator=self.user, title='CC-BY clip', category='music',
            status='ready', moderation_approved=True,
            license='CC-BY', license_family='CC-BY',
            is_noncommercial=False, requires_share_alike=False,
            duration_ms=10000,
        )
        self.clip_cc0 = AudioClip.objects.create(
            creator=self.user, title='CC0 clip', category='music',
            status='ready', moderation_approved=True,
            license='CC0', license_family='CC0',
            is_noncommercial=False, requires_share_alike=False,
            duration_ms=10000,
        )
        self.clip_nc = AudioClip.objects.create(
            creator=self.user, title='NC clip', category='music',
            status='ready', moderation_approved=True,
            license='CC-BY-NC', license_family='CC-BY-NC',
            is_noncommercial=True, requires_share_alike=False,
            duration_ms=10000,
        )
        self.clip_sa = AudioClip.objects.create(
            creator=self.user, title='SA clip', category='music',
            status='ready', moderation_approved=True,
            license='CC-BY-SA', license_family='CC-BY-SA',
            is_noncommercial=False, requires_share_alike=True,
            duration_ms=10000,
        )

    def test_fallback_excludes_nc_and_sa(self):
        # When Redis is empty / errors, the feed falls back to a trending
        # query. Force the primary path to fail by mocking Redis to raise.
        from backend.app.views import feed as feed_view
        with patch.object(feed_view, 'redis_client') as rc:
            rc.llen.side_effect = Exception('redis down')
            resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        ids = {r['id'] for r in resp.data['results']}
        self.assertIn(str(self.clip_cc_by.id), ids)
        self.assertIn(str(self.clip_cc0.id), ids)
        self.assertNotIn(str(self.clip_nc.id), ids)
        self.assertNotIn(str(self.clip_sa.id), ids)

    def test_suggestions_excludes_nc_and_sa(self):
        from backend.app.views import feed as feed_view
        url = f'/suggestions/?category=music'
        with patch.object(feed_view, 'get_user_vectors',
                          return_value=(None, None)):
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        ids = {r['id'] for r in resp.data['results']}
        self.assertNotIn(str(self.clip_nc.id), ids)
        self.assertNotIn(str(self.clip_sa.id), ids)