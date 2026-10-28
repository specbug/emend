"""Explicit, fingerprinted EPUB repairs with independently checked preservation."""

import copy
import html
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from emend import ENGINE_VERSION
from emend.config import Config
from emend.inspect import inspect_epub, local_extra_bytes, sha256_file
from emend.models import Gate, Inspection, RepairChange, Report
from emend.navigation_repair import ACTION as NAV_ACTION
from emend.navigation_repair import DESCRIPTION as NAV_DESCRIPTION
from emend.navigation_repair import (
    LinkRepair,
    discover_links,
    navigation_preserved,
    rewrite_navigation,
)
from emend.pipeline import atomic_copy, get_report
from emend.reports import report_styles, write_reports
from emend.research.discovery import current_source
from emend.revalidation import snapshot
from emend.storage import Store, now
from emend.validation import validate_ace, validate_epubcheck
from emend.xhtml_repair import ACTION as XHTML_ACTION
from emend.xhtml_repair import DESCRIPTION as XHTML_DESCRIPTION
from emend.xhtml_repair import MarkupRepair, discover_markup, markup_preserved, rewrite_markup

ACTION = "container.mimetype"
DESCRIPTION = (
    "Move the existing mimetype entry first, store it uncompressed, and remove its ZIP extra "
    "fields. Keep its content and every other resource byte unchanged."
)


class RepairRejectedError(ValueError):
    """A candidate failed required quality gates; the current book is unchanged."""


class ContainerState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    position: int = Field(ge=1)
    compression: str
    extra_bytes: int = Field(ge=0)
    local_extra_bytes: int = Field(default=0, ge=0)
    header_offset: int = Field(default=0, ge=0)


class RepairPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    book_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    input_sha256: str
    report_sha256: str
    engine_version: str
    policy_sha256: str
    created_at: str
    actions: list[Literal["container.mimetype", "navigation.case_paths", "xhtml.markup"]]
    links: list[LinkRepair] = Field(default_factory=list)
    markup: list[MarkupRepair] = Field(default_factory=list)
    before: ContainerState | None = None
    after: ContainerState | None = None
    detail: str
    findings: list[str]


MIMETYPE_ERRORS = {"zip.mimetype_order", "zip.mimetype_header"}
# A misplaced mimetype entry is rewritten only when every other structural error
# is one a later step in the same plan can repair. Never try to rehabilitate
# unreadable, unsafe, or otherwise damaged containers.
CHAINABLE_ERRORS = MIMETYPE_ERRORS | {"navigation.missing_target"}
ORDER = (ACTION, NAV_ACTION, XHTML_ACTION)
DESCRIPTIONS = {ACTION: DESCRIPTION, NAV_ACTION: NAV_DESCRIPTION, XHTML_ACTION: XHTML_DESCRIPTION}


def proposed_actions(inspection) -> list[str]:
    errors = {f.code for f in inspection.findings if f.severity == "error"}
    return [ACTION] if errors & MIMETYPE_ERRORS and errors <= CHAINABLE_ERRORS else []


@dataclass
class Step:
    """One simulated repair: its input, output and the inspection it was planned from."""

    action: str
    source: Path
    target: Path
    inspection: Inspection
    edits: list


def discover(action, source, inspection) -> list:
    if action == ACTION:
        return proposed_actions(inspection)
    if action == NAV_ACTION:
        return discover_links(source, inspection)
    return discover_markup(source, inspection)


def rewrite(action, source, target, inspection, edits):
    if action == ACTION:
        rewrite_container(source, target)
    elif action == NAV_ACTION:
        rewrite_navigation(source, target, inspection, edits)
    else:
        rewrite_markup(source, target, inspection, edits)


def simulate(current: Path, staging: Path, limits):
    """Apply each supported repair in order to staging copies; the book is not changed.

    Each step is discovered on the previous step's output, so a packaging repair can
    expose contents-link or markup problems that inspection could not reach before.
    A chain whose final copy is still structurally unreadable proposes nothing.
    """
    baseline = inspect_epub(current, limits)
    source, inspection, steps = current, baseline, []
    for action in ORDER:
        edits = discover(action, source, inspection)
        if not edits:
            continue
        target = staging / f"step-{len(steps) + 1}.epub"
        try:
            rewrite(action, source, target, inspection, edits)
        except ValueError:
            break
        steps.append(Step(action, source, target, inspection, [] if action == ACTION else edits))
        source, inspection = target, inspect_epub(target, limits)
    if inspection.blocking:
        return baseline, []
    return baseline, steps


def planned_edits(plan, action) -> list:
    return [ACTION] if action == ACTION else plan.links if action == NAV_ACTION else plan.markup


def replay(plan, current: Path, baseline, staging: Path, limits):
    """Repeat exactly the planned steps; any divergence from the plan is a rejection."""
    source, inspection, steps = current, baseline, []
    for action in plan.actions:
        edits = discover(action, source, inspection)
        if edits != planned_edits(plan, action):
            raise ValueError("Current EPUB no longer matches the proposed repair.")
        target = staging / f"step-{len(steps) + 1}.epub"
        rewrite(action, source, target, inspection, edits)
        steps.append(Step(action, source, target, inspection, [] if action == ACTION else edits))
        source, inspection = target, inspect_epub(target, limits)
    return steps, source, inspection


def step_preserved(step, limits) -> bool:
    """Each step is checked by the comparison written for that repair alone."""
    if step.action == ACTION:
        after = inspect_epub(step.target, limits)
        return (
            container_preserved(step.source, step.target)
            and after.resource_hashes == step.inspection.resource_hashes
        )
    if step.action == NAV_ACTION:
        return navigation_preserved(step.source, step.target, step.inspection, step.edits)
    return markup_preserved(step.source, step.target, step.inspection, step.edits)


def plan_detail(actions) -> str:
    if len(actions) == 1:
        return DESCRIPTIONS[actions[0]]
    return " ".join(
        f"Step {number}: {DESCRIPTIONS[action]}" for number, action in enumerate(actions, 1)
    )


def prepare_repairs(book_id: str, config: Config) -> tuple[RepairPlan, Path]:
    store = Store(config.root)
    with store.locked():
        row, source, digest = current_source(store, book_id, config)
        with tempfile.TemporaryDirectory(dir=config.root / ".staging") as staging:
            current = Path(staging) / "current.epub"
            snapshot(source, current, config.limits.max_archive_mb * 1024**2)
            inspection, steps = simulate(current, Path(staging), config.limits)
            if inspection.source_sha256 != digest or sha256_file(source) != digest:
                raise ValueError("Current EPUB changed while preparing repairs.")
            actions = [step.action for step in steps]
            links = next((s.edits for s in steps if s.action == NAV_ACTION), [])
            markup = next((s.edits for s in steps if s.action == XHTML_ACTION), [])
            before = None
            if ACTION in actions:
                with zipfile.ZipFile(current) as archive:
                    mime = archive.getinfo("mimetype")
                    before = ContainerState(
                        position=archive.namelist().index("mimetype") + 1,
                        compression={0: "stored", 8: "deflated"}.get(
                            mime.compress_type, f"ZIP method {mime.compress_type}"
                        ),
                        extra_bytes=len(mime.extra),
                        local_extra_bytes=local_extra_bytes(current, mime),
                        header_offset=mime.header_offset,
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
                links=links,
                markup=markup,
                before=before,
                after=ContainerState(position=1, compression="stored", extra_bytes=0)
                if ACTION in actions
                else None,
                detail=plan_detail(actions) if actions else DESCRIPTION,
                findings=[f"{f.code}: {f.message}"[:2000] for f in inspection.findings[:200]],
            )
        directory = config.root / "repairs" / row["id"] / plan.id
        directory.mkdir(parents=True)
        path = directory / "plan.json"
        path.write_text(plan.model_dump_json(indent=2))
        esc = html.escape
        action = (
            "".join(
                f"<p><code>{esc(name)}</code>: {esc(DESCRIPTIONS[name])}</p>"
                for name in plan.actions
            )
            + (
                "<p>The steps run in this order, each on the previous step’s output, and are "
                "checked one by one. Only the final copy is validated and used.</p>"
                if len(plan.actions) > 1
                else ""
            )
            if plan.actions
            else "<p>No supported repair. Other structural errors require manual review.</p>"
        )
        if plan.before:
            action += (
                "<h2>Proposed changes</h2><ul>"
                f"<li>mimetype entry position: {plan.before.position} → 1.</li>"
                f"<li>Compression: {esc(plan.before.compression)} → stored.</li>"
                f"<li>Central ZIP extra field bytes: {plan.before.extra_bytes} → 0.</li>"
                f"<li>Local ZIP extra field bytes: {plan.before.local_extra_bytes} → 0.</li>"
                f"<li>First local-header offset: {plan.before.header_offset} → 0.</li>"
                "</ul>"
            )
        if plan.links:
            action += (
                "<h2>Proposed link changes</h2><ul>"
                + "".join(
                    f"<li><strong>{esc(link.document)}</strong><p>Current: <code>{esc(link.before)}</code></p>"
                    f"<p>Proposed: <code>{esc(link.after)}</code></p><p>Existing target: <code>{esc(link.target)}</code></p></li>"
                    for link in plan.links
                )
                + "</ul>"
            )
        if plan.markup:
            action += (
                "<h2>Proposed HTML changes</h2><ul>"
                + "".join(
                    f"<li><strong>{esc(edit.document)}</strong>, element {edit.element}: "
                    + (
                        f"set empty <code>{esc(edit.attribute)}</code> to <code>{esc(edit.after)}</code>"
                        if edit.kind == "language"
                        else "replace block-only <code>p</code> wrapper with <code>div</code>"
                    )
                    + ".</li>"
                    for edit in plan.markup
                )
                + "</ul>"
            )
        findings = "".join(f"<li>{esc(f)}</li>" for f in plan.findings)
        (directory / "review.html").write_text(
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>EPUB repair review</title><style>{report_styles()}</style></head>"
            '<body><main><p class="brand">Emend · explicit repair review</p>'
            f"<h1>EPUB repair</h1><p><code>{esc(row['source_name'])}</code></p>"
            f"<p>Input SHA-256: <code>{digest}</code></p>{action}"
            "<p>This proposal has not changed the EPUB or its approval state. Applying requires "
            "explicit approval and fresh validation. Reading text stays unchanged. Stylesheets "
            "are not rewritten; preview affected pages after markup repairs. "
            "This does not certify publication quality or device compatibility.</p>"
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
        if not plan.actions or set(approve) != set(plan.actions):
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
            current = Path(staging) / "current.epub"
            snapshot(source, current, config.limits.max_archive_mb * 1024**2)
            baseline = inspect_epub(current, config.limits)
            first = plan.actions[0]
            if (
                baseline.source_sha256 != digest
                or any(discover(a, current, baseline) for a in ORDER[: ORDER.index(first)])
                or discover(first, current, baseline) != planned_edits(plan, first)
            ):
                raise ValueError("Current EPUB no longer matches the proposed repair.")
            atomic_copy(current, retained)
            replaced = False
            try:
                steps, candidate, inspection = replay(
                    plan, current, baseline, Path(staging), config.limits
                )
                # Every step passes its own comparison; the whole chain keeps the
                # book's metadata, spine and reading text exactly.
                preserved = (
                    not inspection.blocking
                    and all(step_preserved(step, config.limits) for step in steps)
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
                        detail="Repaired EPUB inspected.",
                    ),
                    Gate(
                        name="preservation",
                        status="passed" if preserved else "failed",
                        required=True,
                        detail="Unrelated resource bytes, reading text, metadata and spine must match; only approved packaging, navigation or HTML markup edits and the EPUB 3 modification timestamp may change.",
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
                        "changes": [
                            *previous.changes,
                            plan.detail,
                            *[
                                f"{link.document} {link.attribute}: {link.before} → {link.after}. Plan: {plan.id}"
                                for link in plan.links
                            ],
                            *[
                                f"{edit.document} element {edit.element}: "
                                f"{edit.attribute or edit.kind} {edit.before!r} → {edit.after!r}. Plan: {plan.id}"
                                for edit in plan.markup
                            ],
                        ],
                        "repair_changes": [
                            *previous.repair_changes,
                            *[
                                RepairChange(
                                    plan_id=plan.id,
                                    action=step.action,
                                    before_sha256=sha256_file(step.source),
                                    after_sha256=sha256_file(step.target),
                                )
                                for step in steps
                            ],
                        ],
                    },
                )
                path = write_reports(report, directory)
                if failures:
                    raise RepairRejectedError(
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
                    f"Applied repair plan {plan.id}: {', '.join(plan.actions)}",
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
