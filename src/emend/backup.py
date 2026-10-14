"""Versioned, verified whole-library backups. Calibre must be closed by the user."""

import hashlib
import io
import json
import os
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from emend.config import Config, executable
from emend.inspect import sha256_file
from emend.publish import command, remote_destination
from emend.storage import Store


def inventory(library: Path) -> dict[str, dict]:
    files = {}

    def unreadable(error):
        raise error

    # pathlib glob may suppress filesystem errors; a full-library backup must fail
    # instead of silently omitting an unreadable directory.
    for directory, folders, names in os.walk(library, onerror=unreadable, followlinks=False):
        for name in [*folders, *names]:
            path = Path(directory) / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError(f"Library contains a symlink: {path}. Resolve it before backup.")
            if stat.S_ISDIR(metadata.st_mode):
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"Unsupported library entry: {path}")
            files[path.relative_to(library).as_posix()] = {
                "size": metadata.st_size,
                "sha256": sha256_file(path),
                "mtime_ns": metadata.st_mtime_ns,
                "mode": stat.S_IMODE(metadata.st_mode) & 0o777,
            }
    return dict(sorted(files.items()))


def verify_backup(archive: Path) -> dict:
    """Check every archived byte and the copied Calibre database, without extraction."""
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            names = [m.name for m in members]
            if len(names) != len(set(names)) or "manifest.json" not in names:
                raise ValueError("Backup contains duplicate entries or lacks a manifest.")
            for member in members:
                path = PurePosixPath(member.name)
                if (
                    not member.isfile()
                    or path.is_absolute()
                    or ".." in path.parts
                    or "\\" in member.name
                    or str(path) != member.name
                ):
                    raise ValueError("Backup contains an unsafe archive member.")
            manifest_member = tar.getmember("manifest.json")
            if manifest_member.size > 32 * 1024**2:
                raise ValueError("Backup manifest is too large.")
            with tar.extractfile(manifest_member) as stream:
                manifest = json.load(stream)
            if (
                not isinstance(manifest, dict)
                or manifest.get("version") != 1
                or not isinstance(manifest.get("files"), dict)
            ):
                raise ValueError("Unsupported backup manifest.")
            expected = {"library/" + name: info for name, info in manifest["files"].items()}
            if (
                set(names) != set(expected) | {"manifest.json"}
                or "library/metadata.db" not in expected
            ):
                raise ValueError("Backup contents do not match the manifest.")
            for name, info in expected.items():
                member = tar.getmember(name)
                with tar.extractfile(member) as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                if member.size != info["size"] or digest != info["sha256"]:
                    raise ValueError(f"Backup checksum mismatch: {name}")
            with tempfile.TemporaryDirectory(prefix="emend-verify-") as temporary:
                db_path = Path(temporary) / "metadata.db"
                with tar.extractfile("library/metadata.db") as src, db_path.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                db = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
                try:
                    results = db.execute("PRAGMA quick_check").fetchall()
                    if (
                        results != [("ok",)]
                        or not db.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name='books'"
                        ).fetchone()
                    ):
                        raise ValueError("Calibre database did not pass its integrity check.")
                finally:
                    db.close()
            return manifest
    except (tarfile.TarError, sqlite3.Error, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid backup: {exc}") from exc


def backup_library(config: Config, calibre_closed: bool, upload: bool = False) -> dict:
    if not calibre_closed:
        raise ValueError(
            "Quit Calibre, its Content Server, and other library writers, then use --calibre-closed."
        )
    library = config.publish.calibre_library
    if library is None or not (library / "metadata.db").is_file():
        raise ValueError("Configure publish.calibre_library with an existing Calibre library.")
    output = config.root / "backups"
    if output.resolve().is_relative_to(library.resolve()):
        raise ValueError("Emend backups must be outside the Calibre library.")
    store = Store(config.root)
    with store.locked():
        for suffix in ["-wal", "-journal"]:
            if (library / ("metadata.db" + suffix)).exists():
                raise ValueError(
                    "Calibre database has a WAL/journal file. Close all library writers and retry."
                )
        output.mkdir(exist_ok=True)
        before = inventory(library)
        manifest = {
            "version": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "files": before,
            "scope": "Complete Calibre library folder; application preferences are separate.",
        }
        name = (
            "calibre-"
            + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            + "-"
            + uuid.uuid4().hex[:8]
            + ".tar.gz"
        )
        final = output / name
        temporary = output / ("." + name + ".partial")
        try:
            with tarfile.open(temporary, "w:gz") as tar:
                for relative in before:
                    path = library / relative
                    tar.add(path, arcname="library/" + relative, recursive=False)
                data = json.dumps(manifest, sort_keys=True, indent=2).encode()
                member = tarfile.TarInfo("manifest.json")
                member.size = len(data)
                member.mode = 0o600
                tar.addfile(member, io.BytesIO(data))
            if inventory(library) != before:
                raise ValueError(
                    "Library changed during backup. Close all library writers and retry."
                )
            verify_backup(temporary)
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, final)
        finally:
            temporary.unlink(missing_ok=True)
        receipt = {
            "archive": str(final),
            "sha256": sha256_file(final),
            "files": len(before),
            "verified": True,
            "cloud_status": "not_requested",
        }
        if upload:
            try:
                receipt.update(upload_backup(final, config))
            except (ValueError, OSError) as exc:
                receipt.update(cloud_status="pending", error=str(exc))
        final.with_suffix(final.suffix + ".receipt.json").write_text(json.dumps(receipt, indent=2))
        return receipt


def upload_backup(archive: Path, config: Config) -> dict:
    verify_backup(archive)
    tool = executable("rclone", config)
    remote = config.publish.backup_remote
    if not tool or not remote:
        raise ValueError(
            "Install rclone and configure publish.backup_remote, e.g. gdrive:Emend/backups."
        )
    destination = remote_destination(remote, archive.name)
    command(
        [tool, "copyto", "--checksum", str(archive.resolve()), destination],
        config.limits.publish_timeout_seconds,
    )
    return {"cloud_status": "uploaded", "destination": destination}


def restore_backup(archive: Path, destination: Path) -> Path:
    """Restore to a new directory only. Never overwrite an existing library."""
    manifest = verify_backup(archive)
    destination = destination.expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("Restore destination must not exist; choose a new folder.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".emend-restore-", dir=destination.parent))
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for relative, info in manifest["files"].items():
                path = temporary / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile("library/" + relative) as src, path.open("xb") as dst:
                    shutil.copyfileobj(src, dst)
                if sha256_file(path) != info["sha256"]:
                    raise ValueError("Restore checksum mismatch.")
                if "mode" in info:
                    path.chmod(int(info["mode"]) & 0o777)
                if "mtime_ns" in info:
                    os.utime(path, ns=(int(info["mtime_ns"]), int(info["mtime_ns"])))
        # An atomic reservation prevents replacing a path created by another process.
        destination.mkdir()
        try:
            os.replace(temporary, destination)
        except Exception:
            destination.rmdir()
            raise
        return destination
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
