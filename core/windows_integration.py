"""Manage this application's current-user SendTo shortcut without extra packages."""

import base64
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .app_paths import application_directory
from .windows_shortcut import ShellLink, read_shortcut as _read_shortcut


SHORTCUT_NAME = "Scribe2SRT.lnk"
SHORTCUT_DESCRIPTION = "Scribe2SRT: Windows SendTo"


class IntegrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ShortcutSpec:
    target: str
    arguments: str
    working_directory: str


def shortcut_spec() -> ShortcutSpec:
    directory = application_directory()
    if getattr(sys, "frozen", False):
        return ShortcutSpec(str(Path(sys.executable).resolve()), "--send-to --", str(directory))
    # Keep a virtual environment's executable path, even when it is a symlink.
    pythonw = Path(sys.executable).absolute().with_name("pythonw.exe")
    if not pythonw.is_file():
        raise IntegrationError(f"当前 Python 环境中找不到 pythonw.exe：{pythonw}")
    return ShortcutSpec(str(pythonw), subprocess.list2cmdline(
        [str(directory / "app.py"), "--send-to", "--"]), str(directory))


def _ps_string(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _run_powershell(script: str) -> str:
    if sys.platform != "win32":
        raise IntegrationError("“发送到”集成仅支持 Windows。")
    script = ("$ErrorActionPreference = 'Stop'\n"
              "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n" + script)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-EncodedCommand", encoded], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=20,
            startupinfo=startupinfo, creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IntegrationError(f"无法调用 PowerShell：{exc}") from exc
    if result.returncode:
        raise IntegrationError(f"无法更新“发送到”快捷方式：{result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def _shortcut_path(directory=None) -> Path:
    if sys.platform != "win32":
        raise IntegrationError("“发送到”集成仅支持 Windows。")
    if directory is None:
        directory = _run_powershell(
            "[Console]::WriteLine([Environment]::GetFolderPath([Environment+SpecialFolder]::SendTo))\n")
        if not directory:
            raise IntegrationError("无法找到当前用户的 SendTo 目录。")
    return Path(os.path.abspath(directory)) / SHORTCUT_NAME


def _check_owner(path):
    if path.exists() and _read_shortcut(path)["description"] != SHORTCUT_DESCRIPTION:
        raise IntegrationError("同名快捷方式不属于本程序，请先检查 Scribe2SRT.lnk。")


def add_send_to_shortcut(directory=None) -> str:
    spec = shortcut_spec()
    path = _shortcut_path(directory)
    try:
        _check_owner(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with ShellLink() as link:
            if path.exists():
                link.load(path)
            link.configure(spec, SHORTCUT_DESCRIPTION)
            link.save(path)
        if not path.is_file():
            raise OSError("快捷方式保存失败。")
    except OSError as exc:
        raise IntegrationError(f"无法更新“发送到”快捷方式：{exc}") from exc
    return str(path)


def remove_send_to_shortcut(directory=None) -> str:
    path = _shortcut_path(directory)
    try:
        _check_owner(path)
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise IntegrationError(f"无法更新“发送到”快捷方式：{exc}") from exc
    return str(path)
