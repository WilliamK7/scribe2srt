"""Offline worker and Qt event-loop integration tests."""

import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication, QLineEdit

from api.translation_client import TranslationError
from core.async_chunk_processor import AsyncChunkProcessor, ChunkProcessorTask
from core.config import DEFAULT_TRANSLATION_SETTINGS
from core.transcription_cache import (
    transcription_cache_path, transcription_identity, save_transcription_checkpoint,
)
from core.translation import translation_cache_path
from core.worker import Worker
from ui.main_window import MainWindow
from ui.translation_settings_dialog import TranslationSettingsDialog
from tests.test_translation import FakeClient, transcript


APP = QApplication.instance() or QApplication([])


def wait_until(predicate, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        APP.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return False


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name, "media.mp3")
        self.source.write_bytes(b"offline-test-media")
        self.settings = {"translation_enabled": False}

    def worker(self, file_path=None, **kwargs):
        return Worker(str(file_path or self.source), "auto", False, 7.0, 90,
                      subtitle_settings=self.settings, **kwargs)

    def signals(self, worker):
        finished, errors = [], []
        worker.finished.connect(finished.append)
        worker.error.connect(errors.append)
        return finished, errors

    def test_json_does_not_transcribe_or_rewrite_source(self):
        source = self.source.with_suffix(".json")
        original = json.dumps(transcript("zh-TW", ["媽媽說話。"]), ensure_ascii=False).encode("utf-8")
        source.write_bytes(original)
        self.settings = {}
        worker = self.worker(source)
        finished, errors = self.signals(worker)
        with patch.object(worker.client, "prepare_upload_task", side_effect=AssertionError("No transcription")):
            worker.run()
            worker._cleanup_chunks(force_cleanup=True)
            worker._cleanup_transcription_cache()
        self.assertEqual(len(finished), 1)
        self.assertEqual(errors, [])
        self.assertEqual(source.read_bytes(), original)
        self.assertIn("妈妈说话。", source.with_suffix(".srt").read_text(encoding="utf-8"))

    def test_single_media_outputs_only_srt(self):
        data = transcript("jpn", ["體育。", "楽しい。"])  # Japanese original must not be simplified.
        raw = copy.deepcopy(data)
        self.settings = {"translation_api_key": "fake-test-key"}
        worker = self.worker()
        finished, errors = self.signals(worker)
        with patch.object(worker.client, "log_media_info", return_value={"duration": 8}), \
             patch.object(worker, "_process_single_file", side_effect=lambda path: worker.on_upload_finished(data)), \
             patch("core.subtitle_pipeline.QwenTranslationClient", return_value=FakeClient()):
            worker.run()
        self.assertEqual(errors, [])
        self.assertEqual(len(finished), 1)
        self.assertEqual({path.name for path in self.source.parent.iterdir()},
                         {self.source.name, self.source.with_suffix(".srt").name})
        self.assertEqual(data, raw)
        self.assertIn("體育。", self.source.with_suffix(".srt").read_text(encoding="utf-8"))
        self.assertTrue(self.source.exists())

    def test_media_processing_preserves_existing_json(self):
        existing_json = self.source.with_suffix(".json")
        original = b"existing user data"
        existing_json.write_bytes(original)
        worker = self.worker()
        worker.total_chunks = 1
        worker.temp_chunks = [str(self.source)]
        finished, errors = self.signals(worker)
        worker.on_upload_finished(transcript())
        self.assertEqual(errors, [])
        self.assertEqual(len(finished), 1)
        self.assertEqual(existing_json.read_bytes(), original)
        self.assertTrue(self.source.with_suffix(".srt").exists())

    def test_async_chunk_processing_does_not_write_json(self):
        processor = AsyncChunkProcessor()
        task = ChunkProcessorTask(0, str(self.source), 30.0, "auto", False, False, 1, processor)
        completed, errors = [], []
        task.signals.chunk_completed.connect(lambda index, data: completed.append(data))
        task.signals.chunk_failed.connect(lambda index, message: errors.append(message))
        with patch("core.async_chunk_processor.ElevenLabsSTTClient"), \
             patch.object(task, "_execute_upload_sync", return_value=transcript()):
            task.run()
        self.assertEqual(errors, [])
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["words"][0]["start"], 30.0)
        self.assertEqual(list(self.source.parent.iterdir()), [self.source])

    def test_translation_failure_and_retry_skip_transcription_even_if_audio_is_missing(self):
        self.settings = {"translation_api_key": "fake-test-key"}
        worker = self.worker()
        worker.combined_transcript = transcript()
        worker.total_chunks = 3
        worker.current_chunk_index = 3
        worker.temp_chunks = ["missing-1.mp3", "missing-2.mp3", "missing-3.mp3"]
        finished, errors = self.signals(worker)
        with patch("core.subtitle_pipeline.QwenTranslationClient", side_effect=TranslationError("Network exhausted")):
            worker._finalize_task()
        self.assertEqual(len(errors), 1)
        self.assertEqual(finished, [])
        self.assertTrue(Path(transcription_cache_path(self.source)).exists())
        self.assertFalse(self.source.with_suffix(".json").exists())
        state = worker.get_state()
        self.assertTrue(state["transcript_ready"])
        self.assertEqual(state["stage"], "subtitles")
        restored = self.worker(restore_state=state)
        done, failures = self.signals(restored)
        with patch.object(restored, "_run_transcription", side_effect=AssertionError("No retranscription")), \
             patch("core.subtitle_pipeline.QwenTranslationClient", return_value=FakeClient()):
            restored.run()
        self.assertEqual(failures, [])
        self.assertEqual(len(done), 1)
        self.assertFalse(os.path.exists(translation_cache_path(self.source)))
        self.assertFalse(os.path.exists(transcription_cache_path(self.source)))

    def test_reselect_media_with_matching_checkpoint_skips_transcription(self):
        identity = transcription_identity(self.source, "auto", False, 90)
        save_transcription_checkpoint(self.source, identity, transcript())
        worker = self.worker()
        finished, errors = self.signals(worker)
        with patch.object(worker, "_run_transcription", side_effect=AssertionError("No retranscription")):
            worker.run()
        self.assertEqual(errors, [])
        self.assertEqual(len(finished), 1)
        self.assertFalse(os.path.exists(transcription_cache_path(self.source)))

    def test_old_json_and_unidentified_checkpoints_are_not_automatically_reused(self):
        paths = [self.source.with_suffix(".json"),
                 self.source.with_suffix(".transcription-cache.json"),
                 Path(transcription_cache_path(self.source)), Path(translation_cache_path(self.source))]
        for path in paths:
            path.write_text(json.dumps(transcript()), encoding="utf-8")
        originals = {path: path.read_bytes() for path in paths}
        worker = self.worker()
        with patch.object(worker, "_run_transcription") as transcribe:
            worker.run()
        transcribe.assert_called_once()
        self.assertEqual({path: path.read_bytes() for path in paths}, originals)

    def test_memory_retry_rejects_changed_source_or_transcription_options(self):
        initial = self.worker()
        initial._transcription_identity = transcription_identity(self.source, "auto", False, 90)
        initial.combined_transcript = transcript()
        initial.transcript_ready = True
        state = initial.get_state()
        for change in ("source", "language", "audio_events", "split_duration"):
            with self.subTest(change=change):
                worker = self.worker(restore_state=state)
                if change == "source":
                    original = self.source.stat()
                    self.source.write_bytes(b"different-media")
                elif change == "language":
                    worker.language_code = "ja"
                elif change == "audio_events":
                    worker.tag_audio_events = True
                else:
                    worker.split_duration_sec = 60
                _, errors = self.signals(worker)
                with patch.object(worker, "_run_transcription") as transcribe, \
                        patch("core.worker.process_subtitles") as subtitles:
                    worker.run()
                self.assertEqual(len(errors), 1)
                self.assertIn("已变化", errors[0])
                transcribe.assert_not_called()
                subtitles.assert_not_called()
                if change == "source":
                    self.source.write_bytes(b"offline-test-media")
                    os.utime(self.source, ns=(original.st_atime_ns, original.st_mtime_ns))

    def test_source_changed_during_transcription_does_not_replace_srt(self):
        output = self.source.with_suffix(".srt")
        output.write_text("existing", encoding="utf-8")
        worker = self.worker()
        _, errors = self.signals(worker)
        def complete():
            self.source.write_bytes(b"changed while transcribing")
            worker.combined_transcript = transcript()
            worker._finalize_task()
        with patch.object(worker, "_run_transcription", side_effect=complete):
            worker.run()
        self.assertEqual(len(errors), 1)
        self.assertEqual(output.read_text(encoding="utf-8"), "existing")
        self.assertFalse(Path(transcription_cache_path(self.source)).exists())

    def test_segmented_completion_uses_common_pipeline_and_can_resume(self):
        self.settings = {"translation_api_key": "fake-test-key"}
        worker = self.worker()
        worker.total_chunks = 2
        first = transcript("eng", ["First segment."])
        second = transcript("eng", ["Second segment."])
        second["words"][0]["start"] += 5400
        second["words"][0]["end"] += 5400
        combined = {**first, "words": first["words"] + second["words"], "text": first["text"] + second["text"]}
        _, errors = self.signals(worker)
        with patch("core.subtitle_pipeline.QwenTranslationClient", side_effect=TranslationError("failure")):
            worker._on_async_all_completed(copy.deepcopy(combined))
        self.assertEqual(len(errors), 1)
        self.assertEqual(worker.current_chunk_index, 2)
        self.assertTrue(worker.get_state()["transcript_ready"])
        self.assertEqual(json.loads(Path(transcription_cache_path(self.source)).read_text(encoding="utf-8"))["transcript"], combined)
        restored = self.worker(restore_state=worker.get_state())
        finished, failures = self.signals(restored)
        with patch.object(restored, "_run_transcription", side_effect=AssertionError("No retranscription")), \
             patch("core.subtitle_pipeline.QwenTranslationClient", return_value=FakeClient()):
            restored.run()
        self.assertEqual(failures, [])
        self.assertEqual(len(finished), 1)
        self.assertEqual({path.name for path in self.source.parent.iterdir()},
                         {self.source.name, self.source.with_suffix(".srt").name})

    def test_cancellation_emits_once_and_discards_late_upload(self):
        worker = self.worker()
        _, errors = self.signals(worker)
        worker.request_cancellation()
        worker.request_cancellation()
        worker.on_upload_finished(transcript())
        self.assertEqual(len(errors), 1)
        self.assertFalse(self.source.with_suffix(".srt").exists())


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings_path = Path(self.directory.name, "settings.json")
        self.patches = [patch("ui.main_window.SETTINGS_FILE", str(self.settings_path)),
                        patch("ui.main_window.is_ffmpeg_available", return_value=False),
                        patch("ui.main_window.QMessageBox.information", return_value=None)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.window = MainWindow()
        self.addCleanup(self.window.close)

    def source(self, name, language="zho"):
        path = Path(self.directory.name, name)
        path.write_text(json.dumps(transcript(language), ensure_ascii=False), encoding="utf-8")
        return str(path)

    def test_old_settings_defaults_and_toggle_persistence(self):
        self.settings_path.write_text('{"max_concurrent_chunks":2}', encoding="utf-8")
        self.window.load_settings()
        for name, default in DEFAULT_TRANSLATION_SETTINGS.items():
            self.assertEqual(self.window.settings[name], default)
        self.window.translation_checkbox.setChecked(False)
        saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertFalse(saved["translation_enabled"])

    def test_dialog_masks_key_and_round_trips_official_settings(self):
        dialog = TranslationSettingsDialog({"translation_api_key": "fake-test-key", "translation_batch_size": 20})
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.api_key_edit.echoMode(), QLineEdit.EchoMode.Password)
        self.assertEqual(dialog.get_settings()["translation_api_key"], "fake-test-key")
        self.assertEqual(dialog.get_settings()["translation_batch_size"], 20)
        self.assertEqual(dialog.get_settings()["translation_region"], "cn-beijing")

    def test_json_processing_is_in_background_and_restores_ui(self):
        source = self.source("input.json")
        started = []
        def pipeline(*args, **kwargs):
            started.append(QThread.currentThread() != APP.thread())
            kwargs["progress"](1, 3)
            time.sleep(0.05)
            kwargs["progress"](3, 3)
        with patch("core.worker.process_subtitles", side_effect=pipeline):
            self.window.set_files([source])
            self.window.start_process()
            self.assertFalse(self.window.translation_checkbox.isEnabled())
            self.assertTrue(wait_until(lambda: self.window.thread is None))
        self.assertEqual(started, [True])
        self.assertTrue(self.window.translation_checkbox.isEnabled())
        self.assertTrue(self.window.select_button.isEnabled())
        self.assertFalse(self.window.subtitle_progress_bar.isVisible())

    def test_batch_failure_continues_and_shares_translation_limiter(self):
        first, second = self.source("one.json"), self.source("two.json")
        calls, limiter_ids = [], []
        def pipeline(data, source, **kwargs):
            calls.append(source)
            limiter_ids.append(id(kwargs["rate_limiter"]))
            if source == first:
                raise TranslationError("expected file failure")
            kwargs["progress"](3, 3)
        with patch("core.worker.process_subtitles", side_effect=pipeline):
            self.window.set_files([first, second])
            self.window.start_process()
            self.assertTrue(wait_until(lambda: not self.window.batch_mode))
        self.assertEqual(calls, [first, second])
        self.assertEqual(len(set(limiter_ids)), 1)
        self.assertIsNone(self.window.thread)

    def test_batch_cancel_stops_queue_and_keeps_source_json(self):
        first, second = self.source("cancel.json"), self.source("skip.json")
        calls = []
        def pipeline(data, source, **kwargs):
            calls.append(source)
            kwargs["progress"](0, 3)
            kwargs["cancellation"].wait(2)
        with patch("core.worker.process_subtitles", side_effect=pipeline):
            self.window.set_files([first, second])
            self.window.start_process()
            self.assertTrue(wait_until(lambda: bool(calls)))
            self.window.cancel_process()
            self.assertTrue(wait_until(lambda: not self.window.batch_mode))
        self.assertEqual(calls, [first])
        self.assertTrue(Path(first).exists())
        self.assertTrue(Path(second).exists())

    def test_progress_shows_completed_entry_count(self):
        self.window.show()
        self.window.update_subtitle_progress(7, 25)
        self.assertEqual(self.window.subtitle_progress_bar.value(), 7)
        self.assertEqual(self.window.subtitle_progress_bar.maximum(), 25)
        self.assertIn("7 / 25", self.window.progress_label.text())
        self.assertTrue(self.window.subtitle_progress_bar.isVisible())

    def test_video_with_transcription_checkpoint_skips_audio_extraction(self):
        source = Path(self.directory.name, "resume.mp4")
        source.write_bytes(b"video-test")
        identity = transcription_identity(source, "auto", False, self.window.settings["split_duration_min"])
        save_transcription_checkpoint(source, identity, transcript())
        self.window.ffmpeg_available = True
        with patch("ui.main_window.extract_audio", side_effect=AssertionError("No extraction")), \
             patch.object(self.window, "_execute_transcription_task") as execute:
            self.window._begin_media_processing(str(source))
        execute.assert_called_once()
        self.assertEqual(execute.call_args.args, (str(source), str(source)))
        state = execute.call_args.kwargs["restore_state"]
        self.assertEqual(state["transcription_identity"], identity)
        self.assertTrue(state["transcript_ready"])

    def test_video_with_mismatched_checkpoint_still_extracts_audio(self):
        source = Path(self.directory.name, "changed.mp4")
        source.write_bytes(b"video-test")
        identity = transcription_identity(source, "ja", False, self.window.settings["split_duration_min"])
        save_transcription_checkpoint(source, identity, transcript())
        self.window.ffmpeg_available = True
        with patch("ui.main_window.get_media_info", return_value={"codec": "aac"}), \
                patch("ui.main_window.extract_audio", return_value=True) as extract, \
                patch.object(self.window, "_execute_transcription_task") as execute:
            self.window._begin_media_processing(str(source))
        extract.assert_called_once()
        self.assertNotEqual(execute.call_args.args[0], str(source))
        self.assertEqual(execute.call_args.args[1], str(source))


if __name__ == "__main__":
    unittest.main()
