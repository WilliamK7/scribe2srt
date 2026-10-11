"""Temporary transcription checkpoints for resuming unfinished subtitle work."""

import os

from .translation import translation_cache_path


def transcription_cache_path(source_path):
    return os.path.splitext(source_path)[0] + ".transcription-cache.json"


def resumable_transcription_path(source_path):
    source_mtime = os.path.getmtime(source_path)
    candidates = [transcription_cache_path(source_path)]
    # Older versions kept the transcript beside a translation checkpoint.
    if os.path.exists(translation_cache_path(source_path)):
        candidates.append(os.path.splitext(source_path)[0] + ".json")
    for path in candidates:
        try:
            if os.path.getmtime(path) >= source_mtime:
                return path
        except FileNotFoundError:
            pass
    return None
