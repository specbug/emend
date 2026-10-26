"""Explicit byte-verified delivery to an existing mounted-reader folder; never overwrite."""

import ctypes
import errno
import hashlib
import json
import os
import sqlite3
import stat
import sys
import unicodedata
import uuid
from pathlib import Path

from emend.config import Config
from emend.publish import approved_artifact
from emend.storage import Store


class ReaderDeliveryError(ValueError):
    pass


def default_filename(report):
    title = "; ".join(report.inspection.metadata.get("title", [])) or "book"
    author = "; ".join(report.inspection.metadata.get("creator", []))
    label = unicodedata.normalize("NFC", title + (" - " + author if author else ""))
    label = "".join(
        " " if c in '/\\<>:"|?*' or unicodedata.category(c).startswith("C") else c for c in label
    )
    label = " ".join(label.split()).strip(" .") or "book"
    while len(label.encode("utf-8")) > 160:
        label = label[:-1]
    return f"{label} [{report.artifact_sha256[:16]}].epub"


def check_filename(name):
    if (
        not name
        or name.startswith(".")
        or Path(name).name != name
        or Path(name).suffix.lower() != ".epub"
        or len(name.encode()) > 240
        or any(c in '/\\<>:"|?*' or unicodedata.category(c).startswith("C") for c in name)
    ):
        raise ValueError(
            "Choose a plain EPUB filename without path separators/control characters (up to 240 UTF-8 bytes)."
        )
    return name


def atomic_noreplace(folder_fd, source_name, destination_name):
    # Native no-clobber rename works on mounted filesystems without hard links.
    # Darwin declarations: apple-oss-distributions/xnu/bsd/sys/stdio.h (RENAME_EXCL=4).
    if sys.platform == "linux":
        function_name, flag = "renameat2", 1
    elif sys.platform == "darwin":
        function_name, flag = "renameatx_np", 4
    else:
        raise ValueError("Safe mounted-folder delivery currently supports macOS and Linux only.")
    library = ctypes.CDLL(None, use_errno=True)
    function = getattr(library, function_name, None)
    if function is None:
        raise ValueError("Native no-overwrite rename is unavailable; use a manual file transfer.")
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    if (
        function(
            folder_fd, os.fsencode(source_name), folder_fd, os.fsencode(destination_name), flag
        )
        != 0
    ):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), destination_name)


def file_hash_at(folder_fd, name, expected_size=None):
    try:
        metadata = os.stat(name, dir_fd=folder_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if expected_size is not None and metadata.st_size != expected_size:
        return "different-size"
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(
            "Destination name is occupied by a symlink or special file; choose another filename."
        )
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder_fd)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (metadata.st_dev, metadata.st_ino) != (before.st_dev, before.st_ino):
            raise ReaderDeliveryError("Destination changed while it was being opened.")
        digest_builder = hashlib.sha256()
        remaining = before.st_size
        while chunk := stream.read(min(1024**2, remaining + 1)):
            remaining -= len(chunk)
            if remaining < 0:
                raise ReaderDeliveryError("Destination grew while being verified.")
            digest_builder.update(chunk)
        digest = digest_builder.hexdigest()
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ReaderDeliveryError("Destination changed while it was being verified.")
        return digest


def check_directory(directory, folder_fd):
    expected = os.fstat(folder_fd)
    try:
        visible = directory.stat(follow_symlinks=False)
    except OSError as exc:
        raise ReaderDeliveryError("Reader folder disappeared; reconnect and retry.") from exc
    if not stat.S_ISDIR(visible.st_mode) or (visible.st_dev, visible.st_ino) != (
        expected.st_dev,
        expected.st_ino,
    ):
        raise ReaderDeliveryError(
            "Reader folder changed or was remounted during delivery; retry after reconnecting."
        )


def export_reader(
    book_id: str,
    directory: Path,
    config: Config,
    *,
    filename: str | None = None,
    expected_artifact_sha256: str | None = None,
):
    directory = directory.expanduser().resolve(strict=True)
    if not directory.is_dir():
        raise ValueError("Choose an existing reader folder.")
    if directory.is_relative_to(config.root.resolve()):
        raise ValueError("Reader destination must be outside Emend managed data.")
    library = config.publish.calibre_library
    if library and directory.is_relative_to(library.resolve()):
        raise ValueError("Reader destination must be outside the active Calibre library.")
    # Compare actual directory identities as well as strings (macOS volumes
    # commonly have case-insensitive paths).
    forbidden = [config.root, *([library] if library else [])]
    identities = set()
    for root in forbidden:
        try:
            info = root.stat()
            identities.add((info.st_dev, info.st_ino))
        except FileNotFoundError:
            pass
    for parent in (directory, *directory.parents):
        info = parent.stat()
        if (info.st_dev, info.st_ino) in identities:
            raise ValueError("Reader destination must be outside managed Emend/Calibre data.")
    store = Store(config.root)
    with store.locked():
        row, report, artifact = approved_artifact(
            store, book_id, config, expected_artifact_sha256=expected_artifact_sha256
        )
        filename = check_filename(filename if filename is not None else default_filename(report))
        folder_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        temporary = ".emend-" + uuid.uuid4().hex + ".part"
        expected_size = artifact.stat().st_size
        try:
            check_directory(directory, folder_fd)
            existing = file_hash_at(folder_fd, filename, expected_size)
            if existing is not None and existing != report.artifact_sha256:
                raise ValueError(
                    "A different file already exists at that reader filename; choose another name. Nothing was overwritten."
                )
            already = existing == report.artifact_sha256
            if not already:
                descriptor = os.open(
                    temporary,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o644,
                    dir_fd=folder_fd,
                )
                with os.fdopen(descriptor, "w+b") as destination:
                    source_fd = os.open(artifact, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(source_fd, "rb") as source:
                        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                            raise ReaderDeliveryError("Approved source changed before copying.")
                        remaining = config.limits.max_archive_mb * 1024**2
                        while chunk := source.read(min(1024**2, remaining + 1)):
                            remaining -= len(chunk)
                            if remaining < 0:
                                raise ReaderDeliveryError(
                                    "Source grew beyond the configured archive limit."
                                )
                            destination.write(chunk)
                    destination.flush()
                    os.fsync(destination.fileno())
                    destination.seek(0)
                    if (
                        hashlib.file_digest(destination, "sha256").hexdigest()
                        != report.artifact_sha256
                    ):
                        raise ReaderDeliveryError("Reader staging copy hash mismatch.")
                approved_artifact(
                    store, row["id"], config, expected_artifact_sha256=report.artifact_sha256
                )
                check_directory(directory, folder_fd)
                try:
                    atomic_noreplace(folder_fd, temporary, filename)
                except FileExistsError as exc:
                    if file_hash_at(folder_fd, filename, expected_size) != report.artifact_sha256:
                        raise ReaderDeliveryError(
                            "Another file appeared at the destination; nothing was overwritten."
                        ) from exc
                    already = True
            check_directory(directory, folder_fd)
            if file_hash_at(folder_fd, filename, expected_size) != report.artifact_sha256:
                raise ReaderDeliveryError("Reader copy failed final byte verification.")
            # Reopen the currently visible path as well as the held directory descriptor.
            visible_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                if file_hash_at(visible_fd, filename, expected_size) != report.artifact_sha256:
                    raise ReaderDeliveryError(
                        "The currently visible reader copy could not be verified."
                    )
            finally:
                os.close(visible_fd)
            synced = True
            try:
                os.fsync(folder_fd)
            except OSError as exc:
                if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EBADF}:
                    raise
                synced = False
            receipt = {
                "directory": str(directory),
                "filename": filename,
                "sha256": report.artifact_sha256,
                "directory_fsync": synced,
                "verification": "Exact EPUB bytes; no reader/firmware compatibility claim.",
            }
            key = (
                "reader:" + hashlib.sha256((str(directory) + "\0" + filename).encode()).hexdigest()
            )
            store.save_receipt(row["id"], key, "published", receipt)
            store.transition(
                row["id"],
                row["source_name"],
                "PUBLISHED",
                "Reader export verified: " + json.dumps(receipt),
                Path(row["report_path"]),
                artifact,
            )
            return {"status": "already_exported" if already else "exported", "receipt": receipt}
        except (OSError, sqlite3.Error, ReaderDeliveryError) as exc:
            try:
                store.event(row["id"], "READER_DELIVERY_FAILED", str(exc))
            except sqlite3.Error:
                pass
            if isinstance(exc, ReaderDeliveryError):
                raise
            raise ReaderDeliveryError(
                f"Reader delivery did not finish; reconnect/inspect the folder and retry. {exc}"
            ) from exc
        finally:
            try:
                os.unlink(temporary, dir_fd=folder_fd)
            except OSError:
                # A disconnected/read-only volume may retain a hidden .part file;
                # never remove or alter the verified visible destination as cleanup.
                pass
            finally:
                os.close(folder_fd)
