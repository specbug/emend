"""Explicit, fingerprinted container repairs; publication resource bytes stay intact."""

import copy
import html
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from emend import ENGINE_VERSION
from emend.config import Config
from emend.inspect import inspect_epub, sha256_file
from emend.models import Gate, RepairChange, Report
from emend.pipeline import atomic_copy, get_report
from emend.reports import report_styles, write_reports
from emend.research.discovery import current_source
from emend.revalidation import snapshot
from emend.storage import Store, now
from emend.validation import validate_ace, validate_epubcheck

ACTION = "container.mimetype"
DESCRIPTION = (
    "Move the existing mimetype entry first, store it uncompressed, and remove its ZIP extra "
    "fields. Keep its content and every other resource byte unchanged."
)


class ContainerState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    position: int = Field(ge=1)
    compression: str
    extra_bytes: int = Field(ge=0)


class RepairPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    book_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    input_sha256: str
    report_sha256: str
    engine_version: str
    policy_sha256: str
    created_at: str
    actions: list[Literal["container.mimetype"]]
    before: ContainerState | None = None
    after: ContainerState | None = None
    detail: str
    findings: list[str]


def proposed_actions(inspection) -> list[str]:
    errors = {f.code for f in inspection.findings if f.severity == "error"}
    # Never try to rehabilitate unreadable, unsafe, or otherwise damaged containers.
    return [ACTION] if errors == {"zip.mimetype_order"} else []


def prepare_repairs(book_id: str, config: Config) -> tuple[RepairPlan, Path]:
    store = Store(config.root)
    with store.locked():
        row, source, digest = current_source(store, book_id, config)
        with tempfile.TemporaryDirectory(dir=config.root / ".staging") as staging:
            current = Path(staging) / "current.epub"
            snapshot(source, current, config.limits.max_archive_mb * 1024**2)
            inspection = inspect_epub(current, config.limits)
            if inspection.source_sha256 != digest or sha256_file(source) != digest:
                raise ValueError("Current EPUB changed while preparing repairs.")
            actions = proposed_actions(inspection)
            before = None
            if actions:
                with zipfile.ZipFile(current) as archive:
                    mime = archive.getinfo("mimetype")
                    before = ContainerState(
                        position=archive.namelist().index("mimetype") + 1,
                        compression={0: "stored", 8: "deflated"}.get(
                            mime.compress_type, f"ZIP method {mime.compress_type}"
                        ),
                        extra_bytes=len(mime.extra),
                    )
            plan = RepairPlan(
                id=uuid.uuid4().hex,
                book_id=row["id"],
                input_sha256=digest,
                report_sha256=sha256_file(Path(row["report_path"])),
                engine_version=ENGINE_VERSION,
                policy_sha256=config.policy_hash(),
                created_at=now(),
                actions=actions,
                before=before,
                after=ContainerState(position=1, compression="stored", extra_bytes=0)
                if actions
                else None,
                detail=DESCRIPTION,
                findings=[f"{f.code}: {f.message}"[:2000] for f in inspection.findings[:200]],
            )
        directory = config.root / "repairs" / row["id"] / plan.id
        directory.mkdir(parents=True)
        path = directory / "plan.json"
        path.write_text(plan.model_dump_json(indent=2))
        esc = html.escape
        action = (
            f"<p><code>{ACTION}</code>: {esc(DESCRIPTION)}</p>"
            if plan.actions
            else "<p>No supported repair. Other structural errors require manual review.</p>"
        )
        if plan.before:
            action += (
                "<h2>Proposed changes</h2><ul>"
                f"<li>mimetype entry position: {plan.before.position} → 1.</li>"
                f"<li>Compression: {esc(plan.before.compression)} → stored.</li>"
                f"<li>ZIP extra field bytes: {plan.before.extra_bytes} → 0.</li>"
                "</ul>"
            )
        findings = "".join(f"<li>{esc(f)}</li>" for f in plan.findings)
        (directory / "review.html").write_text(
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>Container repair review</title><style>{report_styles()}</style></head>"
            '<body><main><p class="brand">Emend · explicit repair review</p>'
            f"<h1>Container repair</h1><p>{esc(row['source_name'])}</p>"
            f"<p>Input SHA-256: <code>{digest}</code></p>{action}"
            "<p>This proposal has not changed the EPUB or its approval state. Applying requires "
            "explicit approval and fresh validation. Typography and reading content stay intact; "
            "this does not certify publication quality or device compatibility.</p>"
            f"<h2>Inspection findings</h2><p>First 200 findings, up to 2,000 characters each. Full findings remain in the quality report.</p><ul>{findings}</ul></main></body></html>"
        )
        store.save_document(plan.id, row["id"], "repairs", path, sha256_file(path))
        return plan, directory / "review.html"


def load_repairs(store: Store, book_id: str, identifier: str | None = None) -> RepairPlan:
    row = store.resolve(book_id)
    document = store.document(row["id"], "repairs", identifier)
    path = Path(document["path"])
    expected = store.root / "repairs" / row["id"] / document["id"] / "plan.json"
    if path.resolve() != expected.resolve() or path.stat().st_size > 4 * 1024**2:
        raise ValueError("Invalid repair plan location or size.")
    if sha256_file(path) != document["sha256"]:
        raise ValueError("Repair plan changed after registration; prepare it again.")
    plan = RepairPlan.model_validate_json(path.read_text())
    if plan.id != document["id"] or plan.book_id != row["id"]:
        raise ValueError("Repair plan identity mismatch.")
    return plan


def rewrite_container(source: Path, target: Path):
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(target, "w") as new:
        new.comment = old.comment
        mime = copy.copy(old.getinfo("mimetype"))
        if old.read(mime) != b"application/epub+zip":
            raise ValueError("Repair requires an existing, exact EPUB mimetype payload.")
        mime.compress_type = zipfile.ZIP_STORED
        mime.extra = b""
        new.writestr(mime, old.read("mimetype"))
        for info in old.infolist():
            if info.filename != "mimetype":
                new.writestr(copy.copy(info), old.read(info))


def container_preserved(source: Path, candidate: Path) -> bool:
    """Independent comparison includes unmanifested entries and directory records."""
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(candidate) as new:
        before, after = old.infolist(), new.infolist()
        if not after or after[0].filename != "mimetype":
            return False
        if after[0].compress_type != zipfile.ZIP_STORED or after[0].extra:
            return False
        if len(before) != len(after) or old.comment != new.comment:
            return False
        old_names = [i.filename for i in before if i.filename != "mimetype"]
        if new.namelist() != ["mimetype", *old_names]:
            return False
        for info in before:
            other = new.getinfo(info.filename)
            if old.read(info) != new.read(other):
                return False
            if info.filename != "mimetype" and any(
                getattr(info, key) != getattr(other, key)
                for key in ["compress_type", "date_time", "extra", "comment", "external_attr"]
            ):
                return False
        return True


def apply_repairs(book_id: str, config: Config, *, plan_id: str, approve: list[str]) -> Report:
    store = Store(config.root)
    with store.locked():
        row, source, digest = current_source(store, book_id, config)
        previous = get_report(store, row["id"])
        plan = load_repairs(store, row["id"], plan_id)
        if set(approve) != {ACTION} or plan.actions != [ACTION]:
            raise ValueError("Explicitly approve a supported action from emend repair-plan.")
        if plan.engine_version != ENGINE_VERSION or plan.policy_sha256 != config.policy_hash():
            raise ValueError("Repair policy or engine changed; prepare a new repair plan.")
        if any(c.plan_id == plan.id and c.after_sha256 == digest for c in previous.repair_changes):
            if previous.state != "VALIDATED" or row["state"] not in {"VALIDATED", "PUBLISHED"}:
                raise ValueError(
                    "Repair was already applied; use revalidate to refresh failed or pending gates."
                )
            return previous
        if (
            plan.input_sha256 != digest
            or sha256_file(Path(row["report_path"])) != plan.report_sha256
        ):
            raise ValueError("Repair plan is stale; prepare it against the current book/report.")
        book_id = row["id"]
        original = config.root / "originals" / f"{book_id}.epub"
        artifact = config.root / "curated" / f"{book_id}.epub"
        if row["artifact_path"] and source.resolve() != artifact.resolve():
            raise ValueError("Current artifact is not at its expected library path.")
        directory = config.root / "reports" / book_id / uuid.uuid4().hex
        directory.mkdir(parents=True)
        versions = config.root / "versions" / book_id
        retained = versions / f"{digest}.epub"
        if retained.exists() and sha256_file(retained) != digest:
            raise ValueError("Stored version hash mismatch.")
        # Snapshot and inspect before permitting any ZIP reads by the rewriter.
        with tempfile.TemporaryDirectory(dir=config.root / ".staging") as staging:
            current, candidate = Path(staging) / "current.epub", Path(staging) / "candidate.epub"
            snapshot(source, current, config.limits.max_archive_mb * 1024**2)
            baseline = inspect_epub(current, config.limits)
            if baseline.source_sha256 != digest or proposed_actions(baseline) != plan.actions:
                raise ValueError("Current EPUB no longer matches the proposed repair.")
            atomic_copy(current, retained)
            replaced = False
            try:
                rewrite_container(current, candidate)
                inspection = inspect_epub(candidate, config.limits)
                preserved = (
                    not inspection.blocking
                    and container_preserved(current, candidate)
                    and inspection.resource_hashes == baseline.resource_hashes
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
                        detail="Repaired container inspected.",
                    ),
                    Gate(
                        name="preservation",
                        status="passed" if preserved else "failed",
                        required=True,
                        detail="Every ZIP resource payload, reading text, metadata, and spine must match; only approved container packaging may change.",
                    ),
                ]
                if preserved and not inspection.blocking:
                    gates += [
                        validate_epubcheck(candidate, directory, config),
                        validate_ace(candidate, directory, config),
                    ]
                else:
                    gates += [
                        Gate(
                            name=name,
                            status="skipped",
                            required=required,
                            detail="Candidate failed preservation or inspection.",
                        )
                        for name, required in [
                            ("epubcheck", config.quality.require_epubcheck),
                            ("accessibility", config.quality.require_accessibility_audit),
                        ]
                    ]
                if any(
                    not p.is_file() or sha256_file(p) != sha
                    for p, sha in [
                        (source, digest),
                        (original, book_id),
                        (candidate, inspection.source_sha256),
                    ]
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
                report = previous.model_copy(
                    deep=True,
                    update={
                        "created_at": now(),
                        "engine_version": ENGINE_VERSION,
                        "policy_sha256": config.policy_hash(),
                        "state": state,
                        "inspection": inspection,
                        "gates": gates,
                        "revalidation": False,
                        "artifact_sha256": inspection.source_sha256 if not failures else None,
                        "parent_artifact_sha256": digest,
                        "parent_report_path": row["report_path"],
                        "changes": [*previous.changes, DESCRIPTION],
                        "repair_changes": [
                            *previous.repair_changes,
                            RepairChange(
                                plan_id=plan.id,
                                action=ACTION,
                                before_sha256=digest,
                                after_sha256=inspection.source_sha256,
                            ),
                        ],
                    },
                )
                path = write_reports(report, directory)
                if failures:
                    raise ValueError(
                        f"Repair candidate rejected; previous book retained. Evidence: {path}"
                    )
                version = versions / f"{report.artifact_sha256}.epub"
                if version.exists() and sha256_file(version) != report.artifact_sha256:
                    raise ValueError("Stored repaired version hash mismatch.")
                atomic_copy(candidate, version)
                replaced = True
                atomic_copy(candidate, artifact)
                if sha256_file(artifact) != report.artifact_sha256:
                    raise ValueError("Repaired artifact copy hash mismatch.")
                store.transition(
                    book_id,
                    row["source_name"],
                    "VALIDATED",
                    f"Applied repair plan {plan.id}: {ACTION}",
                    path,
                    artifact,
                )
                return report
            except Exception as exc:
                if replaced:
                    if row["artifact_path"]:
                        atomic_copy(retained, artifact)
                    else:
                        artifact.unlink(missing_ok=True)
                (directory / "failure.txt").write_text(f"{type(exc).__name__}: {exc}\n")
                store.event(book_id, "REPAIR_REJECTED", f"{exc}; evidence: {directory}")
                raise
