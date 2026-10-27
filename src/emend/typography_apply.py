"""Apply the exact CSS from a reviewed current trial with independent preservation."""

import copy
import posixpath
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from lxml import etree

from emend import ENGINE_VERSION
from emend.config import Config
from emend.inspect import NS, inspect_epub, parse_xml, sha256_file
from emend.models import Gate, TypographyChange
from emend.navigation_repair import same_xml, serialize
from emend.pipeline import atomic_copy, get_report
from emend.reports import write_reports
from emend.research.discovery import current_source
from emend.revalidation import snapshot
from emend.storage import Store, now
from emend.typography import load_trial
from emend.validation import validate_ace, validate_epubcheck


class TypographyRejectedError(ValueError):
    pass


def style_resource(package, css_hash):
    return posixpath.join(posixpath.dirname(package), f"emend-typography-{css_hash[:24]}.css")


def link_attributes(document, resource):
    return {
        "rel": "stylesheet",
        "type": "text/css",
        "href": quote(posixpath.relpath(resource, posixpath.dirname(document)), safe="/-._~"),
    }


def manifest_attributes(package, resource, css_hash):
    return {
        "id": f"emend-typography-{css_hash[:24]}",
        "href": quote(posixpath.relpath(resource, posixpath.dirname(package)), safe="/-._~"),
        "media-type": "text/css",
    }


def rewrite(source, candidate, baseline, documents, css, css_hash):
    resource = style_resource(baseline.package_path, css_hash)
    attrs = manifest_attributes(baseline.package_path, resource, css_hash)
    with zipfile.ZipFile(source) as old:
        edits = {}
        if resource in old.namelist() and old.read(resource) != css.encode():
            raise ValueError("Typography stylesheet resource already exists with different bytes.")
        package = parse_xml(old.read(baseline.package_path))
        manifest = package.find("opf:manifest", NS)
        existing = manifest.xpath("opf:item[@id=$id]", namespaces=NS, id=attrs["id"])
        if existing:
            if len(existing) != 1 or dict(existing[0].attrib) != attrs:
                raise ValueError("Typography manifest ID collides with an existing item.")
        else:
            etree.SubElement(manifest, f"{{{NS['opf']}}}item", attrib=attrs)
        if baseline.epub_version == "3.0":
            modified = package.xpath(
                "opf:metadata/opf:meta[@property='dcterms:modified']", namespaces=NS
            )
            if len(modified) != 1 or len(modified[0]):
                raise ValueError("Expected one plain EPUB 3 modification timestamp.")
            modified[0].text = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        edits[baseline.package_path] = serialize(package)
        for document in documents:
            root = parse_xml(old.read(document))
            heads = root.findall("h:head", NS)
            if len(heads) != 1:
                raise ValueError("Expected one XHTML head for each approved document.")
            etree.SubElement(
                heads[0], f"{{{NS['h']}}}link", attrib=link_attributes(document, resource)
            )
            edits[document] = serialize(root)
        with zipfile.ZipFile(candidate, "w") as new:
            new.comment = old.comment
            for info in old.infolist():
                new.writestr(copy.copy(info), edits.get(info.filename, old.read(info)))
            if resource not in old.namelist():
                new.writestr(resource, css.encode(), compress_type=zipfile.ZIP_DEFLATED)
    return resource


def preserved(source, candidate, baseline, documents, css, css_hash):
    resource = style_resource(baseline.package_path, css_hash)
    attrs = manifest_attributes(baseline.package_path, resource, css_hash)
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(candidate) as new:
        names = old.namelist()
        expected = names if resource in names else [*names, resource]
        if (
            new.namelist() != expected
            or old.comment != new.comment
            or new.read(resource) != css.encode()
        ):
            return False
        changed = {baseline.package_path, *documents}
        for info in old.infolist():
            other = new.getinfo(info.filename)
            if any(
                getattr(info, key) != getattr(other, key)
                for key in ("compress_type", "date_time", "extra", "comment", "external_attr")
            ):
                return False
            a_bytes, b_bytes = old.read(info), new.read(other)
            if info.filename not in changed:
                if a_bytes != b_bytes:
                    return False
                continue
            a, b = parse_xml(a_bytes), parse_xml(b_bytes)
            if info.filename == baseline.package_path:
                old_manifest, new_manifest = a.find("opf:manifest", NS), b.find("opf:manifest", NS)
                old_items = old_manifest.xpath("opf:item[@id=$id]", namespaces=NS, id=attrs["id"])
                items = new_manifest.xpath("opf:item[@id=$id]", namespaces=NS, id=attrs["id"])
                if (
                    len(items) != 1
                    or dict(items[0].attrib) != attrs
                    or len(items[0])
                    or items[0].text
                    or items[0].tail
                ):
                    return False
                if not old_items:
                    new_manifest.remove(items[0])
                if baseline.epub_version == "3.0":
                    old_mod = a.xpath(
                        "opf:metadata/opf:meta[@property='dcterms:modified']", namespaces=NS
                    )
                    new_mod = b.xpath(
                        "opf:metadata/opf:meta[@property='dcterms:modified']", namespaces=NS
                    )
                    if len(old_mod) != 1 or len(new_mod) != 1:
                        return False
                    try:
                        datetime.strptime(new_mod[0].text or "", "%Y-%m-%dT%H:%M:%SZ")
                    except ValueError:
                        return False
                    new_mod[0].text = old_mod[0].text
            else:
                heads = b.findall("h:head", NS)
                if len(heads) != 1 or not len(heads[0]):
                    return False
                node = heads[0][-1]
                if (
                    node.tag != f"{{{NS['h']}}}link"
                    or dict(node.attrib) != link_attributes(info.filename, resource)
                    or len(node)
                    or node.text
                    or node.tail
                ):
                    return False
                heads[0].remove(node)
            if not same_xml(a, b):
                return False
    return True


def apply_typography(
    book_id: str, config: Config, *, trial_id: str, approve: bool, all_spine: bool = False
):
    if not approve:
        raise ValueError("Inspect the typography trial and pass --approve to apply it.")
    store = Store(config.root)
    with store.locked():
        row, source, digest = current_source(store, book_id, config)
        previous = get_report(store, row["id"])
        trial = load_trial(store, row["id"], trial_id)
        if (
            row["state"] not in {"VALIDATED", "PUBLISHED"}
            or previous.state != "VALIDATED"
            or any(g.required and g.status != "passed" for g in previous.gates)
        ):
            raise ValueError("Typography application requires a validated current book.")
        if (
            previous.engine_version != ENGINE_VERSION
            or previous.policy_sha256 != config.policy_hash()
        ):
            raise ValueError("Quality policy or engine changed; revalidate the current EPUB first.")
        if trial.status != "complete" or trial.engine_version != ENGINE_VERSION:
            raise ValueError("A complete current-engine typography trial is required.")
        if any(
            c.trial_id == trial.id and c.after_sha256 == digest and c.all_spine == all_spine
            for c in previous.typography_changes
        ):
            return previous
        if trial.artifact_sha256 != digest:
            raise ValueError("Typography trial is stale; capture a new trial for the current EPUB.")
        book_id = row["id"]
        artifact = config.root / "curated" / f"{book_id}.epub"
        if source.resolve() != artifact.resolve():
            raise ValueError("Current artifact is not at its expected library path.")
        css = trial.settings.css()
        with tempfile.TemporaryDirectory(dir=config.root / ".staging") as staging:
            current, candidate = Path(staging) / "current.epub", Path(staging) / "candidate.epub"
            snapshot(source, current, config.limits.max_archive_mb * 1024**2)
            baseline = inspect_epub(current, config.limits)
            if baseline.blocking or baseline.source_sha256 != digest or not baseline.package_path:
                raise ValueError("Current EPUB failed inspection or changed before editing.")
            documents = list(baseline.spine) if all_spine else list(trial.selected_documents)
            documents = list(dict.fromkeys(documents))
            resource = style_resource(baseline.package_path, trial.css_sha256)
            with zipfile.ZipFile(current) as archive:
                # Reject non-XHTML scope before applying any XML edits.
                for name in documents:
                    if (
                        name not in baseline.resource_hashes
                        or parse_xml(archive.read(name)).tag != f"{{{NS['h']}}}html"
                    ):
                        raise ValueError(
                            "Approved typography scope must contain XHTML documents only."
                        )
            directory = config.root / "reports" / book_id / uuid.uuid4().hex
            directory.mkdir(parents=True)
            versions = config.root / "versions" / book_id
            retained = versions / f"{digest}.epub"
            if retained.exists() and sha256_file(retained) != digest:
                raise ValueError("Stored revision hash mismatch.")
            atomic_copy(current, retained)
            replaced = False
            try:
                rewrite(current, candidate, baseline, documents, css, trial.css_sha256)
                inspection = inspect_epub(candidate, config.limits)
                intact = (
                    not inspection.blocking
                    and preserved(current, candidate, baseline, documents, css, trial.css_sha256)
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
                        detail="Typography candidate inspected.",
                    ),
                    Gate(
                        name="preservation",
                        status="passed" if intact else "failed",
                        required=True,
                        detail="Only approved CSS, stylesheet links, manifest declaration and EPUB 3 modification timestamp may change; reading text and unrelated resources must match.",
                    ),
                ]
                if intact:
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
                        (config.root / "originals" / f"{book_id}.epub", book_id),
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
                change = TypographyChange(
                    trial_id=trial.id,
                    css_sha256=trial.css_sha256,
                    resource_path=resource,
                    documents=documents,
                    all_spine=all_spine,
                    before_sha256=digest,
                    after_sha256=inspection.source_sha256,
                )
                report = previous.model_copy(
                    deep=True,
                    update={
                        "created_at": now(),
                        "state": state,
                        "inspection": inspection,
                        "gates": gates,
                        "revalidation": False,
                        "artifact_sha256": inspection.source_sha256 if not failures else None,
                        "parent_artifact_sha256": digest,
                        "parent_report_path": row["report_path"],
                        "typography_changes": [*previous.typography_changes, change],
                        "changes": [
                            *previous.changes,
                            f"Applied typography trial {trial.id} to {len(documents)} documents; all-spine scope: {all_spine}. CSS SHA-256: {trial.css_sha256}.",
                        ],
                    },
                )
                path = write_reports(report, directory)
                if failures:
                    raise TypographyRejectedError(
                        f"Typography candidate rejected; previous artifact retained. Evidence: {path}"
                    )
                version = versions / f"{report.artifact_sha256}.epub"
                if version.exists() and sha256_file(version) != report.artifact_sha256:
                    raise ValueError("Stored candidate revision hash mismatch.")
                atomic_copy(candidate, version)
                replaced = True
                atomic_copy(candidate, artifact)
                if sha256_file(artifact) != report.artifact_sha256:
                    raise ValueError("Activated typography hash mismatch.")
                store.transition(
                    book_id,
                    row["source_name"],
                    "VALIDATED",
                    f"Applied typography trial {trial.id}",
                    path,
                    artifact,
                )
                return report
            except Exception as exc:
                if replaced:
                    atomic_copy(retained, artifact)
                (directory / "failure.txt").write_text(f"{type(exc).__name__}: {exc}\n")
                store.event(book_id, "TYPOGRAPHY_REJECTED", f"{exc}; evidence: {directory}")
                raise
