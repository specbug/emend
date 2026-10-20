"""Refresh quality evidence for the current bytes without undoing curated edits."""

import tempfile
import uuid
from pathlib import Path

from emend import ENGINE_VERSION
from emend.config import Config
from emend.inspect import inspect_epub, sha256_file
from emend.models import Gate, Report
from emend.pipeline import atomic_copy, get_report
from emend.reports import write_reports
from emend.research.discovery import current_source
from emend.storage import Store, now
from emend.validation import validate_ace, validate_epubcheck


def snapshot(source: Path, target: Path, limit: int):
    """Bound the copy even if an input grows while it is being read."""
    with source.open("rb") as src, target.open("wb") as dst:
        while chunk := src.read(min(1024**2, limit + 1)):
            limit -= len(chunk)
            if limit < 0:
                raise ValueError("Current EPUB exceeds the configured archive size limit.")
            dst.write(chunk)


def revalidate(book_id: str, config: Config) -> Report:
    store = Store(config.root)
    with store.locked():
        row, source, digest = current_source(store, book_id, config)
        previous = get_report(store, row["id"])
        book_id = row["id"]
        original = config.root / "originals" / f"{book_id}.epub"
        artifact = config.root / "curated" / f"{book_id}.epub"
        if previous.inspection.source_sha256 != digest:
            raise ValueError("Saved inspection and current EPUB identity disagree.")
        if row["artifact_path"] and source.resolve() != artifact.resolve():
            raise ValueError("Current artifact is not at its expected library path.")
        with tempfile.TemporaryDirectory(dir=config.root / ".staging") as staging:
            candidate = Path(staging) / "current.epub"
            snapshot(source, candidate, config.limits.max_archive_mb * 1024**2)
            if sha256_file(candidate) != digest or sha256_file(source) != digest:
                raise ValueError("Current EPUB changed while preparing revalidation.")
            directory = config.root / "reports" / book_id / uuid.uuid4().hex
            directory.mkdir(parents=True)
            pending_directory = directory / "pending"
            pending_directory.mkdir()
            pending = previous.model_copy(
                deep=True,
                update={
                    "created_at": now(),
                    "engine_version": ENGINE_VERSION,
                    "policy_sha256": config.policy_hash(),
                    "state": "REVIEW_REQUIRED",
                    "artifact_sha256": digest,
                    "revalidation": True,
                    "parent_report_path": row["report_path"],
                    "parent_artifact_sha256": digest,
                    "gates": [
                        Gate(
                            name="revalidation",
                            status="skipped",
                            required=True,
                            detail=f"Revalidation has not completed. Publication is blocked; retry emend revalidate {book_id[:12]}. Attempt: {directory}",
                        )
                    ],
                },
            )
            pending_path = write_reports(pending, pending_directory)
            # Commit revocation BEFORE inspection or external tools. If interrupted,
            # this report retains the source identity/history and is safe to retry.
            store.transition(
                book_id,
                row["source_name"],
                "REVIEW_REQUIRED",
                "Revalidation started; previous approval revoked, current bytes retained.",
                pending_path,
                source if row["artifact_path"] else None,
            )
            inspection = inspect_epub(candidate, config.limits)
            gates = [
                Gate(
                    name="structure",
                    status="failed" if inspection.blocking else "passed",
                    required=True,
                    detail="Current EPUB inspected without applying edits.",
                ),
                Gate(
                    name="preservation",
                    status="passed",
                    required=True,
                    detail="Current EPUB and stored original remain byte-identical to their recorded hashes.",
                ),
            ]
            if inspection.blocking:
                gates += [
                    Gate(
                        name=name,
                        status="skipped",
                        required=required,
                        detail="Not run because current EPUB inspection failed.",
                    )
                    for name, required in [
                        ("epubcheck", config.quality.require_epubcheck),
                        ("accessibility", config.quality.require_accessibility_audit),
                    ]
                ]
            else:
                gates += [
                    validate_epubcheck(candidate, directory, config),
                    validate_ace(candidate, directory, config),
                ]
            unchanged = (
                all(
                    path.is_file() and sha256_file(path) == expected
                    for path, expected in [
                        (original, book_id),
                        (source, digest),
                        (candidate, digest),
                    ]
                )
                and inspection.source_sha256 == digest
            )
            if not unchanged:
                gates[1] = Gate(
                    name="preservation",
                    status="failed",
                    required=True,
                    detail="An input changed during revalidation; publication blocked.",
                )
            gates.append(
                Gate(
                    name="visual_qa",
                    status="skipped",
                    required=False,
                    detail="Separate advisory visual reviews remain tied to their recorded artifact hashes.",
                )
            )
            failures = [g for g in gates if g.required and g.status != "passed"]
            state = (
                "FAILED"
                if any(g.status == "failed" for g in failures)
                else "REVIEW_REQUIRED"
                if failures
                else "VALIDATED"
            )
            result = pending.model_copy(
                update={
                    "created_at": now(),
                    "state": state,
                    "inspection": inspection,
                    "gates": gates,
                }
            )
            path = write_reports(result, directory)
            # Existing curated files are never replaced by revalidation. An initial
            # failed ingestion has only an original until its first successful audit.
            retained = source if row["artifact_path"] else None
            if state == "VALIDATED" and retained is None:
                atomic_copy(candidate, artifact)
                if sha256_file(artifact) != digest:
                    raise ValueError("Revalidated copy hash mismatch; publication remains blocked.")
                retained = artifact
            store.transition(
                book_id,
                row["source_name"],
                state,
                "Revalidation completed; current EPUB bytes and edit history retained.",
                path,
                retained,
            )
            return result
