"""Tests for the segment-splitter and AudioClip.group_id workflow."""
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from ai_ml.scrapers import normalizer, uploader


def _make_test_audio(path, duration_seconds=10, sample_rate=44100):
    """Create a small test WAV of `duration_seconds`."""
    from pydub.generators import Sine
    audio = Sine(440).to_audio_segment(duration=duration_seconds * 1000)
    audio = audio.set_frame_rate(sample_rate).set_channels(2)
    audio.export(path, format='wav')
    return path


class SplitIntoSegmentsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='seg_test_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = os.path.join(self.tmp, 'src.wav')
        _make_test_audio(self.src, duration_seconds=10)

    def test_no_split_when_max_seconds_zero(self):
        out_dir = os.path.join(self.tmp, 'out')
        segs = normalizer.split_into_segments(
            in_path=self.src, max_seconds=0, target_dir=out_dir)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]['index'], 0)
        self.assertGreater(segs[0]['duration_ms'], 9000)
        self.assertTrue(os.path.exists(segs[0]['path']))

    def test_no_split_when_fits(self):
        out_dir = os.path.join(self.tmp, 'out')
        segs = normalizer.split_into_segments(
            in_path=self.src, max_seconds=30, target_dir=out_dir)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]['duration_ms'], 10000)

    def test_split_into_multiple(self):
        out_dir = os.path.join(self.tmp, 'out')
        # 10s audio split into 3s pieces → 4 segments (3+3+3+1)
        segs = normalizer.split_into_segments(
            in_path=self.src, max_seconds=3, target_dir=out_dir)
        self.assertEqual(len(segs), 4)
        # Indices are 0..3
        self.assertEqual([s['index'] for s in segs], [0, 1, 2, 3])
        # Total duration adds up
        self.assertEqual(sum(s['duration_ms'] for s in segs), 10000)
        # First three are 3s, last is 1s
        self.assertEqual(segs[0]['duration_ms'], 3000)
        self.assertEqual(segs[3]['duration_ms'], 1000)
        # All files exist
        for s in segs:
            self.assertTrue(os.path.exists(s['path']))

    def test_split_cleanups_on_failure(self):
        """If a segment export fails, previously-emitted files are cleaned up."""
        out_dir = os.path.join(self.tmp, 'out')

        # Make pydub's export fail on the second segment
        original_export = normalizer.AudioSegment.export
        call_count = [0]

        def flaky_export(self, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 2:
                raise RuntimeError('simulated export failure')
            return original_export(self, *args, **kwargs)

        with patch.object(normalizer.AudioSegment, 'export', flaky_export):
            with self.assertRaises(RuntimeError):
                normalizer.split_into_segments(
                    in_path=self.src, max_seconds=3, target_dir=out_dir)
        # The first segment file should have been cleaned up.
        for s in range(0, 1):
            for f in os.listdir(out_dir):
                self.assertNotIn('seg-000', f)

    def test_backward_compat_trim(self):
        """normalize_and_trim still works (truncates, single output)."""
        out = os.path.join(self.tmp, 'trimmed.mp3')
        normalizer.normalize_and_trim(self.src, out, max_seconds=5)
        self.assertTrue(os.path.exists(out))
        # Output should be at most 5s
        from pydub import AudioSegment as _AS
        loaded = _AS.from_file(out)
        self.assertLessEqual(len(loaded), 5000 + 100)  # tiny fudge


class SaveClipSegmentsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='seg_test_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = os.path.join(self.tmp, 'src.wav')
        _make_test_audio(self.src, duration_seconds=10)

    def test_save_clip_segments_single_when_max_seconds_0(self):
        user = MagicMock()
        user.id = 1
        # We mock the model create path to avoid hitting DB
        with patch('backend.app.models.AudioClip') as MockClip:
            mock_instance = MagicMock()
            mock_instance.id = 'single-clip-id'
            mock_instance.group_id = None
            MockClip.return_value = mock_instance
            clips = uploader.save_clip_segments(
                user=user, title='Test', source_name='test_src',
                source_url='http://x', license='CC0',
                attribution_text='CC0', local_file_path=self.src,
                max_seconds=0,
            )
        self.assertEqual(len(clips), 1)
        # No group_id when only one segment
        self.assertIsNone(clips[0].group_id)

    def test_save_clip_segments_multiple(self):
        user = MagicMock()
        user.id = 1
        with patch('backend.app.models.AudioClip') as MockClip:
            instances = []
            def make_clip(**kwargs):
                m = MagicMock()
                m.id = f"clip-{len(instances)}"
                # Capture the group_id passed in
                m.group_id = kwargs.get('group_id')
                m.segment_index = kwargs.get('segment_index')
                m.segment_count = kwargs.get('segment_count')
                instances.append(m)
                return m
            MockClip.side_effect = make_clip

            clips = uploader.save_clip_segments(
                user=user, title='Long Audio', source_name='test_src',
                source_url='http://x', license='CC0',
                attribution_text='CC0', local_file_path=self.src,
                max_seconds=3,  # 10s → 4 segments
            )
        self.assertEqual(len(clips), 4)
        # All segments share the same group_id
        group_ids = {c.group_id for c in clips}
        self.assertEqual(len(group_ids), 1)
        # Indices are 0..3
        self.assertEqual([c.segment_index for c in clips], [0, 1, 2, 3])
        # All share the same count
        self.assertTrue(all(c.segment_count == 4 for c in clips))
