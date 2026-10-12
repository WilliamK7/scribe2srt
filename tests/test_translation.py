"""Offline acceptance tests for Qwen-MT batching and subtitle persistence."""

import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from opencc import OpenCC
import requests

from api.translation_client import (
    QwenTranslationClient, RequestRateLimiter, TranslationError, TranslationFormatError,
    TranslationSettings, official_endpoint, retry_after_seconds,
)
from core.atomic_io import atomic_write_text
from core.cancellation import CancellationToken, ProcessingCancelled
from core.languages import normalize_language, qwen_language_name, source_language
from core.srt_processor import SrtProcessor, SubtitleEntry, create_srt_from_json, format_srt_time
from core.subtitle_pipeline import process_subtitles
from core.translation import (
    TranslationCache, TranslationCoordinator, batch_fingerprint, batch_prompt,
    make_batches, parse_numbered_translations, translation_cache_path,
)


def entry(number, text="Hello."):
    return SubtitleEntry(number, number * 2.0, number * 2.0 + 1.5, text)


def transcript(language="eng", texts=None):
    texts = texts or ["Hello.", "How are you?", "Hello."]
    data = {"text": " ".join(texts), "words": [
        {"text": text, "start": i * 4.0, "end": i * 4.0 + 2,
         "type": "word", "speaker_id": "speaker_0"}
        for i, text in enumerate(texts)
    ]}
    if language is not None:
        data["language_code"] = language
    return data


class FakeClient:
    def __init__(self, handler=None, settings=None):
        self.settings = settings or TranslationSettings(api_key="fake-test-key", max_concurrent=1)
        self.handler = handler
        self.calls = []

    def translate(self, prompt, language, cancellation):
        cancellation.check()
        numbers = [int(number) for number in re.findall(r"^\[(\d+)\]", prompt, re.MULTILINE)]
        self.calls.append(numbers)
        if self.handler:
            return self.handler(numbers)
        return "\n".join(f"[{number:06d}] 譯文 {number}" for number in reversed(numbers))


class BatchAndParserTests(unittest.TestCase):
    def test_batch_count_tail_and_adjacent_order(self):
        batches = make_batches([entry(i) for i in range(1, 24)])
        self.assertEqual([len(batch) for batch in batches], [10, 10, 3])
        self.assertEqual([item.number for batch in batches for item in batch], list(range(1, 24)))
        self.assertEqual([len(b) for b in make_batches([entry(i) for i in range(21)], 20)], [20, 1])

    def test_character_budget_and_oversized_entry(self):
        entries = [entry(1, "a" * 1000), entry(2, "b" * 1000), entry(3, "c"),
                   entry(4, "d" * 2001), entry(5, "e")]
        batches = make_batches(entries)
        self.assertEqual([[e.number for e in b] for b in batches], [[1, 2], [3], [4], [5]])
        self.assertIn("d" * 2001, batch_prompt(batches[2]))
        self.assertEqual(make_batches([]), [])

    def test_prompt_has_identifiers_without_timecodes(self):
        prompt = batch_prompt([entry(1, "A\nB")])
        self.assertIn("[000001] A B", prompt)
        self.assertNotIn("-->", prompt)
        self.assertNotIn("00:00:", prompt)

    def test_reverse_order_and_multiline_translations(self):
        result = parse_numbered_translations("[000002] 你好\n朋友\n[000001] 很好", [entry(1), entry(2)])
        self.assertEqual(result, {2: "你好 朋友", 1: "很好"})

    def test_invalid_responses_are_rejected(self):
        for content in (
            "[000001] 一",  # Missing number.
            "[000001] 一\n[000001] 二\n[000002] 二",  # Duplicate.
            "[000001] 一\n[000002] 二\n[000003] 三",  # Extra.
            "[000001] \n[000002] 二",  # Empty.
            "[1] 一\n[000002] 二",  # Non-fixed identifier.
            "翻译如下：\n[000001] 一\n[000002] 二",  # Explanation.
            "```\n[000001] 一\n[000002] 二\n```",  # Markdown fence.
            "[000001] 一 [000002] 二",  # Same-line extra identifier.
        ):
            with self.subTest(content=content), self.assertRaises(TranslationFormatError):
                parse_numbered_translations(content, [entry(1), entry(2)])


class ClientTests(unittest.TestCase):
    def response(self, status=200, content="[000001] 你好", finish="stop", headers=None):
        result = Mock(status_code=status, headers=headers or {})
        result.json.return_value = {"choices": [{"message": {"content": content}, "finish_reason": finish}]}
        return result

    def test_official_endpoints_and_model_validation(self):
        for region, host in (("cn-beijing", "dashscope.aliyuncs.com"),
                             ("ap-southeast-1", "dashscope-intl.aliyuncs.com"),
                             ("cn-hongkong", "cn-hongkong.dashscope.aliyuncs.com")):
            self.assertEqual(official_endpoint(region), f"https://{host}/compatible-mode/v1/chat/completions")
            self.assertIn(f"https://workspace-123.{region}.maas.aliyuncs.com/", official_endpoint(region, "workspace-123"))
        for workspace in ("https://evil.example", "a.b", "x/y", "-abc", "abc-"):
            with self.subTest(workspace=workspace), self.assertRaises(ValueError):
                official_endpoint("cn-beijing", workspace)
        with self.assertRaises(ValueError):
            official_endpoint("custom")
        TranslationSettings(model="qwen-mt-custom-2026").validate(require_key=False)
        for changes in ({"model": "qwen-plus"}, {"batch_size": 21}, {"batch_size": True},
                        {"max_concurrent": 0}, {"requests_per_minute": 0}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(TranslationSettings(), **changes).validate(require_key=False)
        with self.assertRaises(TranslationError):
            TranslationSettings().validate()

    def test_request_contract_and_hidden_credentials(self):
        post = Mock(return_value=self.response())
        settings = TranslationSettings(api_key="never-print-this-key")
        result = QwenTranslationClient(settings, post=post).translate("[000001] Hello", "eng")
        self.assertEqual(result, "[000001] 你好")
        url, = post.call_args.args
        kwargs = post.call_args.kwargs
        self.assertEqual(url, settings.endpoint)
        self.assertEqual(kwargs["timeout"], (10, 60))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["json"]["messages"], [{"role": "user", "content": "[000001] Hello"}])
        self.assertFalse(kwargs["json"]["stream"])
        self.assertEqual(kwargs["json"]["translation_options"], {"source_lang": "English", "target_lang": "Chinese"})
        self.assertNotIn(settings.api_key, repr(settings))

    def test_network_retries_and_retry_after(self):
        token = Mock(spec=CancellationToken)
        responses = [self.response(429, headers={"Retry-After": "5"}), self.response(503), self.response()]
        post = Mock(side_effect=responses)
        client = QwenTranslationClient(TranslationSettings(api_key="fake-test-key"), post=post)
        client.translate("test", "ja", token)
        self.assertEqual(post.call_count, 3)
        self.assertEqual([call.args[0] for call in token.wait.call_args_list], [5, 2])
        for response in responses:
            response.close.assert_called_once()

    def test_retry_limits_for_timeouts_connections_and_server_errors(self):
        for failure in (requests.Timeout("secret"), requests.ConnectionError("secret"), self.response(500)):
            with self.subTest(failure=type(failure).__name__):
                post = Mock(side_effect=failure) if isinstance(failure, Exception) else Mock(return_value=failure)
                token = Mock(spec=CancellationToken)
                logs = []
                client = QwenTranslationClient(TranslationSettings(api_key="secret"), post=post, log=logs.append)
                with self.assertRaises(TranslationError) as error:
                    client.translate("test", "ko", token)
                self.assertEqual(post.call_count, 4)
                self.assertEqual([call.args[0] for call in token.wait.call_args_list], [1, 2, 4])
                self.assertNotIn("secret", str(error.exception) + "".join(logs))

    def test_auth_parameter_and_model_errors_do_not_retry(self):
        for status in (400, 401, 403, 404, 302):
            with self.subTest(status=status):
                post = Mock(return_value=self.response(status))
                with self.assertRaises(TranslationError):
                    QwenTranslationClient(TranslationSettings(api_key="fake-test-key"), post=post).translate("test", "auto")
                self.assertEqual(post.call_count, 1)

    def test_empty_malformed_and_truncated_responses(self):
        for response in (self.response(content=""), self.response(finish="length"), self.response(finish=None),
                         Mock(status_code=200, headers={}, json=Mock(return_value={}))):
            with self.subTest(response=response), self.assertRaises(TranslationFormatError):
                QwenTranslationClient(TranslationSettings(api_key="fake-test-key"), post=Mock(return_value=response)).translate("test", "en")

    def test_cancelled_response_is_ignored(self):
        token = CancellationToken()
        def late_response(*args, **kwargs):
            token.cancel()
            return self.response()
        with self.assertRaises(ProcessingCancelled):
            QwenTranslationClient(TranslationSettings(api_key="fake-test-key"), post=late_response).translate("test", "en", token)

    def test_retry_after_http_date_and_invalid_header(self):
        future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30))
        self.assertGreater(retry_after_seconds(future), 28)
        self.assertEqual(retry_after_seconds("nonsense"), 0)
        self.assertEqual(retry_after_seconds("-1"), 0)
        self.assertEqual(retry_after_seconds("inf"), 0)
        self.assertEqual(retry_after_seconds("nan"), 0)

    def test_rate_limit_rolling_window(self):
        now = [0.0]
        limiter = RequestRateLimiter(2, clock=lambda: now[0])
        token = Mock(spec=CancellationToken)
        token.wait.side_effect = lambda seconds: now.__setitem__(0, now[0] + seconds)
        limiter.acquire(token)
        limiter.acquire(token)
        limiter.acquire(token)
        self.assertGreaterEqual(now[0], 60)
        self.assertEqual(len(limiter._requests), 1)

    def test_rate_limit_wait_is_cancellable(self):
        limiter = RequestRateLimiter(1)
        token = CancellationToken()
        limiter.acquire(token)
        timer = threading.Timer(0.02, token.cancel)
        timer.start()
        started = time.monotonic()
        with self.assertRaises(ProcessingCancelled):
            limiter.acquire(token)
        timer.join()
        self.assertLess(time.monotonic() - started, 0.5)

    def test_concurrency_limit_is_shared_between_files_and_clients(self):
        limiter = RequestRateLimiter(60, max_concurrent=2)
        settings = TranslationSettings(api_key="fake-test-key", max_concurrent=2)
        active, maximum, lock = [0], [0], threading.Lock()
        started, release = threading.Event(), threading.Event()
        errors = []
        def post(*args, **kwargs):
            with lock:
                active[0] += 1
                maximum[0] = max(maximum[0], active[0])
                if active[0] == 2:
                    started.set()
            release.wait(2)
            with lock:
                active[0] -= 1
            return self.response()
        clients = [QwenTranslationClient(settings, limiter, post=post) for _ in range(2)]
        def run(client):
            try:
                client.translate("test", "en")
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=run, args=(clients[i % 2],)) for i in range(4)]
        for thread in threads:
            thread.start()
        self.assertTrue(started.wait(2))
        self.assertEqual(active[0], 2)
        release.set()
        for thread in threads:
            thread.join(2)
        self.assertEqual(errors, [])
        self.assertEqual(maximum[0], 2)

    def test_waiting_for_concurrency_slot_is_cancellable(self):
        limiter = RequestRateLimiter(60, max_concurrent=1)
        limiter.acquire_slot(CancellationToken())
        token = CancellationToken()
        timer = threading.Timer(0.02, token.cancel)
        timer.start()
        with self.assertRaises(ProcessingCancelled):
            limiter.acquire_slot(token)
        timer.join()
        limiter.release_slot()


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache_path = os.path.join(self.directory.name, "test.translation-cache.json")
        self.simplify = OpenCC("t2s").convert

    def coordinator(self, client, token=None, progress=None):
        return TranslationCoordinator(client, TranslationCache(self.cache_path), token, progress)

    def test_reverse_mapping_progress_and_simplification(self):
        client, progress = FakeClient(), []
        entries = [entry(1, "Same."), entry(2, "Same."), entry(3, "Sentence continues")]
        result = self.coordinator(client, progress=lambda d, t: progress.append((d, t))).translate_entries(entries, "en", self.simplify)
        self.assertEqual([e.translation for e in result], ["译文 1", "译文 2", "译文 3"])
        self.assertEqual([(e.number, e.start, e.end, e.text) for e in entries],
                         [(e.number, e.start, e.end, e.text) for e in result])
        self.assertEqual(progress[-1], (3, 3))
        self.assertNotIn("fake-test-key", Path(self.cache_path).read_text(encoding="utf-8"))

    def test_recursive_split_and_saved_branches_resume(self):
        def handler(numbers):
            if len(numbers) > 1:
                return f"[{numbers[0]:06d}] 一"  # Missing the other identifiers.
            if numbers == [4]:
                raise TranslationFormatError("截断")
            return f"[{numbers[0]:06d}] 已完成"
        client = FakeClient(handler)
        entries = [entry(i) for i in range(1, 5)]
        with self.assertRaises(TranslationError):
            self.coordinator(client).translate_entries(entries, "en", self.simplify)
        self.assertEqual(client.calls, [[1, 2, 3, 4], [1, 2], [1], [2], [3, 4], [3], [4]])
        restored = FakeClient()
        results = self.coordinator(restored).translate_entries(entries, "en", self.simplify)
        self.assertEqual(restored.calls, [[4]])
        self.assertEqual(len(results), 4)

    def test_network_errors_do_not_split(self):
        def handler(numbers):
            raise TranslationError("鉴权失败")
        client = FakeClient(handler)
        with self.assertRaises(TranslationError):
            self.coordinator(client).translate_entries([entry(1), entry(2)], "en", self.simplify)
        self.assertEqual(client.calls, [[1, 2]])

    def test_context_language_model_endpoint_prompt_and_batch_config_invalidate(self):
        entries = [entry(1), entry(2, "Context.")]
        client = FakeClient()
        baseline = batch_fingerprint(entries, "en", client.settings)
        variations = [
            batch_fingerprint([entry(1), entry(2, "Other context.")], "en", client.settings),
            batch_fingerprint(entries, "ja", client.settings),
            batch_fingerprint(entries, "en", replace(client.settings, model="qwen-mt-plus")),
            batch_fingerprint(entries, "en", replace(client.settings, region="ap-southeast-1")),
            batch_fingerprint(entries, "en", replace(client.settings, workspace_id="abc")),
            batch_fingerprint(entries, "en", replace(client.settings, batch_size=5)),
            batch_fingerprint(entries, "en", client.settings, max_characters=1000),
        ]
        with patch("core.translation.PROMPT_VERSION", "v-next"):
            variations.append(batch_fingerprint(entries, "en", client.settings))
        self.assertTrue(all(value != baseline for value in variations))
        self.assertEqual(baseline, batch_fingerprint(entries, "en", replace(client.settings, api_key="other-secret")))
        self.coordinator(client).translate_entries(entries, "en", self.simplify)
        reused = FakeClient()
        self.coordinator(reused).translate_entries(entries, "en", self.simplify)
        self.assertEqual(reused.calls, [])
        changed = FakeClient()
        self.coordinator(changed).translate_entries([entry(1), entry(2, "Changed.")], "en", self.simplify)
        self.assertEqual(changed.calls, [[1, 2]])

    def test_corrupt_cache_is_retranslated(self):
        Path(self.cache_path).write_text('{"schema":1,"batches": []}', encoding="utf-8")
        client = FakeClient()
        self.coordinator(client).translate_entries([entry(1)], "en", self.simplify)
        self.assertEqual(client.calls, [[1]])

    def test_concurrency_limit_and_no_further_dispatch_after_cancel(self):
        token, active, maximum, lock = CancellationToken(), [0], [0], threading.Lock()
        started, release = threading.Event(), threading.Event()
        class SlowClient(FakeClient):
            def translate(self, prompt, language, cancellation):
                numbers = [int(n) for n in re.findall(r"^\[(\d+)\]", prompt, re.MULTILINE)]
                with lock:
                    self.calls.append(numbers)
                    active[0] += 1
                    maximum[0] = max(maximum[0], active[0])
                    if active[0] == 2:
                        started.set()
                release.wait(2)
                with lock:
                    active[0] -= 1
                return f"[{numbers[0]:06d}] 迟到译文"
        client = SlowClient(settings=TranslationSettings(api_key="fake-test-key", batch_size=1, max_concurrent=2))
        coordinator = self.coordinator(client, token)
        outcomes = []
        def run():
            try:
                coordinator.translate_entries([entry(i) for i in range(1, 6)], "en", self.simplify)
            except Exception as exc:
                outcomes.append(exc)
        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(started.wait(2))
        token.cancel()
        thread.join(0.5)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcomes[0], ProcessingCancelled)
        release.set()
        deadline = time.monotonic() + 2
        while active[0] and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(maximum[0], 2)
        self.assertEqual(len(client.calls), 2)
        self.assertFalse(os.path.exists(self.cache_path))  # No late responses persisted.


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = os.path.join(self.directory.name, "input.json")
        self.output = Path(self.directory.name, "input.srt")

    def factory(self, client):
        return lambda *args, **kwargs: client

    def test_language_normalization_and_metadata_priority(self):
        for value, expected in (("zho", "zh"), ("chi", "zh"), ("zh-Hant-TW", "zh"), ("zh_CN", "zh"),
                                ("eng-US", "en"), ("jpn", "ja"), ("ja-JP", "ja"), ("kor", "ko"),
                                ("Chinese", "zh"), (None, "auto")):
            with self.subTest(value=value):
                self.assertEqual(normalize_language(value), expected)
        self.assertEqual(source_language({"language_code": "ja-JP"}, "zh"), "ja")
        self.assertEqual(source_language({}, "zh"), "zh")
        self.assertEqual(source_language({}, "auto"), "auto")
        self.assertEqual(qwen_language_name("kor"), "Korean")
        self.assertEqual(qwen_language_name("unknown-code"), "auto")

    def test_chinese_simplification_and_no_remote_call(self):
        for language, text in (("zh-TW", "媽媽說這個軟體很好。"), ("zho", "妈妈说这个软件很好。")):
            with self.subTest(language=language):
                data = transcript(language, [text])
                original = copy.deepcopy(data)
                factory = Mock(side_effect=AssertionError("Chinese must not use the API"))
                process_subtitles(data, self.source, client_factory=factory)
                self.assertIn("妈妈说这个软体很好。" if language == "zh-TW" else text,
                              self.output.read_text(encoding="utf-8"))
                factory.assert_not_called()
                self.assertEqual(data, original)

    def test_english_japanese_korean_dialogue_events_have_exactly_two_lines(self):
        for language, texts in (("eng", ["- Are you sure?", "- Yes, I am sure.", "The sentence", "continues here."]),
                                ("jpn", ["彼女は體育が好きです。", "本当に？", "本当に？"]),
                                ("kor", ["안녕하세요.", "같은 문장입니다.", "같은 문장입니다."])):
            with self.subTest(language=language):
                data = transcript(language, texts)
                data["words"].append({"text": "(applause)", "start": 20, "end": 21, "type": "audio_event"})
                original = copy.deepcopy(data)
                baseline = SrtProcessor(data).create_entries()
                process_subtitles(data, self.source, settings={"translation_api_key": "fake-test-key"},
                                  client_factory=self.factory(FakeClient()))
                blocks = self.output.read_text(encoding="utf-8").strip().split("\n\n")
                self.assertEqual(len(blocks), len(baseline))
                for block, expected in zip(blocks, baseline):
                    lines = block.splitlines()
                    self.assertEqual(len(lines), 4)
                    self.assertEqual(int(lines[0]), expected.number)
                    self.assertEqual(lines[1], f"{format_srt_time(expected.start)} --> {format_srt_time(expected.end)}")
                    self.assertEqual(lines[2], expected.text)
                    self.assertEqual(lines[3], f"译文 {expected.number}")
                self.assertEqual(data, original)
                self.assertFalse(os.path.exists(translation_cache_path(self.source)))

    def test_foreign_long_lines_do_not_wrap_and_times_stay_fixed(self):
        processor = SrtProcessor(transcript())
        original = entry(9, "Long original " * 15)
        translated = replace(original, translation="完整译文" * 80)
        baseline = processor.render_entries([original]).splitlines()
        result = processor.render_entries([translated]).splitlines()
        self.assertEqual(result[:2], baseline[:2])
        self.assertEqual(len(result), 4)
        self.assertEqual(result[2], original.text.strip())
        self.assertEqual(result[3], translated.translation)

    def test_disabled_matches_reviewed_renderer_for_all_repository_samples(self):
        root = Path(__file__).resolve().parents[1]
        hashes = json.loads((root / "tests" / "fixtures" / "legacy_srt_hashes.json").read_text(encoding="utf-8"))
        for path in sorted((root / "sample").glob("*.json")):
            with self.subTest(sample=path.name):
                data = json.loads(path.read_text(encoding="utf-8"))
                original = copy.deepcopy(data)
                content = create_srt_from_json(copy.deepcopy(data))
                self.assertEqual(hashlib.sha256(content.encode("utf-8")).hexdigest(), hashes[path.name])
                process_subtitles(data, self.source, {"translation_enabled": False})
                self.assertEqual(self.output.read_text(encoding="utf-8"), content)
                self.assertEqual(data, original)

    def test_failure_and_cancellation_preserve_existing_srt(self):
        self.output.write_text("EXISTING SRT", encoding="utf-8")
        def invalid(numbers):
            return "No numbered translations"
        with self.assertRaises(TranslationError):
            process_subtitles(transcript(), self.source, {"translation_api_key": "fake-test-key"},
                              client_factory=self.factory(FakeClient(invalid)))
        self.assertEqual(self.output.read_text(encoding="utf-8"), "EXISTING SRT")
        self.assertTrue(os.path.exists(translation_cache_path(self.source)))
        token = CancellationToken()
        token.cancel()
        with self.assertRaises(ProcessingCancelled):
            process_subtitles(transcript(), self.source, cancellation=token)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "EXISTING SRT")

    def test_complete_cached_results_need_no_key_or_request(self):
        data = transcript()
        entries = SrtProcessor(data).create_entries()
        cache = TranslationCache(translation_cache_path(self.source))
        TranslationCoordinator(FakeClient(), cache).translate_entries(entries, "en", OpenCC("t2s").convert)
        with patch("api.translation_client.requests.post", side_effect=AssertionError("No request needed")) as post:
            process_subtitles(data, self.source)
        post.assert_not_called()
        self.assertTrue(self.output.exists())
        self.assertFalse(os.path.exists(translation_cache_path(self.source)))

    def test_missing_key_is_only_reported_when_request_is_needed(self):
        self.output.write_text("EXISTING SRT", encoding="utf-8")
        with patch("api.translation_client.requests.post") as post, self.assertRaisesRegex(TranslationError, "API Key"):
            process_subtitles(transcript(), self.source)
        post.assert_not_called()
        self.assertEqual(self.output.read_text(encoding="utf-8"), "EXISTING SRT")
        self.assertFalse(os.path.exists(translation_cache_path(self.source)))

    def test_invalid_json_and_failed_atomic_replace_preserve_destination(self):
        self.output.write_text("EXISTING SRT", encoding="utf-8")
        for data in ([], {}, {"words": None}, {"words": []}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                process_subtitles(data, self.source, {"translation_enabled": False})
        with patch("core.atomic_io.os.replace", side_effect=OSError("disk failure")), self.assertRaises(OSError):
            atomic_write_text(self.output, "NEW SRT")
        self.assertEqual(self.output.read_text(encoding="utf-8"), "EXISTING SRT")
        self.assertEqual(list(Path(self.directory.name).glob(".scribe2srt-*")), [])

    def test_cancel_before_atomic_replace_removes_temporary_file(self):
        self.output.write_text("EXISTING SRT", encoding="utf-8")
        token = CancellationToken()
        token.cancel()
        with self.assertRaises(ProcessingCancelled):
            atomic_write_text(self.output, "NEW SRT", token)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "EXISTING SRT")
        self.assertEqual(list(Path(self.directory.name).glob(".scribe2srt-*")), [])

    def test_unknown_script_does_not_assume_chinese(self):
        factory = Mock(side_effect=TranslationError("remote translation selected"))
        with self.assertRaisesRegex(TranslationError, "remote translation selected"):
            process_subtitles(transcript(None, ["體育大學"]), self.source,
                              {"translation_api_key": "fake-test-key"}, client_factory=factory)
        factory.assert_called_once()


if __name__ == "__main__":
    unittest.main()
