"""Explicit recovery from original or a previously approved, retained revision."""

import os
import re
import stat
import tempfile
import uuid
from pathlib import Path

from emend import ENGINE_VERSION
from emend.config import Config
from emend.inspect import inspect_epub, sha256_file
from emend.models import Gate, RecoveryChange, Report
from emend.pipeline import atomic_copy
from emend.reports import write_reports
from emend.revalidation import revalidate, snapshot
from emend.storage import Store, now

REPORT_LIMIT = 8 * 1024**2
REPORT_COUNT = 1000


def read_report(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > REPORT_LIMIT:
        raise ValueError("Recovery report is unavailable or exceeds its size limit.")
    with path.open("rb") as stream:
        data = stream.read(REPORT_LIMIT + 1)
    if len(data) > REPORT_LIMIT:
        raise ValueError("Recovery report grew beyond its size limit.")
    return Report.model_validate_json(data)


def approved(report, book):
    gates = {g.name: g for g in report.gates}
    return (
        report.book_id == book
        and report.state == "VALIDATED"
        and report.artifact_sha256 == report.inspection.source_sha256
        and not report.inspection.blocking
        and {"structure", "preservation", "epubcheck", "accessibility"} <= gates.keys()
        and all(g.status == "passed" for g in report.gates if g.required)
        and gates["structure"].required
        and gates["preservation"].required
    )


def candidates(store, row):
    """Saved successful reports identify revisions; filenames alone never approve them."""
    book = row["id"]
    results = {book: None}
    directory = store.root / "reports" / book
    if not directory.is_dir():
        return results
    count = 0
    for child in directory.iterdir():
        if (
            not re.fullmatch(r"[a-f0-9]{32}", child.name)
            or child.is_symlink()
            or not child.is_dir()
        ):
            continue
        count += 1
        if count > REPORT_COUNT:
            raise ValueError(
                "Recovery scan exceeds 1000 report directories; archive older evidence first."
            )
        path = child / "report.json"
        try:
            report = read_report(path)
        except (ValueError, OSError):
            continue
        digest = report.artifact_sha256
        if digest and re.fullmatch(r"[a-f0-9]{64}", digest) and approved(report, book):
            # For a repeated revalidation, retain the newest approved history.
            prior = results.get(digest)
            if prior is None or report.created_at > prior.created_at:
                results[digest] = report
    return results


def regular(path, limit):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ValueError(
            "Recovery needs a regular bounded source file; restore a verified backup instead."
        )


def recovery_options(book_id, config: Config):
    store = Store(config.root)
    row = store.resolve(book_id)
    result = []
    for digest, report in candidates(store, row).items():
        path = (
            store.root
            / ("originals" if digest == row["id"] else f"versions/{row['id']}")
            / f"{digest}.epub"
        )
        if path.is_file() and not path.is_symlink():
            result.append(
                {
                    "sha256": digest,
                    "original": digest == row["id"],
                    "approved_at": report.created_at if report else None,
                }
            )
    return result


def recover(
    book_id,
    config: Config,
    *,
    revision: str,
    approve: bool = False,
    expected_report_path: str | None = None,
):
    if not approve:
        raise ValueError(
            "Recovery replaces the current artifact; explicitly pass --approve after choosing a revision."
        )
    store = Store(config.root)
    with store.locked():
        row = store.resolve(book_id)
        book = row["id"]
        if expected_report_path is not None and (row["report_path"] or "") != expected_report_path:
            raise ValueError("Recovery page is stale; reopen it before choosing a revision.")
        digest = book if revision == "original" else revision
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Choose original or a full retained revision SHA-256.")
        known = candidates(store, row)
        if digest not in known:
            raise ValueError(
                "Revision has no saved successful approval; unknown or rejected bytes cannot be adopted."
            )
        original = store.root / "originals" / f"{book}.epub"
        limit = config.limits.max_archive_mb * 1024**2
        regular(original, limit)
        if sha256_file(original) != book:
            raise ValueError(
                "Stored original hash mismatch; restore a verified library backup first."
            )
        source = original if digest == book else store.root / "versions" / book / f"{digest}.epub"
        regular(source, limit)
        with tempfile.TemporaryDirectory(dir=store.root / ".staging") as staging:
            staged = Path(staging) / "recovery.epub"
            snapshot(source, staged, limit)
            if sha256_file(staged) != digest or sha256_file(source) != digest:
                raise ValueError(
                    "Retained revision hash mismatch; restore a verified backup first."
                )
            inspection = inspect_epub(staged, config.limits)
            if inspection.source_sha256 != digest:
                raise ValueError("Recovery snapshot identity mismatch.")
            attempt = uuid.uuid4().hex
            directory = store.root / "reports" / book / attempt
            directory.mkdir(parents=True)
            quarantine = store.root / "quarantine" / book / f"{attempt}.epub"
            previous = known[digest]
            change = RecoveryChange(
                revision_sha256=digest,
                original=digest == book,
                previous_report_path=row["report_path"],
                quarantine_path=str(quarantine),
                created_at=now(),
            )
            base = previous or Report(
                book_id=book,
                source_name=row["source_name"],
                created_at=now(),
                engine_version=ENGINE_VERSION,
                policy_sha256=config.policy_hash(),
                state="REVIEW_REQUIRED",
                inspection=inspection,
                gates=[],
            )
            pending = base.model_copy(
                deep=True,
                update={
                    "created_at": now(),
                    "engine_version": ENGINE_VERSION,
                    "policy_sha256": config.policy_hash(),
                    "state": "REVIEW_REQUIRED",
                    "inspection": inspection,
                    "artifact_sha256": digest,
                    "revalidation": False,
                    "parent_report_path": row["report_path"],
                    "parent_artifact_sha256": None,
                    "recovery_changes": [*base.recovery_changes, change],
                    "changes": [
                        *base.changes,
                        f"Explicit recovery selected {digest}; approval revoked before replacement. Prior artifact quarantine (if present): {quarantine}.",
                    ],
                    "gates": [
                        Gate(
                            name="recovery",
                            status="skipped",
                            required=True,
                            detail="Recovery is pending. Publication is blocked until the selected revision is copied and revalidated.",
                        )
                    ],
                },
            )
            path = write_reports(pending, directory)
            artifact = store.root / "curated" / f"{book}.epub"
            # Persist revocation first. A process kill at any later point remains blocked.
            store.transition(
                book,
                row["source_name"],
                "REVIEW_REQUIRED",
                "Explicit recovery started; previous approval revoked.",
                path,
                artifact,
            )
            if artifact.exists() or artifact.is_symlink():
                if not (stat.S_ISREG(artifact.lstat().st_mode) or artifact.is_symlink()):
                    raise ValueError(
                        "Current artifact is not a regular file; publication remains blocked."
                    )
                quarantine.parent.mkdir(parents=True, exist_ok=True)
                os.replace(artifact, quarantine)
            atomic_copy(staged, artifact)
            if sha256_file(artifact) != digest or sha256_file(original) != book:
                raise ValueError(
                    "Recovery activation identity mismatch; publication remains blocked."
                )
            store.event(
                book, "RECOVERED", f"Selected {digest}; revalidation required. Attempt: {directory}"
            )
    # The separate operation acquires its own lock and revokes/checks approval again.
    # If interrupted between calls, the pending recovery report is retryable.
    return revalidate(book, config)
