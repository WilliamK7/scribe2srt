import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.native_license_sources import copy_native_licenses, source_records
from scripts import update_license_sources


class NativeLicenseTests(unittest.TestCase):
    versions = {"Python": "3.11.17", "OpenSSL": "3.5.9", "zlib": "1.3.2", "Expat": "2.8.5"}
    other_versions = {"Python": "3.11.9", "OpenSSL": "3.0.13", "zlib": "1.3.1", "Expat": "2.6.0"}

    def fixture(self, root):
        source = root / "third_party"
        records = []
        for versions in (self.versions, self.other_versions):
            for record in source_records(versions):
                content = record["url"].encode()
                path = source / record["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                records.append({**record, "sha256": hashlib.sha256(content).hexdigest()})
        (source / "native-sources.json").write_text(json.dumps({"schema": 1, "files": records}))
        return source, records

    def test_exact_versions_are_selected_without_network(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
                "socket.socket.connect", side_effect=AssertionError("Network must not be used")):
            root = Path(temporary)
            source, _ = self.fixture(root)
            for index, versions in enumerate((self.versions, self.other_versions)):
                staging = root / str(index)
                copied = copy_native_licenses(staging, versions, source)
                self.assertEqual(len(copied), 4)
                for expected, actual in zip(source_records(versions), copied):
                    self.assertEqual(actual["url"], expected["url"])
                    self.assertEqual((staging / actual["path"]).read_bytes(), expected["url"].encode())
                self.assertEqual(json.loads((staging / "licenses/native-sources.json").read_text()), copied)

    def test_missing_version_fails_without_fallback_or_partial_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _ = self.fixture(root)
            with self.assertRaisesRegex(RuntimeError, "update_license_sources.py --native-only"):
                copy_native_licenses(root / "staging", {**self.versions, "Expat": "9.9.9"}, source)
            self.assertFalse((root / "staging").exists())

    def test_modified_source_fails_before_copying(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, records = self.fixture(root)
            (source / records[3]["path"]).write_bytes(b"modified")
            with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                copy_native_licenses(root / "staging", self.versions, source)
            self.assertFalse((root / "staging").exists())

    def test_explicit_update_preserves_other_versions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _ = self.fixture(root)
            previous = {record["path"]: (source / record["path"]).read_bytes()
                        for record in source_records(self.other_versions)}
            with patch.object(update_license_sources, "ROOT", root), patch.object(
                    update_license_sources, "fetch", return_value=b"updated official source"), patch("builtins.print"):
                update_license_sources.refresh_native(self.versions)
            self.assertEqual(len(json.loads((source / "native-sources.json").read_text())["files"]), 8)
            for path, content in previous.items():
                self.assertEqual((source / path).read_bytes(), content)
            copy_native_licenses(root / "current", self.versions, source)
            copy_native_licenses(root / "previous", self.other_versions, source)


if __name__ == "__main__":
    unittest.main()
