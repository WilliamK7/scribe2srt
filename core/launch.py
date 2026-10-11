"""Command line and file identity shared by startup and the task queue."""

import argparse
import ntpath
import os
import sys
from dataclasses import dataclass
from typing import List


class LaunchError(ValueError):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise LaunchError(message)


@dataclass
class LaunchArguments:
    send_to: bool
    files: List[str]


def absolute_path(path: str) -> str:
    return os.path.abspath(path)


def path_key(path: str) -> str:
    path = absolute_path(path)
    return ntpath.normcase(path) if sys.platform == "win32" else os.path.normcase(path)


def parse_launch_arguments(argv=None) -> LaunchArguments:
    parser = _ArgumentParser(description="Scribe2SRT 音视频转字幕")
    parser.add_argument("--send-to", action="store_true", help="接收 Windows 发送到的文件")
    parser.add_argument("files", nargs="*", help="文件路径（可在 -- 后传入）")
    args = parser.parse_args(argv)
    if args.send_to and not args.files:
        parser.error("--send-to 至少需要一个文件路径")
    if args.files and not args.send_to:
        parser.error("文件路径需使用 --send-to -- <文件路径...> 传入")
    return LaunchArguments(args.send_to, [absolute_path(path) for path in args.files])
