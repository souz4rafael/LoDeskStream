"""Local candidate packaging helpers. No dependency installation or legal clearance."""

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import sys
import uuid
import xml.etree.ElementTree as ET
import zipfile


APPLICATION = "LoDeskStream"
MANIFEST = "payload-manifest.json"
ZIP_NAME = "LoDeskStream-Windows-portable.zip"
SETUP_NAME = "LoDeskStream-Setup.exe"
REQUIRED = {
    "LoDeskStream.exe",
    "manager/LoDeskStreamManager.exe",
    "_internal/web/index.html",
    "_internal/web/input-queue.js",
    "_internal/web/keyboard-input.js",
    "_internal/build-info.json",
    "README.md",
    "run-lodeskstream.ps1",
}
SOURCE_FILES = (
    "server.py", "capture_pipeline.py", "input_sessions.py", "input_validation.py", "log_privacy.py",
    "build.ps1", "Directory.Build.props", "Directory.Build.targets",
    "requirements.txt", "requirements-build.txt", "README.md", "run-lodeskstream.ps1",
    "global.json", "NuGet.Config", "nuget.config",
)
SOURCE_TREES = ("web", "tools", "manager-src", "installer-src", "tests")
GENERATED_DIRECTORIES = {
    ".git", ".venv", "venv", "__pycache__", "bin", "obj", "build", "dist",
    "local-candidates", "candidates", "node_modules", "debug-captures",
}


def version_from_props(root):
    version = ET.parse(Path(root) / "Directory.Build.props").findtext(".//Version")
    if not version or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Directory.Build.props must contain a numeric Version")
    return version


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_fingerprint(root):
    """Hash explicit build inputs, reading hydrated/cloud source files normally."""
    root = Path(root)
    paths = {}
    for name in SOURCE_FILES:
        path = root / name
        if path.is_file():
            paths[name] = path
    for tree in SOURCE_TREES:
        directory = root / tree
        if not directory.is_dir():
            continue
        for current, dirs, names in os.walk(directory, followlinks=False):
            dirs[:] = [
                name for name in dirs
                if name.casefold() not in GENERATED_DIRECTORIES
                and not name.startswith(".build-support-")
            ]
            for name in names:
                path = Path(current) / name
                if path.suffix.casefold() in {".log", ".pyc", ".pyo"}:
                    continue
                if path.is_file():
                    paths[path.relative_to(root).as_posix()] = path
    hashes = {name: sha256(path) for name, path in sorted(paths.items())}
    encoded = json.dumps(hashes, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return {
        "inputSha256": hashes,
        "fingerprintSha256": hashlib.sha256(encoded).hexdigest(),
        "fingerprintFormat": "SHA256 of UTF-8 compact sorted-key JSON inputSha256 map",
    }


def verify_source(root, info_path):
    recorded = load_json(info_path).get("source", {})
    actual = source_fingerprint(root)
    if any(recorded.get(key) != value for key, value in actual.items()):
        raise ValueError("Source inputs changed during the build; rerun from a stable source tree")


def requirement_pins(path):
    pins = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)", line)
            if not match:
                raise ValueError(f"Unsupported requirements entry (update preflight): {line}")
            pins[match[1]] = match[2]
    return pins


def write_json_exclusive(path, value):
    with open(path, "x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=True)
        stream.write("\n")


def is_link(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def safe_relative(value):
    if not isinstance(value, str) or not value:
        raise ValueError("Payload paths must be nonempty strings")
    if "\\" in value or ":" in value or "\x00" in value:
        raise ValueError(f"Unsafe payload path: {value!r}")
    parts = value.split("/")
    if any(
        part in ("", ".", "..")
        or part.endswith((" ", "."))
        or any(ord(char) < 32 or char in '<>"|?*' for char in part)
        or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part)
        for part in parts
    ) or PurePosixPath(value).is_absolute():
        raise ValueError(f"Unsafe payload path: {value!r}")
    return value


def payload_files(root):
    root = Path(root)
    if is_link(root) or not root.is_dir():
        raise ValueError("Payload root must be a regular directory, not a link")
    found = {}
    seen = set()
    for current, dirs, names in os.walk(root, followlinks=False):
        for name in sorted(dirs + names):
            path = Path(current) / name
            relative = safe_relative(path.relative_to(root).as_posix())
            key = relative.casefold()
            if key in seen:
                raise ValueError(f"Duplicate Windows payload path: {relative}")
            seen.add(key)
            if is_link(path):
                raise ValueError(f"Symlink/reparse point in payload: {relative}")
            if path.is_file():
                found[relative] = path
            elif not path.is_dir():
                raise ValueError(f"Non-regular payload file: {relative}")
    return found


def load_json(path):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result
    with open(path, encoding="utf-8") as stream:
        return json.load(stream, object_pairs_hook=unique_pairs)


def check_build_info(path, version):
    info = load_json(path)
    if (
        not isinstance(info, dict)
        or info.get("application") != APPLICATION
        or info.get("version") != version
        or info.get("distributionReady") is not False
        or info.get("licensingStatus") != "deferred"
    ):
        raise ValueError("build-info.json identity/development status does not match")


def create_manifest(root, version):
    root = Path(root)
    files = payload_files(root)
    if MANIFEST.casefold() in {name.casefold() for name in files}:
        raise FileExistsError("Refusing to overwrite an existing payload manifest")
    missing = REQUIRED - files.keys()
    if missing:
        raise ValueError(f"Missing required payload files: {sorted(missing)}")
    check_build_info(root / "_internal" / "build-info.json", version)
    entries = [
        {"path": name, "sha256": sha256(path), "size": path.stat().st_size}
        for name, path in sorted(files.items())
    ]
    write_json_exclusive(root / MANIFEST, {
        "application": APPLICATION, "version": version, "files": entries,
    })
    verify_manifest(root, version)


def verify_manifest(root, version):
    root = Path(root)
    actual = payload_files(root)
    if MANIFEST not in actual:
        raise ValueError("Missing payload-manifest.json")
    manifest = load_json(actual.pop(MANIFEST))
    if (
        not isinstance(manifest, dict)
        or manifest.get("application") != APPLICATION
        or manifest.get("version") != version
        or not isinstance(manifest.get("files"), list)
    ):
        raise ValueError("Invalid manifest identity, version, or files")
    expected = {}
    seen = set()
    for entry in manifest["files"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "size"}:
            raise ValueError("Invalid manifest file entry")
        name = safe_relative(entry["path"])
        if name.casefold() in seen or name.casefold() == MANIFEST.casefold():
            raise ValueError(f"Duplicate/self-referencing manifest path: {name}")
        seen.add(name.casefold())
        if type(entry["size"]) is not int or entry["size"] < 0:
            raise ValueError(f"Invalid manifest size: {name}")
        if not isinstance(entry["sha256"], str) or not re.fullmatch(
            r"[0-9a-f]{64}", entry["sha256"]
        ):
            raise ValueError(f"Invalid SHA256: {name}")
        expected[name] = entry
    if REQUIRED - expected.keys():
        raise ValueError(f"Manifest missing required files: {sorted(REQUIRED - expected.keys())}")
    if actual.keys() != expected.keys():
        raise ValueError(
            f"Payload file mismatch; missing={sorted(expected.keys() - actual.keys())}; "
            f"extra={sorted(actual.keys() - expected.keys())}"
        )
    for name, path in actual.items():
        entry = expected[name]
        if path.stat().st_size != entry["size"] or sha256(path) != entry["sha256"]:
            raise ValueError(f"Payload size/hash mismatch: {name}")
    check_build_info(root / "_internal" / "build-info.json", version)
    return actual


def verify_zip(archive_path, root, version):
    files = verify_manifest(root, version)
    files[MANIFEST] = Path(root) / MANIFEST
    with zipfile.ZipFile(archive_path) as archive:
        entries = {}
        folded = set()
        for entry in archive.infolist():
            name = safe_relative(entry.filename)
            mode = entry.external_attr >> 16
            if entry.is_dir() or stat.S_ISLNK(mode) or (
                stat.S_IFMT(mode) not in (0, stat.S_IFREG)
            ):
                raise ValueError(f"Non-regular ZIP entry: {name}")
            if name.casefold() in folded:
                raise ValueError(f"Duplicate ZIP entry: {name}")
            folded.add(name.casefold())
            entries[name] = entry
        if entries.keys() != files.keys():
            raise ValueError("ZIP file list does not exactly match the payload")
        for name, path in files.items():
            if entries[name].file_size != path.stat().st_size:
                raise ValueError(f"ZIP size mismatch: {name}")
            digest = hashlib.sha256()
            with archive.open(entries[name]) as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != sha256(path):
                raise ValueError(f"ZIP hash mismatch: {name}")


def create_zip(root, destination, version):
    files = verify_manifest(root, version)
    files[MANIFEST] = Path(root) / MANIFEST
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Refusing to overwrite ZIP: {destination}")
    # Stable ordering/metadata reduces incidental variation, not toolchain nondeterminism.
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=9, allowZip64=True) as archive:
        for name, path in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            with open(path, "rb") as source, archive.open(info, "w", force_zip64=True) as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
    verify_zip(destination, root, version)


def check_output(destination):
    path = Path(destination).absolute()
    if path.exists() or path.is_symlink():
        if is_link(path) or not path.is_dir() or any(path.iterdir()):
            raise FileExistsError(f"Output directory must be absent or empty: {path}")
    return path


def check_executable(path):
    path = Path(path)
    if is_link(path) or not path.is_file() or path.stat().st_size < 64:
        raise ValueError(f"Missing/invalid fresh executable: {path.name}")
    with open(path, "rb") as stream:
        if stream.read(2) != b"MZ":
            raise ValueError(f"Not a Windows executable: {path.name}")


def create_checksums(root):
    root = Path(root)
    for name in (ZIP_NAME, SETUP_NAME):
        path = root / name
        if is_link(path) or not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"Missing final artifact: {name}")
    check_executable(root / SETUP_NAME)
    with open(root / "SHA256SUMS.txt", "x", encoding="ascii", newline="\n") as stream:
        for name in (ZIP_NAME, SETUP_NAME):
            stream.write(f"{sha256(root / name)}  {name}\n")


def verify_final(root):
    root = Path(root)
    files = payload_files(root)
    if set(files) != {ZIP_NAME, SETUP_NAME, "SHA256SUMS.txt"}:
        raise ValueError("Final candidate directory contains missing or unexpected artifacts")
    check_executable(root / SETUP_NAME)
    expected = "".join(f"{sha256(root / name)}  {name}\n" for name in (ZIP_NAME, SETUP_NAME))
    if (root / "SHA256SUMS.txt").read_text(encoding="ascii") != expected:
        raise ValueError("Final candidate checksums do not match")


def promote(source, destination):
    source = Path(source).resolve()
    destination = check_output(destination)
    if destination == source or source in destination.parents or destination in source.parents:
        raise ValueError("Output directory must not overlap the work directory")
    verify_final(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".{destination.name}.candidate-{uuid.uuid4().hex}"
    try:
        shutil.copytree(source, staging)
        verify_final(staging)
        check_output(destination)
        if destination.exists():
            destination.rmdir()  # Only an empty directory may be replaced.
        # Windows rename fails if another writer created the destination.
        os.rename(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def prepare(root, work, output, revision, dirty, dotnet_version, node_version):
    root, work = Path(root), Path(work)
    check_output(output)
    if sys.version_info[:2] != (3, 12):
        raise ValueError("Use Python 3.12 via -PythonExe (a dedicated build venv is recommended)")
    version = version_from_props(root)
    required_inputs = [
        "server.py", "README.md", "run-lodeskstream.ps1",
        "web/index.html", "web/input-queue.js", "web/keyboard-input.js",
        "web/icons/icon-512.png", "manager-src/LoDeskStreamManager.csproj",
        "installer-src/LoDeskStreamInstaller.csproj",
    ]
    for name in required_inputs:
        if not (root / Path(name)).is_file():
            raise ValueError(f"Missing source input: {name}")
    required_packages = requirement_pins(root / "requirements.txt")
    required_packages["PyInstaller"] = "6.22.3"
    missing = []
    mismatched = []
    for package, expected_version in required_packages.items():
        try:
            actual_version = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            missing.append(package)
        else:
            if actual_version != expected_version:
                mismatched.append(f"{package} {expected_version} required, found {actual_version}")
    if missing or mismatched:
        raise ValueError(
            f"Missing build/runtime packages: {', '.join(missing) or 'none'}. "
            f"Direct-version mismatches: {'; '.join(mismatched) or 'none'}. "
            "In a dedicated Python 3.12 build venv, run: "
            "python -m pip install -r requirements-build.txt; then pass -PythonExe. "
            "Do not modify the live server environment."
        )
    from PIL import Image
    work.mkdir(parents=True, exist_ok=True)
    info_path = work / "build-info.json"
    icon_path = work / "LoDeskStream.ico"
    if info_path.exists() or icon_path.exists():
        raise FileExistsError("Build metadata/icon staging must be fresh")
    if revision is not None and not re.fullmatch(r"[a-fA-F0-9]{40,64}", revision):
        raise ValueError("Invalid source revision")
    source = (
        {"kind": "git", "revision": revision, "dirty": dirty}
        if revision is not None else
        {"kind": "source-archive", "revision": None, "dirty": None,
         "note": "Git provenance unavailable in source archive"}
    )
    source.update(source_fingerprint(root))
    packages = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        if name:
            packages[name] = distribution.version
    write_json_exclusive(info_path, {
        "application": APPLICATION,
        "version": version,
        "distributionReady": False,
        "licensingStatus": "deferred",
        "candidateKind": "local-development",
        "source": source,
        "toolVersions": {
            "python": platform.python_version(),
            "pyinstaller": importlib.metadata.version("PyInstaller"),
            "dotnetSdk": dotnet_version,
            "node": node_version,
        },
        "dependencyInventory": {
            "purpose": "Installed package version inventory, NOT legal clearance or notices compliance",
            "packages": dict(sorted(packages.items(), key=lambda pair: pair[0].casefold())),
        },
    })
    with Image.open(root / "web" / "icons" / "icon-512.png") as image:
        with open(icon_path, "xb") as stream:
            image.convert("RGBA").save(
                stream, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (256, 256)]
            )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preparation = commands.add_parser("prepare")
    for name in ("root", "work", "output", "dotnet-version", "node-version"):
        preparation.add_argument(f"--{name}", required=True)
    preparation.add_argument("--revision")
    preparation.add_argument("--dirty", choices=("true", "false"), default="false")
    for command in ("manifest", "verify-manifest", "zip", "verify-zip"):
        sub = commands.add_parser(command)
        sub.add_argument("--root", required=True)
        sub.add_argument("--version", required=True)
        if command in ("zip", "verify-zip"):
            sub.add_argument("--archive", required=True)
    for command in ("checksums", "verify-final"):
        commands.add_parser(command).add_argument("--root", required=True)
    commands.add_parser("check-exe").add_argument("--path", required=True)
    source_check = commands.add_parser("verify-source")
    source_check.add_argument("--root", required=True)
    source_check.add_argument("--build-info", required=True)
    promotion = commands.add_parser("promote")
    promotion.add_argument("--source", required=True)
    promotion.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        prepare(args.root, args.work, args.output, args.revision, args.dirty == "true",
                args.dotnet_version, args.node_version)
    elif args.command == "manifest":
        create_manifest(args.root, args.version)
    elif args.command == "verify-manifest":
        verify_manifest(args.root, args.version)
    elif args.command == "zip":
        create_zip(args.root, args.archive, args.version)
    elif args.command == "verify-zip":
        verify_zip(args.archive, args.root, args.version)
    elif args.command == "checksums":
        create_checksums(args.root)
    elif args.command == "verify-final":
        verify_final(args.root)
    elif args.command == "check-exe":
        check_executable(args.path)
    elif args.command == "verify-source":
        verify_source(args.root, args.build_info)
    elif args.command == "promote":
        promote(args.source, args.output)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Packaging failed: {error}", file=sys.stderr)
        sys.exit(1)
