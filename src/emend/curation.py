"""Apply recorded metadata proposals without rewriting publication resources."""

import hashlib
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from lxml import etree

from emend import ENGINE_VERSION
from emend.archive import copy_entry
from emend.config import Config
from emend.inspect import NS, inspect_epub, parse_xml, sha256_file
from emend.models import Gate, MetadataChange, Report
from emend.pipeline import atomic_copy, get_report
from emend.reports import write_reports
from emend.research.models import Proposal
from emend.research.service import load_plan
from emend.storage import Store, now
from emend.validation import validate_ace, validate_epubcheck


def edit_metadata(source: Path, target: Path, package: str, proposals: list[Proposal]) -> str:
    """Change only selected DC text nodes and EPUB 3's modification timestamp."""
    with zipfile.ZipFile(source) as archive:
        root = parse_xml(archive.read(package))
        metadata = root.find("opf:metadata", NS)
        if metadata is None:
            raise ValueError("Package metadata is missing.")
        for proposal in proposals:
            nodes = metadata.findall(f"dc:{proposal.field}", NS)
            if len(nodes) > 1 or any(len(node) for node in nodes):
                raise ValueError("Multi-valued or structured metadata requires manual editing.")
            before = ["".join(node.itertext()).strip() for node in nodes]
            if before != proposal.before:
                raise ValueError("Proposal no longer matches the package metadata.")
            value = proposal.after[0]
            if not value.strip() or len(value) > 2000 or any(ord(c) < 32 for c in value):
                raise ValueError(
                    "Proposed metadata contains empty, oversized, or control-character text."
                )
            node = (
                nodes[0] if nodes else etree.SubElement(metadata, f"{{{NS['dc']}}}{proposal.field}")
            )
            node.text = value
        if root.get("version", "").startswith("3"):
            modified = metadata.xpath("opf:meta[@property='dcterms:modified']", namespaces=NS)
            if len(modified) != 1 or len(modified[0]):
                raise ValueError("Expected one plain EPUB 3 modification timestamp.")
            modified[0].text = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        package_bytes = etree.tostring(root, encoding="utf-8", xml_declaration=True)
        with zipfile.ZipFile(target, "w") as output:
            output.comment = archive.comment
            for info in archive.infolist():
                copy_entry(
                    output, info, package_bytes if info.filename == package else archive.read(info)
                )
    return hashlib.sha256(package_bytes).hexdigest()


def package_preserved(
    source: Path, candidate: Path, package: str, proposals: list[Proposal]
) -> bool:
    """Independently compare OPF trees after masking only the authorized text changes."""
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(candidate) as new:
        before, after = parse_xml(old.read(package)), parse_xml(new.read(package))
    old_meta, new_meta = before.find("opf:metadata", NS), after.find("opf:metadata", NS)
    if old_meta is None or new_meta is None:
        return False
    for proposal in proposals:
        old_nodes = old_meta.findall(f"dc:{proposal.field}", NS)
        new_nodes = new_meta.findall(f"dc:{proposal.field}", NS)
        if len(new_nodes) != 1 or new_nodes[0].text != proposal.after[0]:
            return False
        if old_nodes:
            if len(old_nodes) != 1:
                return False
            new_nodes[0].text = old_nodes[0].text
        else:
            node = new_nodes[0]
            if node.attrib or len(node) or node.tail:
                return False
            new_meta.remove(node)
    if before.get("version", "").startswith("3"):
        old_modified = old_meta.xpath("opf:meta[@property='dcterms:modified']", namespaces=NS)
        new_modified = new_meta.xpath("opf:meta[@property='dcterms:modified']", namespaces=NS)
        if len(old_modified) != 1 or len(new_modified) != 1:
            return False
        new_modified[0].text = old_modified[0].text

    def signature(node):
        return (
            node.tag,
            dict(node.attrib),
            node.nsmap,
            node.text,
            node.tail,
            [signature(child) for child in node],
        )

    return signature(before) == signature(after)


def apply_proposals(
    book_id: str,
    config: Config,
    *,
    plan_id: str | None = None,
    safe: bool = False,
    approve: list[str] | None = None,
) -> Report:
    store = Store(config.root)
    with store.locked():
        row = store.resolve(book_id)
        book_id = row["id"]
        previous = get_report(store, book_id)
        plan = load_plan(store, book_id, plan_id)
        requested = set(approve or [])
        if requested - {p.id for p in plan.proposals}:
            raise ValueError("Unknown proposal ID; inspect emend proposals first.")
        selected = [
            p for p in plan.proposals if p.id in requested or (safe and p.decision == "safe")
        ]
        if not selected:
            raise ValueError("No proposals selected. Use --safe or --approve PROPOSAL_ID.")
        if len({p.field for p in selected}) != len(selected):
            raise ValueError("Select only one proposal per metadata field.")
        if (
            row["state"] not in {"VALIDATED", "PUBLISHED"}
            or previous.state != "VALIDATED"
            or any(g.required and g.status != "passed" for g in previous.gates)
        ):
            raise ValueError("Apply requires a validated book.")
        if (
            previous.engine_version != ENGINE_VERSION
            or previous.policy_sha256 != config.policy_hash()
        ):
            raise ValueError(
                "Quality policy or engine changed; run emend revalidate before applying metadata."
            )
        if (
            plan.engine_version != ENGINE_VERSION
            or plan.policy_sha256 != config.metadata.fingerprint()
        ):
            raise ValueError("Research policy or engine changed; run research again.")
        artifact = config.root / "curated" / f"{book_id}.epub"
        original = config.root / "originals" / f"{book_id}.epub"
        if (
            previous.book_id != book_id
            or sha256_file(original) != book_id
            or sha256_file(artifact) != previous.artifact_sha256
        ):
            raise ValueError("Source or artifact hash mismatch.")
        ids = sorted(p.id for p in selected)
        if previous.research_plan_id == plan.id and previous.applied_proposals == ids:
            return previous
        if plan.artifact_sha256 != previous.artifact_sha256:
            raise ValueError("Research plan is stale; research the current artifact again.")
        baseline = inspect_epub(artifact, config.limits)
        if (
            baseline.blocking
            or baseline.source_sha256 != plan.artifact_sha256
            or not baseline.package_path
        ):
            raise ValueError("Current artifact failed inspection.")
        directory = config.root / "reports" / book_id / uuid.uuid4().hex
        directory.mkdir(parents=True)
        versions = config.root / "versions" / book_id
        snapshot = versions / f"{previous.artifact_sha256}.epub"
        if snapshot.exists() and sha256_file(snapshot) != previous.artifact_sha256:
            raise ValueError("Stored version hash mismatch.")
        atomic_copy(artifact, snapshot)
        with tempfile.TemporaryDirectory(dir=config.root / ".staging") as staging:
            candidate = Path(staging) / "candidate.epub"
            replaced = False
            try:
                opf_hash = edit_metadata(snapshot, candidate, baseline.package_path, selected)
                inspection = inspect_epub(candidate, config.limits)
                expected_resources = dict(baseline.resource_hashes)
                expected_resources[baseline.package_path] = opf_hash
                preserved = (
                    inspection.resource_hashes == expected_resources
                    and package_preserved(snapshot, candidate, baseline.package_path, selected)
                    and inspection.spine == baseline.spine
                    and inspection.text_sha256 == baseline.text_sha256
                    and inspection.spine_text_sha256 == baseline.spine_text_sha256
                    and all(inspection.metadata.get(p.field) == p.after for p in selected)
                )
                gates = [
                    Gate(
                        name="structure",
                        status="failed" if inspection.blocking else "passed",
                        required=True,
                        detail="Candidate EPUB inspected.",
                    ),
                    Gate(
                        name="preservation",
                        status="passed" if preserved else "failed",
                        required=True,
                        detail="Only selected package metadata and the EPUB 3 modification timestamp may change; all other resource bytes and reading text must match.",
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
                gates.append(
                    Gate(
                        name="visual_qa",
                        status="skipped",
                        required=False,
                        detail="No visual or device-readiness certification.",
                    )
                )
                if (
                    sha256_file(candidate) != inspection.source_sha256
                    or sha256_file(original) != book_id
                    or sha256_file(artifact) != plan.artifact_sha256
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
                report = Report(
                    book_id=book_id,
                    source_name=row["source_name"],
                    created_at=now(),
                    engine_version=ENGINE_VERSION,
                    policy_sha256=config.policy_hash(),
                    state=state,
                    inspection=inspection,
                    gates=gates,
                    artifact_sha256=inspection.source_sha256 if not failures else None,
                    changes=previous.changes
                    + [
                        f"{p.field}: {'; '.join(p.before) or '(missing)'} → {'; '.join(p.after)}. Evidence: {', '.join(p.evidence_urls)}"
                        for p in selected
                    ],
                    cover_changes=previous.cover_changes,
                    repair_changes=previous.repair_changes,
                    typography_changes=previous.typography_changes,
                    text_size_changes=previous.text_size_changes,
                    image_conversions=previous.image_conversions,
                    cover_additions=previous.cover_additions,
                    recovery_changes=previous.recovery_changes,
                    metadata_changes=previous.metadata_changes
                    + [
                        MetadataChange(
                            field=p.field,
                            before=p.before,
                            after=p.after,
                            evidence_urls=p.evidence_urls,
                        )
                        for p in selected
                    ],
                    research_plan_id=plan.id,
                    applied_proposals=ids,
                    parent_artifact_sha256=previous.artifact_sha256,
                    parent_report_path=row["report_path"],
                    research_plan_sha256=store.plan(book_id, plan.id)["sha256"],
                )
                report_path = write_reports(report, directory)
                if failures:
                    raise ValueError(
                        f"Metadata candidate rejected; previous artifact retained. Evidence: {report_path}"
                    )
                atomic_copy(candidate, versions / f"{report.artifact_sha256}.epub")
                atomic_copy(candidate, artifact)
                replaced = True
                if sha256_file(artifact) != report.artifact_sha256:
                    raise ValueError("Candidate copy hash mismatch.")
                store.transition(
                    book_id,
                    row["source_name"],
                    "VALIDATED",
                    f"Applied plan {plan.id}: {', '.join(ids)}",
                    report_path,
                    artifact,
                )
                return report
            except Exception as exc:
                if replaced:
                    atomic_copy(snapshot, artifact)
                (directory / "failure.txt").write_text(f"{type(exc).__name__}: {exc}\n")
                store.event(book_id, "CURATION_REJECTED", f"{exc}; evidence: {directory}")
                raise
