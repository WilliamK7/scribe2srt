"""The common subtitle stage for imported JSON and all media transcription modes."""

from dataclasses import replace
import os

from api.translation_client import QwenTranslationClient, TranslationSettings
from .atomic_io import atomic_write_text
from .cancellation import CancellationToken
from .languages import source_language
from .srt_processor import SrtProcessor
from .translation import TranslationCache, TranslationCoordinator, translation_cache_path


def process_subtitles(json_data, source_path, settings=None, selected_language="auto",
                      max_subtitle_duration=None, cancellation=None, progress=None,
                      log=None, rate_limiter=None, client_factory=None):
    settings = settings or {}
    cancellation = cancellation or CancellationToken()
    log = log or (lambda message: None)
    progress = progress or (lambda done, total: None)
    cancellation.check()
    if not isinstance(json_data, dict) or not isinstance(json_data.get("words"), list):
        raise ValueError("JSON 文件必须包含 ElevenLabs 格式的 words 列表。")
    language = source_language(json_data, selected_language)
    # A shallow copy is sufficient: SrtProcessor copies every word before processing.
    processing_data = dict(json_data)
    if settings.get("translation_enabled", True) and language != "auto":
        processing_data["language_code"] = language
    processor = SrtProcessor(processing_data, max_subtitle_duration, settings)
    entries = processor.create_entries()
    cancellation.check()
    if not entries:
        raise ValueError("JSON 文件为空或不包含有效的字幕条目。")
    cache = TranslationCache(translation_cache_path(source_path))
    if settings.get("translation_enabled", True):
        from opencc import OpenCC
        simplify = OpenCC("t2s").convert
        if language == "zh":
            log("中文字幕：在本地转换为简体中文。")
            entries = [replace(entry, text=simplify(entry.text)) for entry in entries]
            progress(len(entries), len(entries))
        else:
            config = TranslationSettings.from_dict(settings)
            config.validate(require_key=False)
            log(f"开始翻译 {len(entries)} 条字幕，模型 {config.model}，每批最多 {config.batch_size} 条。")
            client = (client_factory or QwenTranslationClient)(config, rate_limiter=rate_limiter, log=log)
            entries = TranslationCoordinator(client, cache, cancellation, progress, log).translate_entries(
                entries, language, simplify)
    else:
        progress(len(entries), len(entries))
    cancellation.check()
    content = processor.render_entries(entries)
    output_path = os.path.splitext(source_path)[0] + ".srt"
    atomic_write_text(output_path, content, cancellation)
    try:
        cache.clear()
    except OSError:
        log("字幕已保存，翻译缓存未能清理，可手动删除。")
    log(f"最终 SRT 字幕文件已保存到:\n{output_path}")
    return output_path
