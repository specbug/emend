import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from emend import ENGINE_VERSION
from emend.config import Config
from emend.inspect import inspect_epub, sha256_file
from emend.models import Gate, Report
from emend.reports import write_reports
from emend.storage import Store, now
from emend.validation import validate_ace, validate_epubcheck


@dataclass(frozen=True)
class KnownBook:
    book_id: str
    source_sha256: str


def known_book(store: Store, digest: str) -> KnownBook | None:
    """Called under the operation lock, including retained edited revisions."""
    with store.db() as db:
        row = db.execute("SELECT id FROM books WHERE id=?", (digest,)).fetchone()
    root = store.root
    if (
        row
        or (root / "curated" / f"{digest}.epub").exists()
        or (root / "versions" / digest).exists()
    ):
        return KnownBook(digest, digest)
    versions = root / "versions"
    if versions.is_dir():
        for book in versions.iterdir():
            if re.fullmatch(r"[a-f0-9]{64}", book.name) and (book / f"{digest}.epub").exists():
                return KnownBook(book.name, digest)
    return None


def atomic_copy(source: Path, target: Path):
    """Copy into the target filesystem, flush, then atomically publish the filename."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".emend-", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as dest, source.open("rb") as src:
            shutil.copyfileobj(src, dest)
            dest.flush()
            os.fsync(dest.fileno())
        os.chmod(temporary, 0o444)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def ingest(source: Path, config: Config) -> Report:
    result = _ingest(source, config, only_new=False)
    assert isinstance(result, Report)
    return result


def ingest_new(source: Path, config: Config) -> Report | KnownBook:
    """Import only an unknown original; the duplicate check shares the write lock."""
    return _ingest(source, config, only_new=True)


def _ingest(source: Path, config: Config, *, only_new: bool) -> Report | KnownBook:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"EPUB not found: {source}")
    if source.stat().st_size > config.limits.max_archive_mb * 1024**2:
        raise ValueError("Input exceeds the configured archive size limit.")
    store = Store(config.root)
    with store.locked():
        # Snapshot once: identity and every subsequent check refer to the same bytes.
        fd, name = tempfile.mkstemp(suffix=".epub", dir=config.root / ".staging")
        snapshot = Path(name)
        try:
            with os.fdopen(fd, "wb") as dest, source.open("rb") as src:
                remaining = config.limits.max_archive_mb * 1024**2
                while chunk := src.read(min(1024**2, remaining + 1)):
                    remaining -= len(chunk)
                    if remaining < 0:
                        raise ValueError("Input grew beyond the configured archive size limit.")
                    dest.write(chunk)
            book_id = sha256_file(snapshot)
            if only_new:
                if known := known_book(store, book_id):
                    return known
            original = config.root / "originals" / f"{book_id}.epub"
            if original.exists():
                if sha256_file(original) != book_id:
                    raise ValueError(
                        "Stored original hash mismatch. Restore the original from backup before proceeding."
                    )
            else:
                atomic_copy(snapshot, original)
        finally:
            snapshot.unlink(missing_ok=True)
        directory = config.root / "reports" / book_id / uuid.uuid4().hex
        directory.mkdir(parents=True)
        artifact = config.root / "curated" / f"{book_id}.epub"
        # Revalidation revokes the previous ready artifact until the gates pass again.
        artifact.unlink(missing_ok=True)
        store.transition(
            book_id, source.name, "IMPORTED", "Source snapshot stored and SHA-256 verified."
        )
        try:
            inspection = inspect_epub(original, config.limits)
            store.transition(
                book_id, source.name, "ANALYZED", f"{len(inspection.findings)} inspection findings."
            )
            gates = [
                Gate(
                    name="structure",
                    status="failed" if inspection.blocking else "passed",
                    required=True,
                    detail=f"{sum(f.severity == 'error' for f in inspection.findings)} blocking findings.",
                )
            ]
            unchanged = inspection.source_sha256 == book_id and sha256_file(original) == book_id
            gates.append(
                Gate(
                    name="preservation",
                    status="passed" if unchanged else "failed",
                    required=True,
                    detail="Original archive and all resources preserved byte for byte."
                    if unchanged
                    else "Original hash changed during inspection.",
                )
            )
            if inspection.blocking or not unchanged:
                for name, required in [
                    ("epubcheck", config.quality.require_epubcheck),
                    ("accessibility", config.quality.require_accessibility_audit),
                ]:
                    gates.append(
                        Gate(
                            name=name,
                            status="skipped",
                            required=required,
                            detail="Not run because inspection or preservation failed.",
                        )
                    )
            else:
                gates.extend(
                    [
                        validate_epubcheck(original, directory, config),
                        validate_ace(original, directory, config),
                    ]
                )
            gates.append(
                Gate(
                    name="visual_qa",
                    status="skipped",
                    required=False,
                    detail="Not run during ingestion. Use visual-review for separate advisory browser samples.",
                )
            )
            # Check again after external tools: they must not have changed the input.
            if sha256_file(original) != book_id:
                gates[1] = Gate(
                    name="preservation",
                    status="failed",
                    required=True,
                    detail="Source changed during validation; publication blocked.",
                )
            failures = [g for g in gates if g.required and g.status != "passed"]
            state = (
                "FAILED"
                if any(g.status == "failed" for g in failures)
                else "REVIEW_REQUIRED"
                if failures
                else "VALIDATED"
            )
            if state == "VALIDATED":
                atomic_copy(original, artifact)
                if sha256_file(artifact) != book_id:
                    artifact.unlink(missing_ok=True)
                    raise ValueError("Curated artifact does not match its preserved original.")
            report = Report(
                book_id=book_id,
                source_name=source.name,
                created_at=now(),
                engine_version=ENGINE_VERSION,
                policy_sha256=config.policy_hash(),
                state=state,
                inspection=inspection,
                gates=gates,
                artifact_sha256=book_id if state == "VALIDATED" else None,
            )
            report_path = write_reports(report, directory)
            store.transition(
                book_id,
                source.name,
                state,
                "Quality report saved.",
                report_path,
                artifact if state == "VALIDATED" else None,
            )
            return report
        except Exception as exc:
            artifact.unlink(missing_ok=True)
            (directory / "failure.txt").write_text(f"{type(exc).__name__}: {exc}\n")
            store.transition(book_id, source.name, "FAILED", f"{type(exc).__name__}: {exc}")
            raise


def get_report(store: Store, book_id: str) -> Report:
    row = store.resolve(book_id)
    if not row["report_path"]:
        raise ValueError(
            "This attempt has no completed report. Check the event log or re-ingest the original."
        )
    return Report.model_validate_json(Path(row["report_path"]).read_text())
