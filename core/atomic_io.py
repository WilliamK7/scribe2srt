"""Write beside the destination, then replace it only after a complete write."""

import os
import tempfile


def atomic_write_text(path, content, cancellation=None):
    path = os.path.abspath(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=os.path.dirname(path), prefix=".scribe2srt-",
                                         suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if cancellation:
            cancellation.commit(lambda: os.replace(temporary, path))
        else:
            os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)
