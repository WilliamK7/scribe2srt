"""Copy original license/NOTICE files from the exact release environment."""

import hashlib
from importlib import metadata
import json
from pathlib import Path
import re
import shutil
import ssl
import sys
import zlib

from PySide6.QtCore import qVersion
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version
from scripts.native_license_sources import runtime_versions, copy_native_licenses

ROOT = Path(__file__).resolve().parent.parent


def pinned_requirements(path, environment=None):
    """Read applicable, exact pins from a uv requirements export."""
    pins = {}
    for number, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = re.split(r"\s+#", raw.strip(), maxsplit=1)[0]
        if not line or line.startswith("#"):
            continue
        try:
            requirement = Requirement(line)
        except InvalidRequirement as error:
            raise RuntimeError(f"Invalid release requirement at {path}:{number}: {line}") from error
        if requirement.marker and not requirement.marker.evaluate(environment=environment):
            continue
        specifications = list(requirement.specifier)
        if (requirement.url or requirement.extras or len(specifications) != 1
                or specifications[0].operator != "==" or "*" in specifications[0].version):
            raise RuntimeError(f"Release requirement must be an exact version: {line}")
        name = canonicalize_name(requirement.name)
        version = str(Version(specifications[0].version))
        if name in pins and Version(pins[name]) != Version(version):
            raise RuntimeError(f"Conflicting release dependency versions: {name}")
        pins[name] = version
    return list(pins.items())


def release_distributions(path, environment=None):
    for name, version in pinned_requirements(path, environment):
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError as error:
            raise RuntimeError(f"Release dependency missing: {name}=={version}") from error
        if Version(distribution.version) != Version(version):
            raise RuntimeError(f"Release dependency differs: {name} {distribution.version} != {version}")
        yield name, version, distribution


def collect_package_licenses(staging, requirements):
    licenses = staging / "licenses"
    licenses.mkdir()
    packages = []
    for name, version, distribution in release_distributions(requirements):
        copied = []
        for entry in distribution.files or ():
            filename = Path(str(entry))
            if (not filename.name.lower().startswith(
                    ("license", "licence", "copying", "notice", "copyright", "authors"))
                    or filename.suffix.lower() in {".py", ".pyc", ".pyd"}):
                continue
            source = Path(distribution.locate_file(entry))
            if not source.is_file():
                continue
            destination = licenses / name / filename
            if not destination.resolve().is_relative_to(licenses.resolve()):
                raise RuntimeError(f"License path escapes staging: {entry}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied.append(destination.relative_to(staging).as_posix())
        if not copied:
            raise RuntimeError(f"No original license found for {name} {version}")
        info = distribution.metadata
        expression = info.get("License-Expression") or info.get("License", "See original license files")
        if name.startswith("pyside6") or name == "shiboken6":
            expression = "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only; see Qt notices"
        packages.append({"name": name, "version": version, "license": expression,
                         "license_files": sorted(copied),
                         "source": f"https://pypi.org/project/{name}/{version}/"})
    return packages


def collect_licenses(staging, archive_names, native_versions):
    packages = collect_package_licenses(staging, ROOT / "requirements-build.txt")
    licenses = staging / "licenses"

    # Wheels omit most Qt license texts. These copies came from the matching
    # official sources and have independently recorded URLs and SHA-256 hashes.
    source_manifest = json.loads((ROOT / "third_party" / "sources.json").read_text(encoding="utf-8"))
    if source_manifest["qt_version"] != qVersion():
        raise RuntimeError("Qt runtime version differs from the committed license sources")
    for record in source_manifest["files"]:
        source = ROOT / "third_party" / record["path"]
        if hashlib.sha256(source.read_bytes()).hexdigest() != record["sha256"]:
            raise RuntimeError(f"Vendored license hash mismatch: {source}")
    shutil.copytree(ROOT / "third_party" / "licenses", licenses / "official-sources")
    shutil.copy2(ROOT / "third_party" / "sources.json", licenses / "sources.json")
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise RuntimeError("The Python installation must provide its original LICENSE.txt")
    python_directory = licenses / f"Python-{sys.version.split()[0]}"
    python_directory.mkdir()
    shutil.copy2(python_license, python_directory / "LICENSE.txt")

    versions = runtime_versions()
    copy_native_licenses(staging, versions, ROOT / "third_party")

    inventory = {"python": versions["Python"], "qt": qVersion(), "openssl": ssl.OPENSSL_VERSION,
                 "zlib": zlib.ZLIB_RUNTIME_VERSION,
                 "expat": versions["Expat"], "packages": packages,
                 "native_binaries": native_versions,
                 "bundle_files": sorted(archive_names)}
    if "zlib-ng" in versions:
        inventory["zlib_ng"] = versions["zlib-ng"]
    (licenses / "dependency-versions.json").write_text(
        json.dumps(inventory, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    # Explain the runtime separately from the larger, pinned build environment.
    notices = (ROOT / "third_party" / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    compression_version = (f"zlib-ng {inventory['zlib_ng']} (zlib compatibility {inventory['zlib']})"
                           if "zlib_ng" in inventory else f"zlib {inventory['zlib']}")
    notices += (f"\n## This build\n\nPython {inventory['python']}; {inventory['openssl']}; "
                f"{compression_version}. Exact wheel versions and bundled file names are in "
                "`licenses/dependency-versions.json`. The table includes build tools for provenance; "
                "not every installed wheel is distributed in the EXE.\n\n"
                "| Build dependency | Version | Original license files |\n|---|---|---|\n")
    for package in packages:
        notices += f"| {package['name']} | {package['version']} | licenses/{package['name']}/ |\n"
    (staging / "THIRD_PARTY_NOTICES.md").write_text(notices, encoding="utf-8")
