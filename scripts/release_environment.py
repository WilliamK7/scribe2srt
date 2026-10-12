"""Validate the interpreter used by every Windows release stage."""

from pathlib import Path
import platform
import re
import struct
import sys
import sysconfig

ROOT = Path(__file__).resolve().parent.parent


def validate_release_environment():
    pinned = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", pinned):
        raise RuntimeError(".python-version must pin a stable CPython patch version")
    expected = tuple(int(part) for part in pinned.split("."))
    if (sys.platform != "win32" or sys.implementation.name != "cpython"
            or sys.version_info[:3] != expected or sys.version_info.releaselevel != "final"
            or struct.calcsize("P") != 8 or platform.machine().lower() not in {"amd64", "x86_64"}
            or sysconfig.get_config_var("Py_GIL_DISABLED")):
        raise RuntimeError(f"Use standard Windows x64 CPython {pinned} from .python-version")
    print(f"Release environment: Windows x64 CPython {pinned}", flush=True)


if __name__ == "__main__":
    validate_release_environment()
