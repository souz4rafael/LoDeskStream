import json
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest import mock
import warnings
import zipfile

from tools import build_support as build


class BuildSupportTests(unittest.TestCase):
    def setUp(self):
        # Match build staging: isolate mutable fixtures from OneDrive synchronization.
        temporary = tempfile.TemporaryDirectory(prefix="LoDeskStream-build-tests-")
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.root = self.work / "portable"
        self.root.mkdir()
        self.version = build.version_from_props(Path(__file__).resolve().parents[1])
        for name in build.REQUIRED:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"MZ" + bytes(126) if name.endswith(".exe") else b"test payload\n")
        self.info = {
            "application": "LoDeskStream",
            "version": self.version,
            "distributionReady": False,
            "licensingStatus": "deferred",
        }
        (self.root / "_internal" / "build-info.json").write_text(json.dumps(self.info), encoding="utf-8")
        self.archive = self.work / build.ZIP_NAME

    def manifest(self):
        build.create_manifest(self.root, self.version)

    def rewrite_manifest(self, change):
        path = self.root / build.MANIFEST
        data = json.loads(path.read_text(encoding="utf-8"))
        change(data)
        path.write_text(json.dumps(data), encoding="utf-8")

    def final_directory(self):
        self.manifest()
        final = self.work / "final"
        final.mkdir()
        build.create_zip(self.root, final / build.ZIP_NAME, self.version)
        (final / build.SETUP_NAME).write_bytes(b"MZ" + bytes(126))
        build.create_checksums(final)
        return final

    def test_clean_manifest_and_zip_at_archive_root(self):
        extra = self.root / "manager" / "runtime.dll"
        extra.write_bytes(b"runtime")
        self.manifest()
        files = build.verify_manifest(self.root, self.version)
        self.assertIn("manager/runtime.dll", files)
        self.assertNotIn(build.MANIFEST, files)
        build.create_zip(self.root, self.archive, self.version)
        build.verify_zip(self.archive, self.root, self.version)
        with zipfile.ZipFile(self.archive) as archive:
            self.assertIn("LoDeskStream.exe", archive.namelist())
            self.assertIn(build.MANIFEST, archive.namelist())
            self.assertTrue(all(not name.startswith("portable/") for name in archive.namelist()))

    def test_zip_bytes_are_stable_for_identical_payload(self):
        self.manifest()
        other = self.work / "other.zip"
        build.create_zip(self.root, self.archive, self.version)
        build.create_zip(self.root, other, self.version)
        self.assertEqual(self.archive.read_bytes(), other.read_bytes())

    def test_tampering_including_same_size_detected(self):
        self.manifest()
        server = self.root / "LoDeskStream.exe"
        server.write_bytes(b"NZ" + bytes(126))
        with self.assertRaisesRegex(ValueError, "size/hash"):
            build.verify_manifest(self.root, self.version)

    def test_missing_file_detected(self):
        self.manifest()
        (self.root / "README.md").unlink()
        with self.assertRaisesRegex(ValueError, "missing="):
            build.verify_manifest(self.root, self.version)

    def test_extra_file_detected(self):
        self.manifest()
        (self.root / "stale.dll").write_bytes(b"old")
        with self.assertRaisesRegex(ValueError, "extra="):
            build.verify_manifest(self.root, self.version)

    def test_missing_required_input_cannot_be_manifested(self):
        (self.root / "_internal" / "web" / "input-queue.js").unlink()
        with self.assertRaisesRegex(ValueError, "Missing required"):
            self.manifest()
        self.assertFalse((self.root / build.MANIFEST).exists())

    def test_unsafe_manifest_paths_rejected(self):
        self.manifest()
        original = (self.root / build.MANIFEST).read_bytes()
        for name in (
            "../escape", "/absolute", "C:/escape", "C:\\escape", "foo\\..\\escape",
            "a//b", "a/./b", "a/../b", "a:b", "a.", "a /b", "CON.txt", "a/\x00b",
            "a/*", "a/\nb", "",
        ):
            with self.subTest(path=name):
                (self.root / build.MANIFEST).write_bytes(original)
                self.rewrite_manifest(lambda data: data["files"][0].update(path=name))
                with self.assertRaises(ValueError):
                    build.verify_manifest(self.root, self.version)

    def test_duplicate_manifest_paths_rejected_case_insensitively(self):
        self.manifest()
        def duplicate(data):
            entry = dict(data["files"][0])
            entry["path"] = entry["path"].swapcase()
            data["files"].append(entry)
        self.rewrite_manifest(duplicate)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            build.verify_manifest(self.root, self.version)

    def test_invalid_manifest_sizes_and_hashes_rejected(self):
        self.manifest()
        original = (self.root / build.MANIFEST).read_bytes()
        for update in ({"size": True}, {"size": -1}, {"size": 1.5}, {"sha256": "bad"}):
            with self.subTest(update=update):
                (self.root / build.MANIFEST).write_bytes(original)
                self.rewrite_manifest(lambda data: data["files"][0].update(update))
                with self.assertRaises(ValueError):
                    build.verify_manifest(self.root, self.version)

    def test_manifest_identity_and_status_must_agree(self):
        self.manifest()
        with self.assertRaisesRegex(ValueError, "identity"):
            build.verify_manifest(self.root, "9.9.9")
        (self.root / build.MANIFEST).unlink()
        self.info["distributionReady"] = True
        (self.root / "_internal" / "build-info.json").write_text(json.dumps(self.info), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "development status"):
            self.manifest()

    def test_duplicate_json_keys_rejected(self):
        self.manifest()
        (self.root / build.MANIFEST).write_text(
            '{"application":"LoDeskStream","application":"other"}', encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "Duplicate JSON"):
            build.verify_manifest(self.root, self.version)

    def test_manifest_and_zip_are_never_overwritten(self):
        self.manifest()
        manifest_before = (self.root / build.MANIFEST).read_bytes()
        with self.assertRaises(FileExistsError):
            self.manifest()
        self.assertEqual(manifest_before, (self.root / build.MANIFEST).read_bytes())
        self.archive.write_bytes(b"existing zip")
        with self.assertRaises(FileExistsError):
            build.create_zip(self.root, self.archive, self.version)
        self.assertEqual(b"existing zip", self.archive.read_bytes())

    def test_symlink_payload_is_rejected(self):
        # Windows symlink creation may require elevation; emulate its lstat classification.
        original = build.is_link
        for relative in ("README.md", "_internal"):
            with self.subTest(path=relative):
                link = self.root / relative
                with mock.patch.object(
                    build, "is_link", side_effect=lambda path: Path(path) == link or original(path)
                ):
                    with self.assertRaisesRegex(ValueError, "Symlink/reparse"):
                        self.manifest()

    def test_reparse_point_attribute_is_a_link(self):
        path = mock.Mock()
        path.lstat.return_value = mock.Mock(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        self.assertTrue(build.is_link(path))

    def test_zip_rejects_tamper_missing_extra_traversal_duplicate_and_symlink(self):
        self.manifest()
        build.create_zip(self.root, self.archive, self.version)
        with zipfile.ZipFile(self.archive) as archive:
            clean = [(entry.filename, archive.read(entry)) for entry in archive.infolist()]
        for mode in ("tamper", "missing", "extra", "traversal", "duplicate", "symlink"):
            with self.subTest(mode=mode):
                bad = self.work / f"{mode}.zip"
                entries = list(clean)
                if mode == "tamper":
                    entries[0] = (entries[0][0], b"tampered")
                elif mode == "missing":
                    entries.pop()
                elif mode == "extra":
                    entries.append(("stale.exe", b"old"))
                elif mode == "traversal":
                    entries.append(("../escape", b"escape"))
                elif mode == "duplicate":
                    entries.append((entries[0][0].swapcase(), entries[0][1]))
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    with zipfile.ZipFile(bad, "x") as archive:
                        for name, content in entries:
                            archive.writestr(name, content)
                        if mode == "symlink":
                            info = zipfile.ZipInfo("link")
                            info.create_system = 3
                            info.external_attr = (stat.S_IFLNK | 0o777) << 16
                            archive.writestr(info, "LoDeskStream.exe")
                with self.assertRaises(ValueError):
                    build.verify_zip(bad, self.root, self.version)

    def test_promote_complete_output_to_absent_or_empty_directory(self):
        final = self.final_directory()
        for name, empty in (("absent", False), ("empty", True)):
            output = self.work / name
            if empty:
                output.mkdir()
            build.promote(final, output)
            build.verify_final(output)
            self.assertEqual((final / build.ZIP_NAME).read_bytes(), (output / build.ZIP_NAME).read_bytes())
        self.assertFalse(list(self.work.glob(".*.candidate-*")))

    def test_promote_never_overwrites_old_output(self):
        final = self.final_directory()
        output = self.work / "existing"
        output.mkdir()
        old = output / build.SETUP_NAME
        old.write_bytes(b"historical output")
        with self.assertRaises(FileExistsError):
            build.promote(final, output)
        self.assertEqual(old.read_bytes(), b"historical output")

    def test_failed_validation_does_not_promote_partial_artifacts(self):
        final = self.final_directory()
        (final / build.SETUP_NAME).write_bytes(b"MZ" + b"bad" * 100)
        output = self.work / "output"
        with self.assertRaisesRegex(ValueError, "checksums"):
            build.promote(final, output)
        self.assertFalse(output.exists())

    def test_failed_copy_verification_does_not_promote(self):
        final = self.final_directory()
        output = self.work / "output"
        verify = build.verify_final
        calls = 0
        def fail_second(path):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("copy corrupted")
            return verify(path)
        with mock.patch.object(build, "verify_final", side_effect=fail_second):
            with self.assertRaisesRegex(ValueError, "copy corrupted"):
                build.promote(final, output)
        self.assertFalse(output.exists())
        self.assertFalse(list(self.work.glob(".*.candidate-*")))

    def test_final_directory_rejects_stale_extra_artifact(self):
        final = self.final_directory()
        (final / "stale.zip").write_bytes(b"old")
        with self.assertRaisesRegex(ValueError, "unexpected"):
            build.verify_final(final)

    def test_checksums_never_overwritten(self):
        final = self.final_directory()
        before = (final / "SHA256SUMS.txt").read_bytes()
        with self.assertRaises(FileExistsError):
            build.create_checksums(final)
        self.assertEqual(before, (final / "SHA256SUMS.txt").read_bytes())

    def test_output_file_and_link_rejected(self):
        file = self.work / "old.zip"
        file.write_bytes(b"old")
        with self.assertRaises(FileExistsError):
            build.check_output(file)

    def test_plain_text_executable_rejected(self):
        path = self.work / "not-an-exe.exe"
        path.write_bytes(b"not executable" * 20)
        with self.assertRaisesRegex(ValueError, "Not a Windows"):
            build.check_executable(path)

    def test_prepare_records_versions_provenance_and_deferred_status_without_paths(self):
        repository = Path(__file__).resolve().parents[1]
        staging = self.work / "metadata"
        distribution = mock.Mock()
        distribution.metadata = {"Name": "inventory-example"}
        distribution.version = "1.2.3"
        pins = build.requirement_pins(repository / "requirements.txt")
        pins["PyInstaller"] = "6.22.3"
        versions = pins.__getitem__
        with mock.patch.object(build.importlib.metadata, "version", side_effect=versions):
            with mock.patch.object(build.importlib.metadata, "distributions", return_value=[distribution]):
                build.prepare(repository, staging, self.work / "output", None, False, "10.0.403", "v22.0.0")
        info = build.load_json(staging / "build-info.json")
        self.assertEqual(info["version"], self.version)
        self.assertFalse(info["distributionReady"])
        self.assertEqual(info["licensingStatus"], "deferred")
        self.assertEqual(info["source"]["kind"], "source-archive")
        self.assertIsNone(info["source"]["dirty"])
        self.assertIn("server.py", info["source"]["inputSha256"])
        self.assertIn("tools/build_support.py", info["source"]["inputSha256"])
        self.assertEqual(64, len(info["source"]["fingerprintSha256"]))
        self.assertTrue(all(not Path(name).is_absolute() for name in info["source"]["inputSha256"]))
        self.assertEqual(info["toolVersions"]["pyinstaller"], "6.22.3")
        self.assertEqual(info["toolVersions"]["dotnetSdk"], "10.0.403")
        self.assertIn("NOT legal clearance", info["dependencyInventory"]["purpose"])
        self.assertEqual(info["dependencyInventory"]["packages"], {"inventory-example": "1.2.3"})
        self.assertNotIn(str(repository), json.dumps(info))
        self.assertNotIn(str(self.work), json.dumps(info))
        self.assertTrue((staging / "LoDeskStream.ico").is_file())
        before = (staging / "build-info.json").read_bytes()
        with mock.patch.object(build.importlib.metadata, "version", side_effect=versions):
            with self.assertRaises(FileExistsError):
                build.prepare(repository, staging, self.work / "output", None, False, "10.0.403", "v22.0.0")
        self.assertEqual(before, (staging / "build-info.json").read_bytes())

    def test_missing_packages_fail_with_actionable_instructions(self):
        repository = Path(__file__).resolve().parents[1]
        missing = build.importlib.metadata.PackageNotFoundError("PyInstaller")
        with mock.patch.object(build.importlib.metadata, "version", side_effect=missing):
            with self.assertRaisesRegex(ValueError, "pip install -r requirements-build.txt"):
                build.prepare(repository, self.work / "metadata", self.work / "output",
                              None, False, "10.0.403", "v22.0.0")
        self.assertFalse((self.work / "metadata").exists())

    def test_wrong_pyinstaller_fails_before_creating_staging(self):
        repository = Path(__file__).resolve().parents[1]
        with mock.patch.object(build.importlib.metadata, "version", return_value="6.0"):
            with self.assertRaisesRegex(ValueError, "PyInstaller 6.22.3 required"):
                build.prepare(repository, self.work / "metadata", self.work / "output",
                              None, False, "10.0.403", "v22.0.0")
        self.assertFalse((self.work / "metadata").exists())

    def test_direct_runtime_version_mismatch_is_actionable(self):
        repository = Path(__file__).resolve().parents[1]
        pins = build.requirement_pins(repository / "requirements.txt")
        pins["PyInstaller"] = "6.22.3"
        pins["Pillow"] = "0.0.0"
        with mock.patch.object(build.importlib.metadata, "version", side_effect=pins.__getitem__):
            with self.assertRaisesRegex(ValueError, r"Pillow 10\.4\.0 required, found 0\.0\.0"):
                build.prepare(repository, self.work / "metadata", self.work / "output",
                              None, False, "10.0.403", "v22.0.0")
        self.assertFalse((self.work / "metadata").exists())

    def test_source_fingerprint_tracks_inputs_but_not_generated_outputs(self):
        source = self.work / "source"
        source.mkdir()
        for relative in (
            "server.py", "build.ps1", "requirements.txt", "Directory.Build.props",
            "web/index.html", "manager-src/Program.cs", "installer-src/Program.cs",
            "tests/example.test.cjs", "tools/build_support.py", "log_privacy.py",
            "tests/installer/Program.cs", "tests/installer/LoDeskStream.InstallerTests.csproj",
        ):
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"source")
        first = build.source_fingerprint(source)
        for relative in ("log_privacy.py", "tests/installer/Program.cs",
                         "tests/installer/LoDeskStream.InstallerTests.csproj"):
            self.assertIn(relative, first["inputSha256"])
        self.assertEqual(first, build.source_fingerprint(source))
        for relative in (
            "manager-src/bin/generated.dll", "installer-src/obj/generated.cs",
            "tests/installer/bin/generated.dll", "tests/installer/obj/generated.cs",
            "web/__pycache__/generated.pyc", "tools/debug.log",
            "tests/.build-support-scratch/portable/server.py",
            ".git/config", "dist/stale.zip", "local-candidates/old.zip",
        ):
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"generated")
        self.assertEqual(first, build.source_fingerprint(source))
        (source / "server.py").write_bytes(b"changed uncommitted source")
        changed = build.source_fingerprint(source)
        self.assertNotEqual(first["inputSha256"]["server.py"], changed["inputSha256"]["server.py"])
        self.assertNotEqual(first["fingerprintSha256"], changed["fingerprintSha256"])
        self.assertEqual(first["inputSha256"]["build.ps1"], changed["inputSha256"]["build.ps1"])
        (source / "tools" / "new.py").write_bytes(b"new source")
        self.assertIn("tools/new.py", build.source_fingerprint(source)["inputSha256"])

    def test_source_fingerprint_does_not_apply_payload_reparse_rejection(self):
        source = self.work / "source"
        source.mkdir()
        (source / "server.py").write_bytes(b"cloud hydrated source")
        with mock.patch.object(build, "is_link", side_effect=AssertionError("Payload-only policy")):
            fingerprint = build.source_fingerprint(source)
        self.assertEqual(fingerprint["inputSha256"]["server.py"], build.sha256(source / "server.py"))

    def test_source_changes_during_build_rejected_before_promotion(self):
        source = self.work / "source"
        source.mkdir()
        (source / "server.py").write_bytes(b"original")
        info = self.work / "build-info.json"
        build.write_json_exclusive(info, {"source": build.source_fingerprint(source)})
        build.verify_source(source, info)
        (source / "server.py").write_bytes(b"uncommitted edit")
        with self.assertRaisesRegex(ValueError, "Source inputs changed"):
            build.verify_source(source, info)


if __name__ == "__main__":
    unittest.main()
