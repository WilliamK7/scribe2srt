"""Versioned native license inputs; normal packaging never accesses the network."""

import hashlib
import json
from pathlib import Path
import pyexpat
import re
import shutil
import ssl
import sys
import zlib


def runtime_versions():
    return {"Python": sys.version.split()[0], "OpenSSL": ssl.OPENSSL_VERSION.split()[1],
            "zlib": zlib.ZLIB_VERSION, "Expat": pyexpat.EXPAT_VERSION.removeprefix("expat_")}


def inventory_versions(inventory):
    return {"Python": inventory["python"], "OpenSSL": inventory["openssl"].split()[1],
            "zlib": inventory["zlib"], "Expat": inventory["expat"]}


def source_records(versions):
    if set(versions) != {"Python", "OpenSSL", "zlib", "Expat"} or not all(
            isinstance(version, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", version)
            for version in versions.values()):
        raise ValueError("Unsupported native runtime versions")
    sources = {
        "Python": ("license.rst", f"python/cpython/v{versions['Python']}/Doc/license.rst"),
        "OpenSSL": ("LICENSE.txt", f"openssl/openssl/openssl-{versions['OpenSSL']}/LICENSE.txt"),
        "zlib": ("LICENSE", f"madler/zlib/v{versions['zlib']}/LICENSE"),
        "Expat": ("COPYING", f"libexpat/libexpat/R_{versions['Expat'].replace('.', '_')}/expat/COPYING"),
    }
    return [{"component": component, "version": versions[component],
             "path": f"native-licenses/{component}-{versions[component]}/{filename}",
             "url": "https://raw.githubusercontent.com/" + source}
            for component, (filename, source) in sources.items()]


def copy_native_licenses(staging, versions, source_root):
    """Select only exact component versions, verifying committed source hashes."""
    source_root = Path(source_root)
    manifest_path = source_root / "native-sources.json"
    if not manifest_path.is_file():
        raise RuntimeError("Native license inputs missing; run scripts/update_license_sources.py --native-only in the release environment")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != 1:
        raise RuntimeError("Unsupported native license manifest schema")
    records = {}
    for record in manifest["files"]:
        key = (record["component"], record["version"])
        if key in records:
            raise RuntimeError(f"Duplicate native license record: {key}")
        records[key] = record
    selected = []
    for expected in source_records(versions):
        record = records.get((expected["component"], expected["version"]))
        if not record or any(record.get(key) != value for key, value in expected.items()):
            raise RuntimeError(f"Native license input missing or mismatched: {expected['component']} {expected['version']}; "
                               "run scripts/update_license_sources.py --native-only in the release environment")
        source = source_root / record["path"]
        if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != record["sha256"]:
            raise RuntimeError(f"Native license hash mismatch or file missing: {source}")
        selected.append((record, source))
    copied = []
    for record, source in selected:
        relative = Path(record["path"]).relative_to("native-licenses")
        destination = staging / "licenses" / "native" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied.append({"path": destination.relative_to(staging).as_posix(),
                       "url": record["url"], "sha256": record["sha256"]})
    (staging / "licenses" / "native-sources.json").write_text(
        json.dumps(copied, indent=2) + "\n", encoding="utf-8")
    return copied
