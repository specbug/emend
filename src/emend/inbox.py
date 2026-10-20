"""Bounded foreground polling of an EPUB inbox; importing never resets known books."""

import math
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep
from typing import Literal

from pydantic import BaseModel

from emend.config import Config
from emend.pipeline import KnownBook, ingest_new
from emend.storage import OperationBusyError, Store

MAX_ENTRIES = 10_000


class InboxEvent(BaseModel):
    source: str
    status: Literal["waiting", "imported", "known", "blocked", "busy", "error"]
    detail: str
    book_id: str | None = None
    source_sha256: str | None = None
    state: str | None = None


@dataclass
class Observation:
    signature: tuple
    since: float
    done: bool = False
    status: str | None = None


class FileChanged(ValueError):
    pass


def signature(metadata: os.stat_result) -> tuple:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def stable_copy(source: Path, target: Path, expected: tuple, limit: int):
    # A replaced symlink/FIFO must not redirect or block a file read between polls.
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as src:
        if signature(os.fstat(src.fileno())) != expected:
            raise FileChanged("Input changed before copying.")
        with target.open("xb") as dst:
            while chunk := src.read(min(1024**2, limit + 1)):
                limit -= len(chunk)
                if limit < 0:
                    raise ValueError("EPUB exceeds the configured archive size limit.")
                dst.write(chunk)
        if signature(os.fstat(src.fileno())) != expected or signature(source.lstat()) != expected:
            raise FileChanged("Input changed while copying.")


class Inbox:
    def __init__(self, config: Config, directory: Path | None = None, *, stable_seconds: float = 2):
        if not math.isfinite(stable_seconds) or not 0.1 <= stable_seconds <= 300:
            raise ValueError("Stable interval must be between 0.1 and 300 seconds.")
        self.config = config
        self.store = Store(config.root)
        root = config.root.resolve()
        self.directory = (directory or root / "inbox").expanduser().resolve()
        if not self.directory.is_dir():
            raise ValueError("Inbox directory does not exist.")
        if self.directory.is_relative_to(root) and not self.directory.is_relative_to(
            root / "inbox"
        ):
            raise ValueError("Use the inbox folder or a directory outside Emend's managed data.")
        library = config.publish.calibre_library
        if library and self.directory.is_relative_to(library.resolve()):
            raise ValueError("Inbox must be outside the active Calibre library.")
        self.stable_seconds = stable_seconds
        self.observations: dict[Path, Observation] = {}

    def scan(self, *, now: float | None = None) -> list[InboxEvent]:
        now = monotonic() if now is None else now
        entries = []
        for entry in self.directory.iterdir():
            if len(entries) >= MAX_ENTRIES:
                raise ValueError(
                    f"Inbox exceeds the {MAX_ENTRIES:,}-entry limit; choose a smaller folder."
                )
            entries.append(entry)
        events = []
        active = set()

        def emit(path, observation, status, detail, **fields):
            if observation.status != status:
                events.append(InboxEvent(source=str(path), status=status, detail=detail, **fields))
            observation.status = status

        for path in sorted(entries, key=lambda p: (p.name.casefold(), p.name)):
            if path.name.startswith(".") or path.suffix.lower() != ".epub":
                continue
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(metadata.st_mode):
                continue
            active.add(path)
            current = signature(metadata)
            observation = self.observations.get(path)
            if observation is None or observation.signature != current:
                observation = Observation(
                    current, now, status=observation.status if observation else None
                )
                self.observations[path] = observation
            if observation.done:
                continue
            if now - observation.since < self.stable_seconds:
                emit(path, observation, "waiting", "Waiting for the file to remain unchanged.")
                continue
            try:
                with self.store.locked():
                    pass  # Avoid copying large files while another operation is busy.
                if metadata.st_size > self.config.limits.max_archive_mb * 1024**2:
                    raise ValueError("EPUB exceeds the configured archive size limit.")
                with tempfile.TemporaryDirectory(dir=self.config.root / ".staging") as staging:
                    snapshot = Path(staging) / path.name
                    try:
                        stable_copy(
                            path, snapshot, current, self.config.limits.max_archive_mb * 1024**2
                        )
                    except FileNotFoundError as exc:
                        raise FileChanged("Input disappeared while copying.") from exc
                    report = ingest_new(snapshot, self.config)
                if isinstance(report, KnownBook):
                    emit(
                        path,
                        observation,
                        "known",
                        "Existing book or retained revision; left unchanged. Inspect its report/event log if it needs attention.",
                        book_id=report.book_id,
                        source_sha256=report.source_sha256,
                    )
                else:
                    emit(
                        path,
                        observation,
                        "imported" if report.state == "VALIDATED" else "blocked",
                        "Original preserved and quality report recorded. Publishing remains explicit.",
                        book_id=report.book_id,
                        source_sha256=report.inspection.source_sha256,
                        state=report.state,
                    )
                observation.done = True
            except FileChanged:
                observation.since = now
                emit(
                    path,
                    observation,
                    "waiting",
                    "Input changed during processing; waiting for a stable copy.",
                )
            except OperationBusyError:
                emit(
                    path,
                    observation,
                    "busy",
                    "Another Emend operation is running; retrying on the next scan.",
                )
            except (OSError, ValueError) as exc:
                observation.done = True
                emit(path, observation, "error", str(exc))
        self.observations = {
            path: item for path, item in self.observations.items() if path in active
        }
        return events

    def batch(self) -> list[InboxEvent]:
        """One bounded observation window; changing files are deferred to a later run."""
        latest = {event.source: event for event in self.scan()}
        if any(not item.done for item in self.observations.values()):
            sleep(self.stable_seconds)
            latest.update({event.source: event for event in self.scan()})
        for path, item in self.observations.items():
            if not item.done:
                latest.setdefault(
                    str(path),
                    InboxEvent(
                        source=str(path),
                        status=item.status or "waiting",
                        detail="Input is pending; retry after the file settles or other Emend operations finish.",
                    ),
                )
        return [
            event
            for event in latest.values()
            if event.status not in {"waiting", "busy"} or Path(event.source) in self.observations
        ]
