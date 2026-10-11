"""Stage, inspect, ZIP, extract and exercise the freshly built Windows EXE."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import sys
import tempfile
import zipfile

from PyInstaller.archive.readers import CArchiveReader
import pefile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.collect_licenses import collect_licenses
from tests.send_to_windows_smoke import run_smoke


def inspect_executable(executable):
    archive = CArchiveReader(str(executable))
    names = {name.replace("\\", "/") for name in archive.toc}
    required = {"opencc/config/t2s.json", "opencc/dictionary/TSCharacters.txt",
                "opencc/dictionary/TSPhrases.txt", "opencc/NOTICE.txt",
                "PySide6/plugins/platforms/qwindows.dll", "certifi/cacert.pem"}
    if missing := required - names:
        raise RuntimeError(f"Missing bundle runtime data: {sorted(missing)}")
    if any(Path(name).name.lower() == "settings.json" for name in names):
        raise RuntimeError("Local settings.json must never be embedded in the EXE")
    if any("virtualkeyboard" in name.lower() or name.lower().endswith("/qpdf.dll") for name in names):
        raise RuntimeError("Unused Qt Virtual Keyboard/PDF plugin was collected")
    print("EXE contains Qt Windows plugin, OpenCC dictionaries and HTTPS certificates; config excluded", flush=True)
    native_versions = []
    for name in sorted(archive.toc):
        if not name.lower().endswith((".dll", ".pyd")):
            continue
        binary = pefile.PE(data=archive.extract(name), fast_load=True)
        try:
            binary.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]])
            info = getattr(binary, "VS_FIXEDFILEINFO", [])
            version = None
            if info:
                version = ".".join(str(value) for value in
                                   (info[0].FileVersionMS >> 16, info[0].FileVersionMS & 0xffff,
                                    info[0].FileVersionLS >> 16, info[0].FileVersionLS & 0xffff))
            native_versions.append({"file": name.replace("\\", "/"), "file_version": version})
        finally:
            binary.close()
    return names, native_versions


def package(executable, artifact_id, output_directory):
    if sys.platform != "win32":
        raise RuntimeError("Windows packaging and shell validation require Windows")
    if sys.version_info[:2] != (3, 11) or struct.calcsize("P") != 8:
        raise RuntimeError("Use the Windows x64 Python 3.11 release environment")
    if not re.fullmatch(r"(?:v\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?|[0-9a-f]{7,40})", artifact_id):
        raise ValueError("Artifact ID must be a version tag or commit SHA")
    names, native_versions = inspect_executable(executable)
    output_directory.mkdir(parents=True, exist_ok=True)
    asset_name = f"Scribe2SRT-{artifact_id}-windows-x86_64.zip"
    final_zip = output_directory / asset_name
    checksum = output_directory / (asset_name + ".sha256")
    # A unique staging root keeps old builds out of the current ZIP. Leave any
    # previous published files alone until the replacement has passed validation.
    (ROOT / "build").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="release-package-", dir=ROOT / "build") as temporary:
        directory = Path(temporary)
        staging = directory / "staging"
        staging.mkdir()
        shutil.copy2(executable, staging / "Scribe2SRT.exe")
        for filename in ("README.md", "settings.example.json"):
            shutil.copy2(ROOT / filename, staging / filename)
        example = json.loads((staging / "settings.example.json").read_text(encoding="utf-8"))
        if example.get("translation_api_key"):
            raise RuntimeError("Example configuration contains a key")
        collect_licenses(staging, names, native_versions)
        if (staging / "settings.json").exists():
            raise RuntimeError("Local settings must not be staged")
        pending_zip = directory / asset_name
        with zipfile.ZipFile(pending_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(staging).as_posix())
        extracted = directory / "extracted 中文 空格"
        with zipfile.ZipFile(pending_zip) as archive:
            if archive.testzip() is not None:
                raise RuntimeError("ZIP integrity check failed")
            archive.extractall(extracted)
        if not (extracted / "Scribe2SRT.exe").is_file():
            raise RuntimeError("ZIP must contain the executable directly at its root")
        if (extracted / "Scribe2SRT.exe").read_bytes() != executable.read_bytes():
            raise RuntimeError("Extracted EXE differs from the current build")
        # The default-settings Chinese case launches this exact extracted EXE.
        run_smoke(extracted / "Scribe2SRT.exe", directory / "isolated SendTo")
        digest = hashlib.sha256(pending_zip.read_bytes()).hexdigest()
        pending_checksum = directory / checksum.name
        pending_checksum.write_text(f"{digest}  {asset_name}\n", encoding="utf-8")
        shutil.copy2(pending_zip, final_zip)
        shutil.copy2(pending_checksum, checksum)
    print(f"Verified release ZIP: {final_zip}\nSHA256: {digest}", flush=True)
    return final_zip, checksum


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", required=True, type=Path)
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--output-directory", type=Path, default=ROOT / "dist" / "releases")
    args = parser.parse_args()
    package(args.executable.resolve(strict=True), args.artifact_id, args.output_directory.resolve())
