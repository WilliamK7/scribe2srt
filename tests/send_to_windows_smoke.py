"""Exercise the real Windows shell shortcut with isolated source/bundle copies.

The current user's Scribe2SRT shortcut is restored byte-for-byte afterwards.
Only local JSON is processed; no transcription or translation service is used.
"""

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import windows_integration as integration


class ShellExecuteInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
                ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
                ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR),
                ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
                ("hIcon", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]


def shell_launch(shortcut, files):
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
    shell.ShellExecuteExW.restype = wintypes.BOOL
    request = ShellExecuteInfo()
    request.cbSize = ctypes.sizeof(request)
    request.fMask = 0x40 | 0x100  # SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    request.lpVerb = "open"
    request.lpFile = str(shortcut)
    request.lpParameters = subprocess.list2cmdline([str(path) for path in files])
    request.nShow = 0
    if not shell.ShellExecuteExW(ctypes.byref(request)) or not request.hProcess:
        raise ctypes.WinError(ctypes.get_last_error())
    return request.hProcess


def wait_for_exit(handle, timeout=30):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if kernel.WaitForSingleObject(handle, 100) == 0:
                code = wintypes.DWORD()
                if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                    raise ctypes.WinError(ctypes.get_last_error())
                if code.value:
                    raise AssertionError(f"SendTo process exited with {code.value}")
                return
        kernel.TerminateProcess(handle, 1)
        kernel.WaitForSingleObject(handle, 3000)
        raise AssertionError("SendTo process did not automatically exit")
    finally:
        kernel.CloseHandle(handle)


def fixture(path):
    path.write_text(json.dumps({"language_code": "zho", "text": "這個字幕是繁體中文。",
                               "words": [{"text": "這個字幕是繁體中文。", "type": "word",
                                          "start": 0, "end": 3}]}, ensure_ascii=False), encoding="utf-8")
    path.with_suffix(".srt").write_text("existing subtitle", encoding="utf-8")
    return path.read_bytes()


def check_files(paths, originals):
    for path, original in zip(paths, originals):
        assert path.read_bytes() == original, f"source JSON changed: {path}"
        subtitle = path.with_suffix(".srt").read_text(encoding="utf-8")
        assert "繁體中文" in subtitle, f"saved translation_enabled=false was not loaded: {subtitle}"
        assert "-->" in subtitle, f"subtitle not replaced: {path}"


def run_smoke(executable):
    if sys.platform != "win32":
        raise RuntimeError("This smoke test requires Windows")
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    send_to = Path(integration._run_powershell(
        "[Console]::WriteLine([Environment]::GetFolderPath([Environment+SpecialFolder]::SendTo))\n"))
    shortcut = send_to / integration.SHORTCUT_NAME
    previous = shortcut.read_bytes() if shortcut.exists() else None
    with tempfile.TemporaryDirectory(prefix="Scribe2SRT 发送到 '") as temporary:
        directory = Path(temporary)
        source = directory / "源码 程序"
        source.mkdir()
        shutil.copy2(ROOT / "app.py", source / "app.py")
        for name in ("api", "core", "ui"):
            shutil.copytree(ROOT / name, source / name, ignore=shutil.ignore_patterns("__pycache__"))
        bundle = directory / "打包 程序"
        bundle.mkdir()
        shutil.copy2(executable, bundle / "Scribe2SRT.exe")
        inputs = directory / "输入 文件"
        inputs.mkdir()
        # If the app loads its CWD instead of its own directory, it cannot auto-exit.
        (inputs / "settings.json").write_text("invalid cwd configuration", encoding="utf-8")
        for location in (source, bundle):
            (location / "settings.json").write_text(json.dumps({"translation_enabled": False,
                "source_language": "ja", "tag_audio_events": True}), encoding="utf-8")
        pythonw = str(Path(sys.executable).resolve().with_name("pythonw.exe"))
        specs = [("source", integration.ShortcutSpec(pythonw,
                   subprocess.list2cmdline([str(source / "app.py"), "--send-to", "--"]), str(source))),
                 ("bundle", integration.ShortcutSpec(str(bundle / "Scribe2SRT.exe"), "--send-to --", str(bundle)))]
        installed = False
        modified = False
        try:
            for name, spec in specs:
                with patch.object(integration, "shortcut_spec", return_value=spec):
                    integration.add_send_to_shortcut()
                    installed = True
                    modified = True
                    integration.add_send_to_shortcut()  # Update the existing shortcut.
                for count in (1, 3):
                    paths = [inputs / f"{name} 中文 空格 {count}-{number}.json" for number in range(count)]
                    originals = [fixture(path) for path in paths]
                    wait_for_exit(shell_launch(shortcut, paths))
                    check_files(paths, originals)
                    print(f"{name}: shell SendTo {count} file(s), output and auto-exit OK", flush=True)
                direct = inputs / f"{name} different cwd.json"
                original = fixture(direct)
                command = ([pythonw, str(source / "app.py")] if name == "source" else [spec.target])
                result = subprocess.run(command + ["--send-to", "--", str(direct)], cwd=inputs, timeout=30,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
                assert result.returncode == 0, result.returncode
                check_files([direct], [original])
                print(f"{name}: different working directory settings OK", flush=True)
            integration.remove_send_to_shortcut()
            assert not shortcut.exists(), "shortcut removal failed"
            installed = False
        finally:
            if installed:
                integration.remove_send_to_shortcut()
            if previous is not None and modified:
                shortcut.write_bytes(previous)
    print("Windows source/bundle SendTo smoke: OK", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", required=True, type=Path)
    args = parser.parse_args()
    run_smoke(args.executable.resolve(strict=True))
