"""Refresh Qt and native runtime notices from versioned official sources.

Run deliberately after changing dependency versions, then review and commit the resulting
third_party files. Release builds consume the committed, hash-checked copies.
"""

from concurrent.futures import ThreadPoolExecutor
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import posixpath
import sys
import time

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.native_license_sources import runtime_versions, inventory_versions, source_records

SOURCES = (("qt", "qtbase", "v6.12.0"), ("qt", "qtsvg", "v6.12.0"),
           ("qt", "qtimageformats", "v6.12.0"), ("qt", "qtdeclarative", "v6.12.0"),
           ("pyside", "pyside-setup", "6.12.0"))


def fetch(url):
    for attempt in range(3):
        try:
            response = requests.get(url, timeout=(10, 60))
            response.raise_for_status()
            return response.content
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def refresh():
    records, repositories, downloads = [], [], {}
    for owner, repository, tag in SOURCES:
        data = json.loads(fetch(f"https://api.github.com/repos/{owner}/{repository}/git/trees/{tag}?recursive=1"))
        if data.get("truncated"):
            raise RuntimeError(f"Incomplete source tree: {repository}")
        commit = data["sha"]
        repositories.append({"repository": f"{owner}/{repository}", "tag": tag,
                             "tree_sha": commit,
                             "source": f"https://github.com/{owner}/{repository}/tree/{tag}"})
        paths = {entry["path"] for entry in data["tree"] if entry["type"] == "blob"}
        selected = {path for path in paths if path.startswith("LICENSES/")}
        attributions = [path for path in sorted(paths) if path.endswith("qt_attribution.json")
                        and path.startswith(("src/", "sources/"))
                        and not any(part in path.split("/") for part in ("testlib", "testinternal"))]
        base = f"https://raw.githubusercontent.com/{owner}/{repository}/{tag}/"
        # Include original attribution metadata even for optional/platform-specific
        # code; it records the exact embedded third-party versions and copyrights.
        with ThreadPoolExecutor(max_workers=6) as executor:
            contents = list(executor.map(lambda path: fetch(base + path), attributions))
        for path, content in zip(attributions, contents):
            selected.add(path)
            downloads[(repository, path)] = (base + path, content)
            # Some official attribution descriptions contain literal newlines.
            entries = json.loads(content, strict=False)
            if isinstance(entries, dict):
                entries = [entries]
            for entry in entries:
                for key in ("LicenseFile", "LicenseFiles", "CopyrightFile", "CopyrightFiles"):
                    values = entry.get(key, [])
                    if isinstance(values, str):
                        values = [values]
                    for value in values:
                        candidate = posixpath.normpath(str(PurePosixPath(path).parent / value))
                        if candidate not in paths:
                            raise RuntimeError(f"Missing official notice: {repository}/{candidate}")
                        selected.add(candidate)
        for path in sorted(selected):
            downloads.setdefault((repository, path), (base + path, None))

    def download(item):
        (repository, path), (url, content) = item
        content = fetch(url) if content is None else content
        relative = PurePosixPath("licenses", "Qt-6.12.0", repository, path)
        destination = ROOT / "third_party" / Path(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return {"path": relative.as_posix(), "url": url,
                "sha256": hashlib.sha256(content).hexdigest()}

    with ThreadPoolExecutor(max_workers=6) as executor:
        records = list(executor.map(download, sorted(downloads.items())))
    manifest = {"qt_version": "6.12.0", "repositories": repositories,
                "files": sorted(records, key=lambda entry: entry["path"])}
    (ROOT / "third_party" / "sources.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Preserved {len(records)} original Qt/PySide source license and attribution files", flush=True)


def refresh_native(versions):
    manifest_path = ROOT / "third_party" / "native-sources.json"
    records = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("schema") != 1:
            raise RuntimeError("Unsupported native license manifest schema")
        records = {(record["component"], record["version"]): record for record in manifest["files"]}
    for record in source_records(versions):
        content = fetch(record["url"])
        destination = ROOT / "third_party" / record["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        record["sha256"] = hashlib.sha256(content).hexdigest()
        records[(record["component"], record["version"])] = record
    manifest_path.write_text(json.dumps({"schema": 1, "files": [records[key] for key in sorted(records)]},
                                        indent=2) + "\n", encoding="utf-8")
    print(f"Preserved native license inputs for {versions}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-only", action="store_true", help="Update native runtime sources without re-fetching Qt sources")
    parser.add_argument("--runtime-manifest", type=Path, help="Use versions from a release licenses/dependency-versions.json")
    args = parser.parse_args()
    if not args.native_only:
        refresh()
    versions = inventory_versions(json.loads(args.runtime_manifest.read_text(encoding="utf-8"))) if args.runtime_manifest else runtime_versions()
    refresh_native(versions)
