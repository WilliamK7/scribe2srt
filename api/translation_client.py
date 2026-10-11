"""Non-streaming Qwen-MT requests to official Alibaba Cloud endpoints."""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import re
import threading
import time

import requests

from core.cancellation import CancellationToken
from core.config import DEFAULT_TRANSLATION_SETTINGS
from core.languages import qwen_language_name


class TranslationError(Exception):
    pass


class TranslationFormatError(TranslationError):
    """A response that can be retried by splitting the batch."""


_HOSTS = {
    "cn-beijing": "dashscope.aliyuncs.com",
    "ap-southeast-1": "dashscope-intl.aliyuncs.com",
    "cn-hongkong": "cn-hongkong.dashscope.aliyuncs.com",
}


def official_endpoint(region, workspace_id=""):
    if region not in _HOSTS:
        raise ValueError("请选择支持的阿里云官方地域。")
    workspace_id = workspace_id.strip()
    if workspace_id and not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", workspace_id):
        raise ValueError("业务空间 ID 只能包含字母、数字和连字符。")
    host = f"{workspace_id}.{region}.maas.aliyuncs.com" if workspace_id else _HOSTS[region]
    return f"https://{host}/compatible-mode/v1/chat/completions"


@dataclass(frozen=True)
class TranslationSettings:
    api_key: str = field(default="", repr=False)
    region: str = "cn-beijing"
    workspace_id: str = ""
    model: str = "qwen-mt-flash"
    batch_size: int = 10
    max_concurrent: int = 3
    requests_per_minute: int = 60

    @classmethod
    def from_dict(cls, settings):
        values = {**DEFAULT_TRANSLATION_SETTINGS, **(settings or {})}
        return cls(values["translation_api_key"].strip(), values["translation_region"],
                   values["translation_workspace_id"].strip(), values["translation_model"].strip(),
                   values["translation_batch_size"], values["translation_max_concurrent"],
                   values["translation_rate_limit_per_minute"])

    @property
    def endpoint(self):
        return official_endpoint(self.region, self.workspace_id)

    def validate(self, require_key=True):
        self.endpoint
        if not re.fullmatch(r"qwen-mt-[A-Za-z0-9][A-Za-z0-9._-]*", self.model):
            raise ValueError("模型名称必须是官方 qwen-mt-* 文本翻译型号。")
        for value, lower, upper, name in (
            (self.batch_size, 1, 20, "每批条数"),
            (self.max_concurrent, 1, 20, "翻译并发数"),
            (self.requests_per_minute, 1, 6000, "翻译请求频率"),
        ):
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"{name}必须在 {lower}–{upper} 之间。")
        if require_key and not self.api_key:
            raise TranslationError("外语字幕需要翻译 API Key，请在“翻译设置”中填写，或关闭自动翻译。")


class RequestRateLimiter:
    """A thread-safe rolling window shared across files, splits and retries."""

    def __init__(self, requests_per_minute=60, clock=time.monotonic, max_concurrent=3):
        if type(requests_per_minute) is not int or requests_per_minute < 1:
            raise ValueError("翻译请求频率必须是正整数。")
        if type(max_concurrent) is not int or max_concurrent < 1:
            raise ValueError("翻译并发数必须是正整数。")
        self.requests_per_minute = requests_per_minute
        self._clock = clock
        self._requests = deque()
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(max_concurrent)

    def acquire_slot(self, cancellation):
        while True:
            cancellation.check()
            if self._slots.acquire(timeout=0.1):
                return

    def release_slot(self):
        self._slots.release()

    def acquire(self, cancellation):
        while True:
            cancellation.check()
            with self._lock:
                now = self._clock()
                while self._requests and now - self._requests[0] >= 60:
                    self._requests.popleft()
                if len(self._requests) < self.requests_per_minute:
                    cancellation.check()
                    self._requests.append(now)
                    return
                delay = max(0.01, 60 - (now - self._requests[0]))
            cancellation.wait(min(delay, 0.2))


def retry_after_seconds(value):
    if not value:
        return 0
    try:
        seconds = float(value)
        return max(0, seconds) if math.isfinite(seconds) else 0
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0


class QwenTranslationClient:
    def __init__(self, settings, rate_limiter=None, post=None, log=None):
        settings.validate(require_key=False)
        self.settings = settings
        self.rate_limiter = rate_limiter or RequestRateLimiter(settings.requests_per_minute,
                                                              max_concurrent=settings.max_concurrent)
        self._post = post or requests.post
        self._log = log or (lambda message: None)

    def translate(self, prompt, language, cancellation=None):
        cancellation = cancellation or CancellationToken()
        cancellation.check()
        self.settings.validate()  # Cached results and local Chinese conversion need no key.
        payload = {
            "model": self.settings.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "translation_options": {"source_lang": qwen_language_name(language), "target_lang": "Chinese"},
        }
        for attempt in range(4):  # Initial attempt plus at most three network retries.
            response = None
            acquired_slot = False
            try:
                self.rate_limiter.acquire_slot(cancellation)
                acquired_slot = True
                self.rate_limiter.acquire(cancellation)
                cancellation.check()
                response = self._post(
                    self.settings.endpoint, json=payload,
                    headers={"Authorization": f"Bearer {self.settings.api_key}",
                             "Content-Type": "application/json"},
                    timeout=(10, 60), allow_redirects=False,
                )
                cancellation.check()  # Ignore responses arriving after cancellation.
                status = response.status_code
                if status == 429 or 500 <= status <= 599:
                    if attempt == 3:
                        raise TranslationError(f"翻译服务 HTTP {status}，网络重试已耗尽；进度已保留。")
                    delay = max(2 ** attempt, retry_after_seconds(response.headers.get("Retry-After")))
                elif status != 200:
                    explanation = {401: "鉴权失败，请检查 API Key 和地域", 403: "无访问权限",
                                   400: "参数或输入不被模型接受", 404: "模型或业务空间不可用"}
                    raise TranslationError(f"翻译服务 HTTP {status}：{explanation.get(status, '请求被拒绝')}。")
                else:
                    try:
                        data = response.json()
                        choice = data["choices"][0]
                        content = choice["message"]["content"]
                    except (ValueError, KeyError, IndexError, TypeError):
                        raise TranslationFormatError("翻译响应缺少有效的文本结果。") from None
                    if choice.get("finish_reason") != "stop":
                        raise TranslationFormatError("翻译响应未正常结束或已截断。")
                    if not isinstance(content, str) or not content.strip():
                        raise TranslationFormatError("翻译响应为空。")
                    return content
            except (requests.Timeout, requests.ConnectionError):
                cancellation.check()
                if attempt == 3:
                    raise TranslationError("翻译请求超时或连接失败，网络重试已耗尽；进度已保留。") from None
                delay = 2 ** attempt
            except requests.RequestException:
                # Never print exception text, request headers, response bodies or credentials.
                raise TranslationError("翻译请求失败，请检查网络和官方服务配置。") from None
            finally:
                try:
                    if response is not None:
                        response.close()
                finally:
                    if acquired_slot:
                        self.rate_limiter.release_slot()
            self._log(f"翻译请求暂时失败，将在 {delay:g} 秒后重试（{attempt + 1}/3）。")
            cancellation.wait(delay)
