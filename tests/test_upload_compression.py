"""Exercise Requests' real upload/decompression path without external services."""

import gzip
import gc
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from api import client


class UploadCompressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_uploader_advertises_supported_encodings_and_decodes_gzip_json(self):
        # Earlier GUI tests leave Python reference cycles. Collect them on the
        # Qt thread before HTTP server threads can trigger garbage collection.
        gc.collect()
        observed = []
        transcription = {"language_code": "zho", "text": "繁體字幕", "words": []}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                encodings = [value.strip().lower() for value in
                             self.headers.get("Accept-Encoding", "").split(",")]
                observed.append((self.path, encodings, body))
                # Reject unsupported declarations rather than silently choosing gzip.
                if set(encodings) != {"gzip", "deflate"}:
                    self.send_error(406, "Only gzip and deflate are supported")
                    return
                compressed = gzip.compress(json.dumps(transcription).encode("utf-8"))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(compressed)))
                self.end_headers()
                self.wfile.write(compressed)

            def log_message(self, *args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever,
                                      kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()
            try:
                with tempfile.TemporaryDirectory() as temporary:
                    audio = Path(temporary) / "离线 上传.wav"
                    audio.write_bytes(b"offline audio fixture")
                    url = f"http://127.0.0.1:{server.server_port}/speech-to-text"
                    stt = client.ElevenLabsSTTClient()
                    with patch.object(stt, "log_media_info"), patch.object(
                            client, "ELEVENLABS_STT_API_URL", url):
                        uploader = stt.prepare_upload_task(str(audio), "zh", False)
                        finished, errors = [], []
                        uploader.signals.finished.connect(finished.append)
                        uploader.signals.error.connect(errors.append)
                        try:
                            uploader.run()
                        finally:
                            uploader.session.close()
                    self.assertEqual(errors, [])
                    self.assertEqual(finished, [transcription])
                    self.assertEqual(len(observed), 1)
                    path, encodings, body = observed[0]
                    self.assertEqual(path, "/speech-to-text?allow_unauthenticated=1")
                    self.assertEqual(encodings, ["gzip", "deflate"])
                    self.assertIn(audio.read_bytes(), body)
            finally:
                server.shutdown()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
