"""Explicitly add a missing cover while preserving every existing reading resource."""

import copy
import hashlib
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
from emend.covers import declared_cover, image_details, load_cover_review, read_image
from emend.inspect import NS, inspect_epub, local_target, parse_xml, sha256_file
from emend.models import CoverAddition, Gate
from emend.navigation_repair import same_xml, serialize
from emend.pagination import fixed_layout
from emend.pipeline import atomic_copy
from emend.publish import approved_artifact
from emend.reports import write_reports
from emend.revalidation import snapshot
from emend.storage import Store, now
from emend.validation import validate_ace, validate_epubcheck


class CoverCreationRejectedError(ValueError):
    pass


def specification(baseline, review, asset):
    folder = posixpath.dirname(baseline.package_path)
    stem = "emend-cover-" + review.id[:24]
    return {
        "image": posixpath.join(
            folder, stem + (".png" if asset.media_type == "image/png" else ".jpg")
        ),
        "page": posixpath.join(folder, stem + ".xhtml"),
        "image_id": stem + "-image",
        "page_id": stem + "-page",
    }


def cover_document(baseline, spec):
    title = "; ".join(baseline.metadata.get("title", []))[:2000] or "Cover"
    language = (baseline.metadata.get("language") or ["en"])[0]
    root = etree.Element(
        f"{{{NS['h']}}}html",
        nsmap={None: NS["h"], **({"epub": NS["epub"]} if baseline.epub_version == "3.0" else {})},
    )
    root.set("{http://www.w3.org/XML/1998/namespace}lang", language)
    head = etree.SubElement(root, f"{{{NS['h']}}}head")
    etree.SubElement(head, f"{{{NS['h']}}}title").text = title
    etree.SubElement(
        head, f"{{{NS['h']}}}style", type="text/css"
    ).text = "html, body, #cover { height: 100%; margin: 0; padding: 0; } img { display: block; width: auto; height: auto; max-width: 100%; max-height: 100%; margin: 0 auto; }"
    body = etree.SubElement(root, f"{{{NS['h']}}}body")
    if baseline.epub_version == "3.0":
        body.set(f"{{{NS['epub']}}}type", "cover")
    container = etree.SubElement(
        body, f"{{{NS['h']}}}{'main' if baseline.epub_version == '3.0' else 'div'}", id="cover"
    )
    etree.SubElement(
        container,
        f"{{{NS['h']}}}img",
        src=quote(posixpath.relpath(spec["image"], posixpath.dirname(spec["page"])), safe="/-._~"),
        alt=title,
    )
    return serialize(root)


def additions(package, baseline, spec, media_type):
    manifest = package.find("opf:manifest", NS)
    spine = package.find("opf:spine", NS)
    metadata = package.find("opf:metadata", NS)
    image_attrs = {
        "id": spec["image_id"],
        "href": quote(
            posixpath.relpath(spec["image"], posixpath.dirname(baseline.package_path)), safe="/-._~"
        ),
        "media-type": media_type,
    }
    if baseline.epub_version == "3.0":
        image_attrs["properties"] = "cover-image"
    page_attrs = {
        "id": spec["page_id"],
        "href": quote(
            posixpath.relpath(spec["page"], posixpath.dirname(baseline.package_path)), safe="/-._~"
        ),
        "media-type": "application/xhtml+xml",
    }
    return manifest, spine, metadata, image_attrs, page_attrs


def rewrite(source, candidate, baseline, spec, data, media_type):
    with zipfile.ZipFile(source) as old:
        package = parse_xml(old.read(baseline.package_path))
        manifest, spine, metadata, image_attrs, page_attrs = additions(
            package, baseline, spec, media_type
        )
        if (
            any(i.get("id") in {spec["image_id"], spec["page_id"]} for i in manifest)
            or spec["image"] in old.namelist()
            or spec["page"] in old.namelist()
        ):
            raise ValueError("Cover resource/ID collision; prepare a new review.")
        etree.SubElement(manifest, f"{{{NS['opf']}}}item", attrib=image_attrs)
        etree.SubElement(manifest, f"{{{NS['opf']}}}item", attrib=page_attrs)
        spine.insert(0, etree.Element(f"{{{NS['opf']}}}itemref", idref=spec["page_id"]))
        if baseline.epub_version == "2.0":
            etree.SubElement(
                metadata, f"{{{NS['opf']}}}meta", name="cover", content=spec["image_id"]
            )
            guide = package.find("opf:guide", NS)
            if guide is None:
                guide = etree.SubElement(package, f"{{{NS['opf']}}}guide")
            etree.SubElement(
                guide,
                f"{{{NS['opf']}}}reference",
                type="cover",
                title="Cover",
                href=page_attrs["href"],
            )
        else:
            modified = metadata.xpath("opf:meta[@property='dcterms:modified']", namespaces=NS)
            if len(modified) != 1 or len(modified[0]):
                raise ValueError("Expected one plain EPUB 3 modification timestamp.")
            modified[0].text = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with zipfile.ZipFile(candidate, "w") as new:
            new.comment = old.comment
            for info in old.infolist():
                new.writestr(
                    copy.copy(info),
                    serialize(package)
                    if info.filename == baseline.package_path
                    else old.read(info),
                )
            new.writestr(spec["image"], data, compress_type=zipfile.ZIP_DEFLATED)
            new.writestr(
                spec["page"], cover_document(baseline, spec), compress_type=zipfile.ZIP_DEFLATED
            )


def preserved(source, candidate, baseline, spec, data, media_type):
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(candidate) as new:
        if (
            new.namelist() != [*old.namelist(), spec["image"], spec["page"]]
            or old.comment != new.comment
        ):
            return False
        if new.read(spec["image"]) != data or new.read(spec["page"]) != cover_document(
            baseline, spec
        ):
            return False
        for info in old.infolist():
            other = new.getinfo(info.filename)
            if any(
                getattr(info, k) != getattr(other, k)
                for k in ("compress_type", "date_time", "extra", "comment", "external_attr")
            ):
                return False
            if info.filename != baseline.package_path and old.read(info) != new.read(other):
                return False
        a, b = (
            parse_xml(old.read(baseline.package_path)),
            parse_xml(new.read(baseline.package_path)),
        )
        manifest, spine, metadata, image_attrs, page_attrs = additions(
            b, baseline, spec, media_type
        )
        for attrs in (image_attrs, page_attrs):
            items = manifest.xpath("opf:item[@id=$id]", namespaces=NS, id=attrs["id"])
            if (
                len(items) != 1
                or dict(items[0].attrib) != attrs
                or len(items[0])
                or items[0].text
                or items[0].tail
            ):
                return False
            manifest.remove(items[0])
        first = spine[0]
        if (
            first.tag != f"{{{NS['opf']}}}itemref"
            or dict(first.attrib) != {"idref": spec["page_id"]}
            or first.text
            or first.tail
            or len(first)
        ):
            return False
        spine.remove(first)
        if baseline.epub_version == "2.0":
            meta = metadata.xpath("opf:meta[@name='cover']", namespaces=NS)
            if (
                len(meta) != 1
                or dict(meta[0].attrib) != {"name": "cover", "content": spec["image_id"]}
                or meta[0].text
                or meta[0].tail
                or len(meta[0])
            ):
                return False
            metadata.remove(meta[0])
            guide = b.find("opf:guide", NS)
            refs = (
                guide.xpath("opf:reference[@type='cover']", namespaces=NS)
                if guide is not None
                else []
            )
            if (
                len(refs) != 1
                or dict(refs[0].attrib)
                != {"type": "cover", "title": "Cover", "href": page_attrs["href"]}
                or refs[0].text
                or refs[0].tail
                or len(refs[0])
            ):
                return False
            guide.remove(refs[0])
            if a.find("opf:guide", NS) is None:
                if len(guide) or guide.attrib or guide.text or guide.tail:
                    return False
                b.remove(guide)
        else:
            old_mod = a.xpath("opf:metadata/opf:meta[@property='dcterms:modified']", namespaces=NS)
            new_mod = metadata.xpath("opf:meta[@property='dcterms:modified']", namespaces=NS)
            if len(old_mod) != 1 or len(new_mod) != 1:
                return False
            try:
                datetime.strptime(new_mod[0].text or "", "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                return False
            new_mod[0].text = old_mod[0].text
        return same_xml(a, b)


def create_cover(book_id, config: Config, *, review_id, approve, allow_small=False):
    store = Store(config.root)
    with store.locked():
        row, previous, artifact = approved_artifact(store, book_id, config)
        book_id = row["id"]
        digest = previous.artifact_sha256
        review = load_cover_review(store, book_id, review_id)
        selected = [a for a in review.candidates if a.id == approve]
        if len(selected) != 1:
            raise ValueError("Choose one candidate ID from the saved cover review.")
        asset = selected[0]
        if review.engine_version != ENGINE_VERSION or review.policy_sha256 != config.policy_hash():
            raise ValueError("Cover policy/engine changed; prepare a new review.")
        if any(
            c.review_id == review.id and c.candidate_id == asset.id and c.after_sha256 == digest
            for c in previous.cover_additions
        ):
            return previous
        if review.artifact_sha256 != digest:
            raise ValueError("Cover review is stale; prepare another review.")
        if review.original is not None:
            raise ValueError("A declared cover already exists; use cover replacement instead.")
        if review.plan_id:
            from emend.research.service import load_plan

            load_plan(store, book_id, review.plan_id)
            if store.plan(book_id, review.plan_id)["sha256"] != review.plan_sha256:
                raise ValueError("Cover source research changed.")
        data = read_image(Path(asset.path))
        if hashlib.sha256(data).hexdigest() != asset.sha256:
            raise ValueError("Cover image changed while preparing creation.")
        details = image_details(data)
        if (
            details["media_type"] != asset.media_type
            or details["width"] != asset.width
            or details["height"] != asset.height
        ):
            raise ValueError("Cover image dimensions/format changed.")
        if details["orientation"] != 1 or details["mode"] not in {"RGB", "RGBA", "L", "LA", "P"}:
            raise ValueError("Prepare a normally oriented RGB/grayscale cover first.")
        if min(asset.width, asset.height) < 600 and not allow_small:
            raise ValueError(
                "A missing-cover source below 600 px on its short edge needs --allow-small after reviewing readability."
            )
        with tempfile.TemporaryDirectory(dir=config.root / ".staging") as staging:
            source = Path(staging) / "current.epub"
            candidate = Path(staging) / "candidate.epub"
            snapshot(artifact, source, config.limits.max_archive_mb * 1024**2)
            baseline = inspect_epub(source, config.limits)
            if baseline.blocking or baseline.source_sha256 != digest or not baseline.package_path:
                raise ValueError("Current artifact failed inspection.")
            with zipfile.ZipFile(source) as archive:
                package = parse_xml(archive.read(baseline.package_path))
                if declared_cover(source, baseline.package_path):
                    raise ValueError("A cover declaration already exists.")
                if fixed_layout(package):
                    raise ValueError(
                        "Missing-cover creation currently supports reflowable EPUBs only."
                    )
                if package.xpath("opf:guide/opf:reference[@type='cover']", namespaces=NS):
                    raise ValueError(
                        "An existing cover-page hint needs manual declaration repair, not another page."
                    )
                for item in package.xpath(
                    "opf:manifest/opf:item[@media-type='application/xhtml+xml']", namespaces=NS
                ):
                    target = local_target(baseline.package_path, item.get("href", ""))
                    if target:
                        root = parse_xml(archive.read(target[0]))
                        if root.xpath(
                            "//*[contains(concat(' ', @epub:type, ' '), ' cover ')]", namespaces=NS
                        ):
                            raise ValueError(
                                "An existing semantic cover page/landmark needs manual declaration repair."
                            )
            spec = specification(baseline, review, asset)
            directory = config.root / "reports" / book_id / uuid.uuid4().hex
            directory.mkdir(parents=True)
            versions = config.root / "versions" / book_id
            retained = versions / f"{digest}.epub"
            if retained.exists() and sha256_file(retained) != digest:
                raise ValueError("Stored version hash mismatch.")
            atomic_copy(source, retained)
            replaced = False
            try:
                rewrite(source, candidate, baseline, spec, data, asset.media_type)
                inspection = inspect_epub(candidate, config.limits)
                intact = (
                    not inspection.blocking
                    and preserved(source, candidate, baseline, spec, data, asset.media_type)
                    and inspection.spine == [spec["page"], *baseline.spine]
                    and inspection.metadata == baseline.metadata
                )
                gates = [
                    Gate(
                        name="structure",
                        status="failed" if inspection.blocking else "passed",
                        required=True,
                        detail="Missing-cover candidate inspected.",
                    ),
                    Gate(
                        name="preservation",
                        status="passed" if intact else "failed",
                        required=True,
                        detail="Only the approved image/page and their package declarations may be added. Existing resource bytes and relative spine order must match.",
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
                            name=n,
                            status="skipped",
                            required=r,
                            detail="Candidate failed preservation/inspection.",
                        )
                        for n, r in [
                            ("epubcheck", config.quality.require_epubcheck),
                            ("accessibility", config.quality.require_accessibility_audit),
                        ]
                    ]
                if any(
                    not p.is_file() or sha256_file(p) != sha
                    for p, sha in [
                        (artifact, digest),
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
                change = CoverAddition(
                    review_id=review.id,
                    candidate_id=asset.id,
                    image_path=spec["image"],
                    page_path=spec["page"],
                    image_sha256=asset.sha256,
                    source=asset.source,
                    source_url=asset.source_url,
                    allowed_small=allow_small,
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
                        "cover_additions": [*previous.cover_additions, change],
                        "changes": [
                            *previous.changes,
                            f"Added explicitly reviewed missing cover {review.id}/{asset.id}; a new cover page precedes the unchanged original reading sequence. Source: {asset.source}.",
                        ],
                    },
                )
                path = write_reports(report, directory)
                if failures:
                    raise CoverCreationRejectedError(
                        f"Cover candidate rejected; previous artifact retained. Evidence: {path}"
                    )
                version = versions / f"{report.artifact_sha256}.epub"
                if version.exists() and sha256_file(version) != report.artifact_sha256:
                    raise ValueError("Stored candidate revision hash mismatch.")
                atomic_copy(candidate, version)
                replaced = True
                atomic_copy(candidate, artifact)
                if sha256_file(artifact) != report.artifact_sha256:
                    raise ValueError("Activated cover hash mismatch.")
                store.transition(
                    book_id,
                    row["source_name"],
                    "VALIDATED",
                    f"Added reviewed cover {review.id}",
                    path,
                    artifact,
                )
                return report
            except Exception as exc:
                if replaced:
                    atomic_copy(retained, artifact)
                (directory / "failure.txt").write_text(f"{type(exc).__name__}: {exc}\n")
                store.event(book_id, "COVER_CREATION_REJECTED", f"{exc}; evidence: {directory}")
                raise
