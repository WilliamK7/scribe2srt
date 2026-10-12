from importlib import metadata
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

try:
    from scripts import collect_licenses as collector
except ModuleNotFoundError as error:
    if error.name != "packaging":
        raise
    collector = None


@unittest.skipIf(collector is None, "License tests require the build dependency group")
class ReleaseLicenseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.requirements = self.root / "requirements-build.txt"

    def write_requirements(self, content):
        self.requirements.write_text(content, encoding="utf-8")
        return self.requirements

    def test_comments_and_normalized_package_names(self):
        path = self.write_requirements("  # Generated file\n\nPySide6_Addons==6.12.0  # wheel\n"
                                       "requests.Toolbelt==1.0.0\n")
        self.assertEqual(collector.pinned_requirements(path),
                         [("pyside6-addons", "6.12.0"), ("requests-toolbelt", "1.0.0")])

    def test_only_applicable_platform_and_python_pins_are_selected(self):
        path = self.write_requirements('macholib==1.16.4; sys_platform == "darwin"\n'
                                       'pywin32_ctypes==0.2.3; sys_platform == "win32"\n'
                                       'old-package==1.0; python_version < "3.14"\n')
        self.assertEqual(collector.pinned_requirements(path, {"sys_platform": "win32", "python_version": "3.15"}),
                         [("pywin32-ctypes", "0.2.3")])
        self.assertEqual(collector.pinned_requirements(path, {"sys_platform": "darwin", "python_version": "3.15"}),
                         [("macholib", "1.16.4")])

    def test_inactive_missing_package_is_not_inspected(self):
        path = self.write_requirements('not-installed==1.0; sys_platform == "darwin"\n')
        with patch.object(collector.metadata, "distribution") as lookup:
            self.assertEqual(list(collector.release_distributions(path, {"sys_platform": "win32"})), [])
            lookup.assert_not_called()

    def test_active_missing_package_fails(self):
        path = self.write_requirements("not-installed==1.0\n")
        with patch.object(collector.metadata, "distribution", side_effect=metadata.PackageNotFoundError("not-installed")):
            with self.assertRaisesRegex(RuntimeError, "Release dependency missing: not-installed==1.0"):
                list(collector.release_distributions(path))

    def test_different_installed_version_fails(self):
        path = self.write_requirements("sample-package==1.0\n")
        with patch.object(collector.metadata, "distribution", return_value=SimpleNamespace(version="2.0")):
            with self.assertRaisesRegex(RuntimeError, "Release dependency differs"):
                list(collector.release_distributions(path))

    def test_equivalent_pep440_versions_and_normalized_lookup(self):
        path = self.write_requirements("Sample_Package==1.0\n")
        distribution = SimpleNamespace(version="1.0.0")
        with patch.object(collector.metadata, "distribution", return_value=distribution) as lookup:
            self.assertEqual(list(collector.release_distributions(path)), [("sample-package", "1.0", distribution)])
            lookup.assert_called_once_with("sample-package")

    def test_non_exact_and_direct_requirements_are_rejected(self):
        for requirement in ("sample>=1.0", "sample==1.*", "sample[extra]==1.0", "sample @ https://example.com/sample.whl"):
            with self.subTest(requirement=requirement):
                path = self.write_requirements(requirement + "\n")
                with self.assertRaisesRegex(RuntimeError, "must be an exact version"):
                    collector.pinned_requirements(path)

    def test_invalid_requirement_identifies_line(self):
        path = self.write_requirements("# header\nthis is not a requirement\n")
        with self.assertRaisesRegex(RuntimeError, r"requirements-build.txt:2"):
            collector.pinned_requirements(path)

    def test_conflicting_pins_fail(self):
        path = self.write_requirements("Sample_Package==1.0\nsample-package==2.0\n")
        with self.assertRaisesRegex(RuntimeError, "Conflicting release dependency versions"):
            collector.pinned_requirements(path)

    def test_missing_original_license_fails(self):
        path = self.write_requirements("sample==1.0\n")
        distribution = SimpleNamespace(version="1.0", files=[])
        with patch.object(collector.metadata, "distribution", return_value=distribution):
            with self.assertRaisesRegex(RuntimeError, "No original license found for sample 1.0"):
                collector.collect_package_licenses(self.root, path)

    def test_original_notice_is_copied_and_qt_names_are_recognized(self):
        path = self.write_requirements("PySide6_Essentials==6.12.0\n")
        wheel = self.root / "wheel"
        wheel.mkdir()
        (wheel / "LICENSE.txt").write_bytes(b"original license bytes\r\n")
        distribution = SimpleNamespace(version="6.12.0", files=[Path("LICENSE.txt")],
                                       locate_file=lambda entry: wheel / entry,
                                       metadata={"License-Expression": "LGPL-3.0-only"})
        with patch.object(collector.metadata, "distribution", return_value=distribution):
            packages = collector.collect_package_licenses(self.root, path)
        self.assertEqual((self.root / "licenses/pyside6-essentials/LICENSE.txt").read_bytes(), b"original license bytes\r\n")
        self.assertEqual(packages[0]["license"], "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only; see Qt notices")


if __name__ == "__main__":
    unittest.main()
