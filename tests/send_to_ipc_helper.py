"""Separate processes for IPC tests, with no GUI, transcription or remote API."""

import argparse
import json
import os
import time
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QTimer

from core.single_instance import InstanceError, SingleInstance


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True)
    parser.add_argument("--files", required=True)
    parser.add_argument("--mode", choices=["race", "forward"], default="forward")
    parser.add_argument("--barrier")
    parser.add_argument("--receipts")
    parser.add_argument("--expected", type=int, default=1)
    parser.add_argument("--timeout-ms", type=int, default=5000)
    args = parser.parse_args()
    app = QCoreApplication([])
    instance = SingleInstance(args.directory, timeout_ms=args.timeout_ms)
    if args.barrier:
        deadline = time.monotonic() + 10
        while not Path(args.barrier).exists():
            if time.monotonic() > deadline:
                raise RuntimeError("start barrier timed out")
            time.sleep(0.01)
    try:
        files = json.loads(args.files)
        owner = instance.acquire_or_forward(True, files)
        if owner and args.mode == "race":
            receipts = []
            def receive(send_to, paths):
                receipts.append({"send_to": send_to, "files": paths})
                Path(args.receipts).write_text(json.dumps(receipts, ensure_ascii=False), encoding="utf-8")
                if len(receipts) == args.expected:
                    QTimer.singleShot(100, app.quit)
                return {"ok": True}
            instance.handler = receive
            QTimer.singleShot(0, lambda: receive(True, files))
            QTimer.singleShot(args.timeout_ms + 1000, app.quit)
            app.exec()
            if len(receipts) != args.expected:
                raise InstanceError(f"only received {len(receipts)}/{args.expected} requests")
        print(json.dumps({"status": "owner" if owner else "forwarded", "pid": os.getpid()}))
        return 0
    except InstanceError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1
    finally:
        instance.close()


if __name__ == "__main__":
    raise SystemExit(main())
