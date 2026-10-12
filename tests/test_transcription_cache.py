"""Task identity guards against accidental cross-input and stale checkpoint reuse."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.transcription_cache import (
    transcription_cache_path, transcription_identity, load_transcription_checkpoint,
    save_transcription_checkpoint,
)


class TranscriptionCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name) / "same.mp3"
        self.source.write_bytes(b"media")
        self.identity = self.identify()
        self.transcript = {"language_code": "eng", "words": [{"text": "Hello.", "start": 0, "end": 1}]}
        save_transcription_checkpoint(self.source, self.identity, self.transcript)

    def identify(self, **options):
        return transcription_identity(self.source, **{
            "language_code": "auto", "tag_audio_events": False, "split_duration_min": 90, **options})

    def test_different_extensions_and_copied_checkpoints_do_not_share_results(self):
        other = self.source.with_suffix(".mp4")
        other.write_bytes(self.source.read_bytes())
        os.utime(other, ns=(self.source.stat().st_atime_ns, self.source.stat().st_mtime_ns))
        self.assertNotEqual(transcription_cache_path(other), transcription_cache_path(self.source))
        Path(transcription_cache_path(other)).write_bytes(Path(transcription_cache_path(self.source)).read_bytes())
        self.assertIsNone(load_transcription_checkpoint(other, transcription_identity(other, "auto", False, 90)))

    def test_size_and_nanosecond_time_changes_invalidate(self):
        original = self.source.stat()
        self.source.write_bytes(b"larger media")
        os.utime(self.source, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assertIsNone(load_transcription_checkpoint(self.source, self.identify()))
        self.source.write_bytes(b"media")
        os.utime(self.source, ns=(original.st_atime_ns, original.st_mtime_ns + 1_000_000))
        self.assertIsNone(load_transcription_checkpoint(self.source, self.identify()))

    def test_transcription_options_and_model_invalidate(self):
        for options in ({"language_code": "ja"}, {"tag_audio_events": True}, {"split_duration_min": 30}):
            with self.subTest(options=options):
                self.assertIsNone(load_transcription_checkpoint(self.source, self.identify(**options)))
        with patch("api.client.DEFAULT_STT_MODEL_ID", "different-model"):
            self.assertIsNone(load_transcription_checkpoint(self.source, self.identify()))

    def test_valid_checkpoint_returns_original_transcript(self):
        self.assertEqual(load_transcription_checkpoint(self.source, self.identify()), self.transcript)
        self.assertEqual(self.source.read_bytes(), b"media")

    def test_corrupt_and_unknown_schema_checkpoints_are_ignored_without_deleting(self):
        path = Path(transcription_cache_path(self.source))
        valid = json.loads(path.read_text(encoding="utf-8"))
        for content in ("invalid", "[]", json.dumps(self.transcript),
                        json.dumps({**valid, "schema": 2}),
                        json.dumps({**valid, "transcript": {"words": [None]}})):
            with self.subTest(content=content):
                path.write_text(content, encoding="utf-8")
                self.assertIsNone(load_transcription_checkpoint(self.source, self.identity))
                self.assertEqual(path.read_text(encoding="utf-8"), content)
