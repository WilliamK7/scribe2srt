"""Manage this application's current-user SendTo shortcut without extra packages."""

import base64
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .app_paths import application_directory


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


def _shortcut_script(directory=None) -> str:
    location = (_ps_string(os.path.abspath(directory)) if directory is not None else
                "[Environment]::GetFolderPath([Environment+SpecialFolder]::SendTo)")
    return (f"$directory = {location}\n"
            "if (-not $directory) { throw '无法找到当前用户的 SendTo 目录。' }\n"
            f"$path = Join-Path $directory {_ps_string(SHORTCUT_NAME)}\n"
            "$shell = New-Object -ComObject WScript.Shell\n"
            "if (Test-Path -LiteralPath $path) {\n"
            "    $existing = $shell.CreateShortcut($path)\n"
            f"    if ($existing.Description -ne {_ps_string(SHORTCUT_DESCRIPTION)}) {{\n"
            "        throw '同名快捷方式不属于本程序，请先检查 Scribe2SRT.lnk。'\n"
            "    }\n"
            "}\n")


def add_send_to_shortcut(directory=None) -> str:
    spec = shortcut_spec()
    script = _shortcut_script(directory)
    script += ("[IO.Directory]::CreateDirectory($directory) | Out-Null\n"
               "$shortcut = $shell.CreateShortcut($path)\n"
               f"$shortcut.TargetPath = {_ps_string(spec.target)}\n"
               f"$shortcut.Arguments = {_ps_string(spec.arguments)}\n"
               f"$shortcut.WorkingDirectory = {_ps_string(spec.working_directory)}\n"
               f"$shortcut.Description = {_ps_string(SHORTCUT_DESCRIPTION)}\n"
               f"$shortcut.IconLocation = {_ps_string(spec.target + ',0')}\n"
               "$shortcut.WindowStyle = 1\n"
               "$shortcut.Save()\n"
               "if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw '快捷方式保存失败。' }\n"
               "[Console]::WriteLine($path)\n")
    return _run_powershell(script)


def remove_send_to_shortcut(directory=None) -> str:
    return _run_powershell(_shortcut_script(directory) +
                           "if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path }\n"
                           "[Console]::WriteLine($path)\n")
