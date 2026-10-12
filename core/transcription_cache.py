"""Temporary transcription checkpoints for resuming unfinished subtitle work."""

import json
import os

from .atomic_io import atomic_write_text
from .languages import normalize_language


def transcription_cache_path(source_path):
    # Keep the media extension: movie.mp3 and movie.mp4 are different inputs.
    return os.fspath(source_path) + ".transcription-cache.json"


def transcription_identity(source_path, language_code, tag_audio_events, split_duration_min):
    from api.client import DEFAULT_STT_MODEL_ID

    path = os.path.normcase(os.path.realpath(os.path.abspath(source_path)))
    stat = os.stat(path)
    return {
        "source": {"path": path, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns},
        "options": {"model_id": DEFAULT_STT_MODEL_ID, "language": normalize_language(language_code),
                    "tag_audio_events": bool(tag_audio_events), "diarize": True,
                    "split_duration_sec": split_duration_min * 60},
    }


def load_transcription_checkpoint(source_path, identity):
    """Only resume a versioned checkpoint belonging to this input and request."""
    try:
        with open(transcription_cache_path(source_path), encoding="utf-8") as handle:
            checkpoint = json.load(handle)
    except (OSError, ValueError, UnicodeError):
        return None
    if (isinstance(checkpoint, dict) and checkpoint.get("schema") == 1
            and checkpoint.get("kind") == "scribe2srt-transcription"
            and checkpoint.get("identity") == identity):
        transcript = checkpoint.get("transcript")
        if (isinstance(transcript, dict) and isinstance(transcript.get("words"), list)
                and all(isinstance(word, dict) for word in transcript["words"])):
            return transcript
    return None


def save_transcription_checkpoint(source_path, identity, transcript, cancellation=None):
    checkpoint = {"schema": 1, "kind": "scribe2srt-transcription",
                  "identity": identity, "transcript": transcript}
    atomic_write_text(transcription_cache_path(source_path),
                      json.dumps(checkpoint, ensure_ascii=False, indent=2), cancellation)
