"""Batch coordination, strict numbered results, split retries and durable progress."""

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import replace
import hashlib
import json
import os
import re
import threading
import time

from api.translation_client import TranslationError, TranslationFormatError
from .atomic_io import atomic_write_text
from .cancellation import CancellationToken
from .config import TRANSLATION_MAX_CHARACTERS
from .srt_processor import single_line


PROMPT_VERSION = "numbered-subtitles-v1"
PROMPT_INSTRUCTION = (
    "Translate the numbered subtitles below into Simplified Chinese using the context of this batch. "
    "Keep each [000001] identifier exactly once with its own translation. "
    "Do not merge, omit or add entries. Return only numbered translations, without explanations.\n\n"
)


def make_batches(entries, batch_size=10, max_characters=TRANSLATION_MAX_CHARACTERS):
    if not 1 <= batch_size <= 20 or max_characters < 1:
        raise ValueError("无效的翻译组批配置。")
    batches, current, characters = [], [], 0
    for entry in entries:
        size = len(entry.text)
        if current and (len(current) >= batch_size or characters + size > max_characters):
            batches.append(current)
            current, characters = [], 0
        current.append(entry)
        characters += size
        # Oversized entries are sent intact and alone, never truncated.
        if size > max_characters:
            batches.append(current)
            current, characters = [], 0
    if current:
        batches.append(current)
    return batches


def batch_prompt(entries):
    return PROMPT_INSTRUCTION + "\n".join(f"[{entry.number:06d}] {single_line(entry.text)}" for entry in entries)


def parse_numbered_translations(content, entries):
    expected = {f"{entry.number:06d}": entry.number for entry in entries}
    markers = list(re.finditer(r"^\[(\d+)\][ \t]*", content, re.MULTILINE))
    if not markers or content[:markers[0].start()].strip() or "```" in content:
        raise TranslationFormatError("翻译响应包含说明或缺少固定编号。")
    results = {}
    for i, marker in enumerate(markers):
        identifier = marker.group(1)
        if identifier not in expected or expected[identifier] in results:
            raise TranslationFormatError("翻译响应有额外、重复或格式错误的编号。")
        end = markers[i + 1].start() if i + 1 < len(markers) else len(content)
        text = single_line(content[marker.end():end])
        if not text or re.search(r"\[\d+\]", text):
            raise TranslationFormatError("译文为空或编号未独立成行。")
        results[expected[identifier]] = text
    if results.keys() != set(expected.values()):
        raise TranslationFormatError("翻译响应遗漏了字幕编号。")
    return results


def batch_fingerprint(entries, language, settings, max_characters=TRANSLATION_MAX_CHARACTERS):
    context = {
        "entries": [[entry.number, entry.text, entry.is_audio_event] for entry in entries],
        "language": language, "target": "Chinese", "model": settings.model,
        "endpoint": settings.endpoint, "prompt_version": PROMPT_VERSION,
        "batch_size": settings.batch_size, "max_characters": max_characters,
    }
    return hashlib.sha256(json.dumps(context, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def translation_cache_path(source_path):
    return os.path.splitext(source_path)[0] + ".translation-cache.json"


class TranslationCache:
    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self.data = {"schema": 1, "batches": {}}
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and data.get("schema") == 1 and isinstance(data.get("batches"), dict):
                self.data = data
        except (OSError, ValueError):
            pass

    def node(self, fingerprint, branch):
        with self._lock:
            batch = self.data["batches"].get(fingerprint, {})
            return batch.get(branch, {}) if isinstance(batch, dict) else {}

    def save_node(self, fingerprint, branch, value, cancellation):
        if "translations" in value:
            value = {"translations": {str(number): text for number, text in value["translations"].items()}}

        def save():
            with self._lock:
                batch = self.data["batches"].setdefault(fingerprint, {})
                if not isinstance(batch, dict):
                    batch = self.data["batches"][fingerprint] = {}
                batch[branch] = value
                atomic_write_text(self.path, json.dumps(self.data, ensure_ascii=False, indent=2))
        cancellation.commit(save)

    def clear(self):
        with self._lock:
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass


class TranslationCoordinator:
    def __init__(self, client, cache, cancellation=None, progress=None, log=None):
        self.client = client
        self.cache = cache
        self.cancellation = cancellation or CancellationToken()
        self.progress = progress or (lambda done, total: None)
        self.log = log or (lambda message: None)
        self._progress_lock = threading.Lock()
        self._completed = set()
        self._total = 0
        self._stop = CancellationToken()

    def _check(self):
        self.cancellation.check()
        self._stop.check()

    def _accepted(self, results):
        def accept():
            with self._progress_lock:
                self._completed.update(results)
                self.progress(len(self._completed), self._total)
        self._request_token.commit(accept)

    def _translate_batch(self, entries, fingerprint, branch="root"):
        self._check()
        cached = self.cache.node(fingerprint, branch)
        if not isinstance(cached, dict):
            cached = {}
        results = cached.get("translations")
        expected = {str(entry.number) for entry in entries}
        if (isinstance(results, dict) and results.keys() == expected
                and all(isinstance(text, str) and single_line(text) for text in results.values())):
            results = {int(number): single_line(text) for number, text in results.items()}
            self._accepted(results)
            return results

        if cached.get("split") is not True or len(entries) == 1:
            try:
                content = self.client.translate(batch_prompt(entries), self._language, self._request_token)
                self._check()
                results = parse_numbered_translations(content, entries)
                self.cache.save_node(fingerprint, branch, {"translations": results}, self._request_token)
                self._accepted(results)
                return results
            except TranslationFormatError as exc:
                self._check()
                if len(entries) == 1:
                    raise TranslationError(f"字幕 {entries[0].number} 翻译校验失败：{exc} 进度已保留。") from None
                self.log(f"字幕 {entries[0].number}–{entries[-1].number} 编号校验失败，拆为两批重试。")
                self.cache.save_node(fingerprint, branch, {"split": True}, self._request_token)

        middle = len(entries) // 2
        left = self._translate_batch(entries[:middle], fingerprint, branch + "/L")
        right = self._translate_batch(entries[middle:], fingerprint, branch + "/R")
        return {**left, **right}

    def translate_entries(self, entries, language, simplify):
        self._language = language
        self._total = len(entries)
        self._completed.clear()
        self._request_token = _CombinedCancellation(self.cancellation, self._stop)
        self.progress(0, self._total)
        batches = iter(make_batches(entries, self.client.settings.batch_size))
        executor = ThreadPoolExecutor(max_workers=self.client.settings.max_concurrent,
                                      thread_name_prefix="subtitle-translation")
        pending, translations = set(), {}

        def dispatch():
            batch = next(batches, None)
            if batch is None:
                return False
            self._check()
            fingerprint = batch_fingerprint(batch, language, self.client.settings)
            pending.add(executor.submit(self._translate_batch, batch, fingerprint))
            return True

        try:
            for _ in range(self.client.settings.max_concurrent):
                if not dispatch():
                    break
            while pending:
                self._check()
                completed, _ = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
                # Read all completed outcomes before dispatching further batches.
                for future in completed:
                    pending.remove(future)
                    translations.update(future.result())
                for _ in completed:
                    dispatch()
            self._check()
            if translations.keys() != {entry.number for entry in entries}:
                raise TranslationError("翻译条目不完整，已保留进度。")
            return [replace(entry, translation=simplify(translations[entry.number])) for entry in entries]
        finally:
            self._stop.cancel()
            for future in pending:
                future.cancel()
            # In-flight requests have (10, 60) timeouts and discard late results.
            executor.shutdown(wait=False, cancel_futures=True)


class _CombinedCancellation:
    """File cancellation plus a coordinator failure that stops sibling requests."""

    def __init__(self, outer, inner):
        self.outer, self.inner = outer, inner

    def check(self):
        self.outer.check()
        self.inner.check()

    def wait(self, seconds):
        end = time.monotonic() + max(0, seconds)
        while True:
            self.check()
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            self.outer.wait(min(remaining, 0.1))

    def commit(self, action):
        return self.outer.commit(lambda: self.inner.commit(action))
