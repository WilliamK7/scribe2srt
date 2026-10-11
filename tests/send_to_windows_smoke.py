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


def terminate_process_tree(pid):
    """Kill only the timed-out process we launched and its bundle children."""
    subprocess.run(["taskkill.exe", "/PID", str(pid), "/T", "/F"],
                   capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)


def run_direct(command, cwd, env=None):
    with subprocess.Popen(command, cwd=cwd, env=env,
                          creationflags=subprocess.CREATE_NO_WINDOW) as process:
        try:
            code = process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            terminate_process_tree(process.pid)
            process.wait(timeout=10)
            raise AssertionError("Direct SendTo process did not automatically exit")
    assert code == 0, f"SendTo process exited with {code}"


def wait_for_exit(handle, timeout=60):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.GetProcessId.argtypes = [wintypes.HANDLE]
    kernel.GetProcessId.restype = wintypes.DWORD
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
        terminate_process_tree(kernel.GetProcessId(handle))
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


def check_files(paths, originals, expected="繁體中文"):
    for path, original in zip(paths, originals):
        assert path.read_bytes() == original, f"source JSON changed: {path}"
        subtitle = path.with_suffix(".srt").read_text(encoding="utf-8")
        assert expected in subtitle, f"unexpected subtitle text: {subtitle}"
        assert "-->" in subtitle, f"subtitle not replaced: {path}"
        for suffix in (".transcription-cache.json", ".translation-cache.json"):
            assert not path.with_suffix(suffix).exists(), f"task cache left behind: {path}"


def run_smoke(executable, send_to_directory=None):
    if sys.platform != "win32":
        raise RuntimeError("This smoke test requires Windows")
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    send_to = (Path(send_to_directory).resolve() if send_to_directory is not None else
               Path(integration._run_powershell(
                   "[Console]::WriteLine([Environment]::GetFolderPath([Environment+SpecialFolder]::SendTo))\n")))
    send_to.mkdir(parents=True, exist_ok=True)
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
                    integration.add_send_to_shortcut(directory=send_to_directory)
                    installed = True
                    modified = True
                    integration.add_send_to_shortcut(directory=send_to_directory)  # Update it.
                for count in (1, 3):
                    paths = [inputs / f"{name} 中文 空格 {count}-{number}.json" for number in range(count)]
                    originals = [fixture(path) for path in paths]
                    wait_for_exit(shell_launch(shortcut, paths))
                    check_files(paths, originals)
                    print(f"{name}: shell SendTo {count} file(s), output and auto-exit OK", flush=True)
                direct = inputs / f"{name} different cwd.json"
                original = fixture(direct)
                command = ([pythonw, str(source / "app.py")] if name == "source" else [spec.target])
                run_direct(command + ["--send-to", "--", str(direct)], cwd=inputs)
                check_files([direct], [original])
                print(f"{name}: different working directory settings OK", flush=True)
            integration.remove_send_to_shortcut(directory=send_to_directory)
            assert not shortcut.exists(), "shortcut removal failed"
            installed = False
        finally:
            if installed:
                integration.remove_send_to_shortcut(directory=send_to_directory)
            if previous is not None and modified:
                shortcut.write_bytes(previous)
        # Default configuration must work without credentials or external runtimes.
        assert not (executable.parent / "settings.json").exists(), "default smoke requires a clean bundle"
        default_input = inputs / "默认 配置 繁体.json"
        original = fixture(default_input)
        environment = os.environ.copy()
        # Only Windows system tools remain available, never Python or FFmpeg.
        windows = Path(os.environ["SystemRoot"])
        environment["PATH"] = os.pathsep.join(str(windows / part) for part in
                                              ("System32", "System32/WindowsPowerShell/v1.0"))
        for program in ("python", "pythonw", "ffmpeg", "ffprobe"):
            assert shutil.which(program, path=environment["PATH"]) is None, program
        run_direct([str(executable), "--send-to", "--", str(default_input)],
                   cwd=inputs, env=environment)
        check_files([default_input], [original], expected="这个字幕是繁体中文")
        assert "繁體中文" not in default_input.with_suffix(".srt").read_text(encoding="utf-8")
        assert not (executable.parent / "settings.json").exists(), "smoke wrote default configuration"
        print("bundle: default Chinese/OpenCC, no keys/Python/FFmpeg, different cwd OK", flush=True)
    print("Windows source/bundle SendTo smoke: OK", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", required=True, type=Path)
    parser.add_argument("--send-to-directory", type=Path,
                        help="Use an isolated shortcut directory instead of the user's SendTo folder")
    args = parser.parse_args()
    run_smoke(args.executable.resolve(strict=True), args.send_to_directory)
