"""Reviewed correction of unambiguous case-only navigation resource paths."""

import posixpath
import zipfile
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

from lxml import etree
from pydantic import BaseModel, ConfigDict, Field

from emend.archive import copy_entry
from emend.inspect import NS, local_target, parse_xml

ACTION = "navigation.case_paths"
DESCRIPTION = (
    "Correct case-only filename errors in the declared table of contents. Keep link labels, "
    "fragments, reading text, and spine order unchanged; update the EPUB 3 modification timestamp."
)
MAX_LINKS = 200


class LinkRepair(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document: str
    element: int = Field(ge=0)
    attribute: str
    before: str
    after: str
    target: str


def navigation_document(archive, inspection):
    package = parse_xml(archive.read(inspection.package_path))
    if not same_xml(package, package):
        return None
    manifest = package.find("opf:manifest", NS)
    if manifest is None:
        return None
    if inspection.epub_version == "3.0":
        items = [i for i in manifest if "nav" in i.get("properties", "").split()]
        modified = package.xpath(
            "opf:metadata/opf:meta[@property='dcterms:modified']", namespaces=NS
        )
        if len(items) != 1 or len(modified) != 1 or len(modified[0]):
            return None
        item = items[0]
        selector, attribute = (
            "//h:nav[contains(concat(' ', @epub:type, ' '), ' toc ')]//h:a[@href]",
            "href",
        )
    elif inspection.epub_version == "2.0":
        spine = package.find("opf:spine", NS)
        items = [i for i in manifest if spine is not None and i.get("id") == spine.get("toc")]
        if len(items) != 1:
            return None
        item = items[0]
        selector, attribute = "//ncx:navMap//ncx:navPoint/ncx:content[@src]", "src"
    else:
        return None
    resolved = local_target(inspection.package_path, item.get("href", ""))
    return (resolved[0], selector, attribute) if resolved else None


def discover_links(source, inspection) -> list[LinkRepair]:
    errors = [f for f in inspection.findings if f.severity == "error"]
    if not errors or any(f.code != "navigation.missing_target" for f in errors):
        return []
    with zipfile.ZipFile(source) as archive:
        navigation = navigation_document(archive, inspection)
        if navigation is None:
            return []
        document, selector, attribute = navigation
        # All broken references must belong to the one declared navigation document.
        if any(f.location != document for f in errors):
            return []
        tree = parse_xml(archive.read(document))
        if not same_xml(tree, tree):
            return []
        indices = {node: i for i, node in enumerate(tree.iter())}
        files = set(inspection.resource_hashes)
        targets = set(inspection.spine)
        folded = {}
        for name in files:
            folded.setdefault(name.casefold(), []).append(name)
        repairs = []
        target_ids = {}
        for node in tree.xpath(selector, namespaces=NS):
            before = node.get(attribute)
            url = urlsplit(before)
            # Preserve queries/remotes/absolute paths for manual review rather than reinterpret them.
            if url.scheme or url.netloc or url.query or url.path.startswith("/"):
                continue
            resolved = local_target(document, before)
            if resolved is None or resolved[0] in files:
                continue
            candidates = folded.get(resolved[0].casefold(), [])
            if len(candidates) != 1 or candidates[0] not in targets:
                return []
            target = candidates[0]
            if target == document:
                return []
            if target not in target_ids:
                target_tree = parse_xml(archive.read(target))
                if target_tree.tag != f"{{{NS['h']}}}html":
                    return []
                target_ids[target] = set(target_tree.xpath("//@id | //@xml:id", namespaces=NS))
            ids = target_ids[target]
            if resolved[1] and resolved[1] not in ids:
                return []
            after = quote(posixpath.relpath(target, posixpath.dirname(document)), safe="/-._~")
            if url.fragment:
                after += "#" + url.fragment
            if any(
                len(value.encode("utf-8")) > 2000 for value in (document, before, after, target)
            ):
                return []
            repairs.append(
                LinkRepair(
                    document=document,
                    element=indices[node],
                    attribute=attribute,
                    before=before,
                    after=after,
                    target=target,
                )
            )
            if len(repairs) > MAX_LINKS:
                return []
        # An unsupported broken resource/attribute in the same file must not be hidden.
        return repairs if len(repairs) == len(errors) else []


def serialize(root):
    return etree.tostring(root.getroottree(), encoding="utf-8", xml_declaration=True)


def rewrite_navigation(source, target, inspection, links):
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(target, "w") as new:
        edits = {}
        elements = {}
        for link in links:
            if link.document not in edits:
                edits[link.document] = parse_xml(old.read(link.document))
                elements[link.document] = list(edits[link.document].iter())
            node = elements[link.document][link.element]
            if node.get(link.attribute) != link.before:
                raise ValueError("Navigation proposal no longer matches the source.")
            node.set(link.attribute, link.after)
        if inspection.epub_version == "3.0":
            package = parse_xml(old.read(inspection.package_path))
            modified = package.xpath(
                "opf:metadata/opf:meta[@property='dcterms:modified']", namespaces=NS
            )
            if len(modified) != 1 or len(modified[0]):
                raise ValueError("Expected one plain EPUB 3 modification timestamp.")
            modified[0].text = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            edits[inspection.package_path] = package
        new.comment = old.comment
        for info in old.infolist():
            copy_entry(
                new,
                info,
                serialize(edits[info.filename]) if info.filename in edits else old.read(info),
            )


def same_xml(before, after):
    a, b = before.getroottree(), after.getroottree()
    try:
        return a.docinfo.doctype == b.docinfo.doctype and etree.tostring(
            a, method="c14n", with_comments=True
        ) == etree.tostring(b, method="c14n", with_comments=True)
    except etree.C14NError:
        return False


def navigation_preserved(source, candidate, inspection, links):
    """Mask only authorized attributes/timestamp, then independently compare entire XML trees."""
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(candidate) as new:
        if old.namelist() != new.namelist() or old.comment != new.comment:
            return False
        changed = {link.document for link in links}
        if inspection.epub_version == "3.0":
            changed.add(inspection.package_path)
        for info in old.infolist():
            other = new.getinfo(info.filename)
            if any(
                getattr(info, key) != getattr(other, key)
                for key in ["compress_type", "date_time", "extra", "comment", "external_attr"]
            ):
                return False
            before, after = old.read(info), new.read(other)
            if info.filename not in changed:
                if before != after:
                    return False
                continue
            a, b = parse_xml(before), parse_xml(after)
            old_nodes, new_nodes = list(a.iter()), list(b.iter())
            if len(old_nodes) != len(new_nodes):
                return False
            for link in links:
                if link.document != info.filename:
                    continue
                old_node, new_node = old_nodes[link.element], new_nodes[link.element]
                if (
                    old_node.get(link.attribute) != link.before
                    or new_node.get(link.attribute) != link.after
                ):
                    return False
                new_node.set(link.attribute, link.before)
            if info.filename == inspection.package_path:
                old_modified = a.xpath(
                    "opf:metadata/opf:meta[@property='dcterms:modified']", namespaces=NS
                )
                new_modified = b.xpath(
                    "opf:metadata/opf:meta[@property='dcterms:modified']", namespaces=NS
                )
                if len(old_modified) != 1 or len(new_modified) != 1:
                    return False
                try:
                    datetime.strptime(new_modified[0].text or "", "%Y-%m-%dT%H:%M:%SZ")
                except ValueError:
                    return False
                # Other timestamp attributes/children still participate in full-tree comparison.
                new_modified[0].text = old_modified[0].text
            if not same_xml(a, b):
                return False
        return True
