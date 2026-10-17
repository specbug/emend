"""Apply one reviewed cover while keeping every unrelated resource unchanged."""

import hashlib
import tempfile
import uuid
import zipfile
from pathlib import Path

from emend import ENGINE_VERSION
from emend.config import Config
from emend.covers import (
    aspect_changed,
    declared_cover,
    image_details,
    load_cover_review,
    read_image,
)
from emend.curation import edit_metadata, package_preserved
from emend.inspect import inspect_epub, sha256_file
from emend.models import CoverChange, Gate, Report
from emend.pipeline import atomic_copy, get_report
from emend.reports import write_reports
from emend.storage import Store, now
from emend.validation import validate_ace, validate_epubcheck


def replace_cover(source: Path, target: Path, package: str, cover_path: str, data: bytes) -> str:
    # Reuse the metadata writer only to advance EPUB 3's modification timestamp.
    intermediate = target.with_name("timestamp.epub")
    opf_hash = edit_metadata(source, intermediate, package, [])
    with zipfile.ZipFile(intermediate) as archive, zipfile.ZipFile(target, "w") as output:
        output.comment = archive.comment
        for info in archive.infolist():
            output.writestr(info, data if info.filename == cover_path else archive.read(info))
    intermediate.unlink()
    return opf_hash


def apply_cover(
    book_id: str,
    config: Config,
    *,
    review_id: str,
    approve: str,
    allow_smaller: bool = False,
    allow_aspect_change: bool = False,
) -> Report:
    store = Store(config.root)
    with store.locked():
        row = store.resolve(book_id)
        book_id = row["id"]
        previous = get_report(store, book_id)
        review = load_cover_review(store, book_id, review_id)
        selected = [c for c in review.candidates if c.id == approve]
        if len(selected) != 1:
            raise ValueError("Choose one candidate ID from the saved cover review.")
        candidate = selected[0]
        if (
            previous.state != "VALIDATED"
            or row["state"] not in {"VALIDATED", "PUBLISHED"}
            or any(g.required and g.status != "passed" for g in previous.gates)
        ):
            raise ValueError("Cover replacement requires a validated book.")
        if (
            previous.engine_version != ENGINE_VERSION
            or review.engine_version != ENGINE_VERSION
            or previous.policy_sha256 != config.policy_hash()
            or review.policy_sha256 != config.policy_hash()
        ):
            raise ValueError(
                "Quality policy or engine changed; revalidate and prepare a new cover review."
            )
        original = config.root / "originals" / f"{book_id}.epub"
        artifact = config.root / "curated" / f"{book_id}.epub"
        if (
            previous.book_id != book_id
            or sha256_file(original) != book_id
            or sha256_file(artifact) != previous.artifact_sha256
        ):
            raise ValueError("Source or artifact hash mismatch.")
        if previous.cover_review_id == review.id and previous.approved_cover_id == approve:
            return previous
        if previous.artifact_sha256 != review.artifact_sha256:
            raise ValueError("Cover review is stale; prepare a review for the current artifact.")
        if review.plan_id:
            from emend.research.service import load_plan

            load_plan(store, book_id, review.plan_id)
            if store.plan(book_id, review.plan_id)["sha256"] != review.plan_sha256:
                raise ValueError("Cover source research has changed; prepare another review.")
        baseline = inspect_epub(artifact, config.limits)
        if (
            baseline.blocking
            or not baseline.package_path
            or baseline.source_sha256 != review.artifact_sha256
        ):
            raise ValueError("Current artifact failed inspection.")
        declaration = declared_cover(artifact, baseline.package_path)
        if (
            not review.original
            or not declaration
            or declaration[0] != review.cover_path
            or baseline.resource_hashes.get(review.cover_path) != review.original.sha256
        ):
            raise ValueError(
                "Cover is missing, ambiguous, or changed; this path only replaces an existing declared image."
            )
        if candidate.media_type != declaration[1]:
            raise ValueError(
                "Candidate must use the same JPEG/PNG format as the declared cover. No implicit transcoding is performed."
            )
        if candidate.sha256 == review.original.sha256:
            raise ValueError(
                "Candidate is identical to the current cover; no replacement is needed."
            )
        if candidate.orientation != 1 or candidate.mode not in {"RGB", "RGBA", "L", "LA", "P"}:
            raise ValueError("Prepare a normally oriented RGB/grayscale image before replacement.")
        if not allow_smaller and (
            candidate.width < review.original.width or candidate.height < review.original.height
        ):
            raise ValueError(
                "Candidate is smaller than the current cover. Use --allow-smaller only after reviewing legibility."
            )
        if not allow_aspect_change and aspect_changed(review.original, candidate):
            raise ValueError(
                "Candidate changes aspect ratio by more than 5%. Review the cover-page layout before using --allow-aspect-change."
            )
        data = read_image(Path(candidate.path))
        details = image_details(data)
        if hashlib.sha256(data).hexdigest() != candidate.sha256 or any(
            details[k] != getattr(candidate, k)
            for k in ["media_type", "width", "height", "mode", "orientation"]
        ):
            raise ValueError("Candidate bytes or dimensions changed after review.")
        versions = config.root / "versions" / book_id
        snapshot = versions / f"{previous.artifact_sha256}.epub"
        if snapshot.exists() and sha256_file(snapshot) != previous.artifact_sha256:
            raise ValueError("Stored version hash mismatch.")
        atomic_copy(artifact, snapshot)
        directory = config.root / "reports" / book_id / uuid.uuid4().hex
        directory.mkdir(parents=True)
        with tempfile.TemporaryDirectory(dir=config.root / ".staging") as staging:
            output = Path(staging) / "candidate.epub"
            replaced = False
            try:
                opf_hash = replace_cover(
                    snapshot, output, baseline.package_path, review.cover_path, data
                )
                inspection = inspect_epub(output, config.limits)
                expected = dict(baseline.resource_hashes)
                expected[baseline.package_path] = opf_hash
                expected[review.cover_path] = candidate.sha256
                preserved = (
                    inspection.resource_hashes == expected
                    and package_preserved(snapshot, output, baseline.package_path, [])
                    and inspection.metadata == baseline.metadata
                    and inspection.spine == baseline.spine
                    and inspection.text_sha256 == baseline.text_sha256
                    and inspection.spine_text_sha256 == baseline.spine_text_sha256
                )
                gates = [
                    Gate(
                        name="structure",
                        status="failed" if inspection.blocking else "passed",
                        required=True,
                        detail="Cover candidate EPUB inspected.",
                    ),
                    Gate(
                        name="preservation",
                        status="passed" if preserved else "failed",
                        required=True,
                        detail="Only the declared cover image and EPUB 3 modification timestamp may change. Every other resource, metadata field, and reading-text fingerprint must match.",
                    ),
                ]
                if preserved and not inspection.blocking:
                    gates += [
                        validate_epubcheck(output, directory, config),
                        validate_ace(output, directory, config),
                    ]
                else:
                    gates += [
                        Gate(
                            name=name,
                            status="skipped",
                            required=required,
                            detail="Cover candidate failed inspection or preservation.",
                        )
                        for name, required in [
                            ("epubcheck", config.quality.require_epubcheck),
                            ("accessibility", config.quality.require_accessibility_audit),
                        ]
                    ]
                gates.append(
                    Gate(
                        name="visual_qa",
                        status="skipped",
                        required=False,
                        detail="Artwork selection was explicit; device rendering and visual quality are not certified.",
                    )
                )
                if (
                    sha256_file(output) != inspection.source_sha256
                    or sha256_file(original) != book_id
                    or sha256_file(artifact) != review.artifact_sha256
                ):
                    gates[1] = Gate(
                        name="preservation",
                        status="failed",
                        required=True,
                        detail="An input changed during validation.",
                    )
                failures = [g for g in gates if g.required and g.status != "passed"]
                state = (
                    "FAILED"
                    if any(g.status == "failed" for g in failures)
                    else "REVIEW_REQUIRED"
                    if failures
                    else "VALIDATED"
                )
                change = CoverChange(
                    review_id=review.id,
                    candidate_id=candidate.id,
                    resource_path=review.cover_path,
                    before_sha256=review.original.sha256,
                    after_sha256=candidate.sha256,
                    before_dimensions=[review.original.width, review.original.height],
                    after_dimensions=[candidate.width, candidate.height],
                    source=candidate.source,
                    source_url=candidate.source_url,
                    record_id=candidate.record_id,
                    allowed_smaller=allow_smaller,
                    allowed_aspect_change=allow_aspect_change,
                )
                report = previous.model_copy(
                    update=dict(
                        created_at=now(),
                        state=state,
                        inspection=inspection,
                        gates=gates,
                        artifact_sha256=inspection.source_sha256 if not failures else None,
                        changes=previous.changes
                        + [
                            f"Cover replaced explicitly: {review.original.sha256} → {candidate.sha256}. Source: {candidate.source}"
                        ],
                        cover_changes=previous.cover_changes + [change],
                        cover_review_id=review.id,
                        approved_cover_id=candidate.id,
                        parent_artifact_sha256=previous.artifact_sha256,
                        parent_report_path=row["report_path"],
                    )
                )
                report_path = write_reports(report, directory)
                if failures:
                    raise ValueError(
                        f"Cover candidate rejected; previous artifact retained. Evidence: {report_path}"
                    )
                atomic_copy(output, versions / f"{report.artifact_sha256}.epub")
                atomic_copy(output, artifact)
                replaced = True
                if sha256_file(artifact) != report.artifact_sha256:
                    raise ValueError("Cover candidate copy hash mismatch.")
                store.transition(
                    book_id,
                    row["source_name"],
                    "VALIDATED",
                    f"Applied cover {candidate.id} from review {review.id}.",
                    report_path,
                    artifact,
                )
                return report
            except Exception as exc:
                if replaced:
                    atomic_copy(snapshot, artifact)
                (directory / "failure.txt").write_text(f"{type(exc).__name__}: {exc}\n")
                store.event(book_id, "COVER_REJECTED", f"{exc}; evidence: {directory}")
                raise
