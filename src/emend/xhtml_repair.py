"""Reviewed repairs for empty root languages and block-only paragraph wrappers."""

import copy
import re
import zipfile
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from emend.inspect import NS, local_target, parse_xml
from emend.navigation_repair import same_xml, serialize

ACTION = "xhtml.markup"
DESCRIPTION = (
    "In EPUB 2, fill empty root language attributes from the one declared package language. Replace "
    "paragraph wrappers containing only headings/lists with divs. Preserve all text, IDs, links, "
    "attributes and unrelated resource bytes; update the EPUB 3 modification timestamp. "
    "Stylesheets stay unchanged, but element selectors may render a div differently: preview "
    "the affected pages before sending."
)
MAX_DOCUMENT_BYTES = 8 * 1024**2
MAX_DOCUMENTS = 1000
MAX_ELEMENTS = 50000
MAX_EDITS = 500
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
HTML = f"{{{NS['h']}}}"
BLOCKS = {HTML + tag for tag in ("h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol")}
# Conservative subset of BCP 47: ordinary language/script/region/variant tags.
# Unknown or more complex declarations remain for manual review; no language detection.
LANGUAGE = re.compile(
    r"[A-Za-z]{2,3}(?:-[A-Za-z]{4})?(?:-(?:[A-Za-z]{2}|[0-9]{3}))?"
    r"(?:-(?:[A-Za-z0-9]{5,8}|[0-9][A-Za-z0-9]{3}))*\Z"
)


class MarkupRepair(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document: str = Field(max_length=1000)
    element: int = Field(ge=0, lt=50000)
    kind: Literal["language", "block-wrapper"]
    attribute: Literal["lang", "xml:lang"] | None = None
    before: str = Field(max_length=100)
    after: str = Field(max_length=100)


def discover_markup(source, inspection) -> list[MarkupRepair]:
    if inspection.blocking or inspection.epub_version not in {"2.0", "3.0"}:
        return []
    with zipfile.ZipFile(source) as archive:
        package_info = archive.getinfo(inspection.package_path)
        if package_info.file_size > MAX_DOCUMENT_BYTES:
            return []
        package = parse_xml(archive.read(package_info))
        if not same_xml(package, package):
            return []
        if inspection.epub_version == "3.0":
            modified = package.xpath(
                "opf:metadata/opf:meta[@property='dcterms:modified']", namespaces=NS
            )
            if len(modified) != 1 or len(modified[0]):
                return []
        languages = package.xpath("opf:metadata/dc:language/text()", namespaces=NS)
        language = (
            languages[0] if len(languages) == 1 and LANGUAGE.fullmatch(languages[0]) else None
        )
        documents = set()
        for item in package.xpath(
            "opf:manifest/opf:item[@media-type='application/xhtml+xml']", namespaces=NS
        ):
            target = local_target(inspection.package_path, item.get("href", ""))
            if target:
                documents.add(target[0])
        if len(documents) > MAX_DOCUMENTS:
            return []
        edits = []
        for document in sorted(documents):
            info = archive.getinfo(document)
            if info.file_size > MAX_DOCUMENT_BYTES or len(document) > 1000:
                return []
            tree = parse_xml(archive.read(info))
            if tree.tag != HTML + "html" or not same_xml(tree, tree):
                return []
            nodes = list(tree.iter())
            if len(nodes) > MAX_ELEMENTS:
                return []
            # Do not override a nonempty or conflicting document language. Missing
            # attributes and local language overrides are intentionally untouched.
            if (
                inspection.epub_version == "2.0"
                and language
                and all(tree.get(key) in {None, ""} for key in ("lang", XML_LANG))
            ):
                for attribute, key in (("lang", "lang"), ("xml:lang", XML_LANG)):
                    if tree.get(key) == "":
                        edits.append(
                            MarkupRepair(
                                document=document,
                                element=0,
                                kind="language",
                                attribute=attribute,
                                before="",
                                after=language,
                            )
                        )
            for index, node in enumerate(nodes):
                if node.tag != HTML + "p" or node.getparent().tag not in {
                    HTML + "body",
                    HTML + "div",
                }:
                    continue
                children = [child for child in node if isinstance(child.tag, str)]
                if (
                    not children
                    or any(child.tag not in BLOCKS for child in children)
                    or (node.text or "").strip()
                    or any((child.tail or "").strip() for child in node)
                ):
                    continue
                edits.append(
                    MarkupRepair(
                        document=document,
                        element=index,
                        kind="block-wrapper",
                        before="p",
                        after="div",
                    )
                )
            if len(edits) > MAX_EDITS:
                return []
        return edits


def rewrite_markup(source, target, inspection, edits):
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(target, "w") as new:
        trees, elements = {}, {}
        for edit in edits:
            if edit.document not in trees:
                trees[edit.document] = parse_xml(old.read(edit.document))
                elements[edit.document] = list(trees[edit.document].iter())
            node = elements[edit.document][edit.element]
            if edit.kind == "language":
                attribute = XML_LANG if edit.attribute == "xml:lang" else "lang"
                if node.get(attribute) != edit.before:
                    raise ValueError("Language proposal no longer matches the source.")
                node.set(attribute, edit.after)
            else:
                if node.tag != HTML + "p":
                    raise ValueError("Paragraph proposal no longer matches the source.")
                node.tag = HTML + "div"
        if inspection.epub_version == "3.0":
            package = parse_xml(old.read(inspection.package_path))
            modified = package.xpath(
                "opf:metadata/opf:meta[@property='dcterms:modified']", namespaces=NS
            )
            if len(modified) != 1 or len(modified[0]):
                raise ValueError("Expected one plain EPUB 3 modification timestamp.")
            modified[0].text = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            trees[inspection.package_path] = package
        new.comment = old.comment
        for info in old.infolist():
            new.writestr(
                copy.copy(info),
                serialize(trees[info.filename]) if info.filename in trees else old.read(info),
            )


def markup_preserved(source, candidate, inspection, edits):
    """Independently mask only approved changes, then compare complete XML trees."""
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(candidate) as new:
        if old.namelist() != new.namelist() or old.comment != new.comment:
            return False
        changed = {edit.document for edit in edits}
        if inspection.epub_version == "3.0":
            changed.add(inspection.package_path)
        for info in old.infolist():
            other = new.getinfo(info.filename)
            if any(
                getattr(info, key) != getattr(other, key)
                for key in ("compress_type", "date_time", "extra", "comment", "external_attr")
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
            for edit in edits:
                if edit.document != info.filename:
                    continue
                left, right = old_nodes[edit.element], new_nodes[edit.element]
                if edit.kind == "language":
                    attribute = XML_LANG if edit.attribute == "xml:lang" else "lang"
                    if left.get(attribute) != edit.before or right.get(attribute) != edit.after:
                        return False
                    right.set(attribute, edit.before)
                else:
                    if left.tag != HTML + "p" or right.tag != HTML + "div":
                        return False
                    right.tag = left.tag
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
                new_modified[0].text = old_modified[0].text
            if not same_xml(a, b):
                return False
        return True
