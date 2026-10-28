"""Read EPUB containers without extracting them or fetching external resources."""

import hashlib
import io
import json
import posixpath
import warnings
import zipfile
import zlib
from collections import Counter
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

import tinycss2
from lxml import etree
from PIL import Image

from emend.config import Limits
from emend.models import Finding, Inspection

NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "h": "http://www.w3.org/1999/xhtml",
    "epub": "http://www.idpf.org/2007/ops",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
}
DOCUMENT_TYPES = {"application/xhtml+xml", "image/svg+xml", "application/x-dtbncx+xml"}


def sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def local_extra_bytes(path: Path, info: zipfile.ZipInfo) -> int:
    with path.open("rb") as stream:
        stream.seek(info.header_offset)
        header = stream.read(30)
    if len(header) != 30 or header[:4] != b"PK\x03\x04":
        return -1
    return int.from_bytes(header[28:30], "little")


def local_target(source: str, href: str) -> tuple[str, str] | None:
    """Resolve one URL exactly once; reject filesystem escape and active schemes."""
    url = urlsplit(href)
    if url.scheme or url.netloc:
        if url.scheme.lower() in {"javascript", "file", "vbscript"}:
            raise ValueError("Unsafe URI scheme")
        return None
    path = unquote(url.path)
    if "\\" in path or "\x00" in path or path.startswith("/"):
        raise ValueError("Invalid EPUB-relative path")
    target = posixpath.normpath(posixpath.join(posixpath.dirname(source), path)) if path else source
    if target == ".." or target.startswith("../"):
        raise ValueError("Reference escapes the EPUB container")
    return target, unquote(url.fragment)


def parse_xml(data: bytes) -> etree._Element:
    parser = etree.XMLParser(
        resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False, recover=False
    )
    tree = etree.fromstring(data, parser)
    # A public XHTML doctype is common in EPUB 2. Entity declarations are never needed.
    if any(isinstance(node, etree._Entity) for node in tree.iter()):
        raise ValueError("Custom XML entities are unsupported")
    dtd = tree.getroottree().docinfo.internalDTD
    if dtd is not None and list(dtd.iterentities()):
        raise ValueError("XML entity declarations are unsupported")
    return tree


def body_text(tree: etree._Element) -> str:
    bodies = tree.xpath("//h:body", namespaces=NS)
    root = bodies[0] if bodies else tree
    # Preserve punctuation, whitespace, captions and notes; omit only scripts/styles.
    pieces = []

    def walk(node):
        if not isinstance(node.tag, str):
            return
        if etree.QName(node).localname in {"script", "style"}:
            return
        if node.text:
            pieces.append(node.text)
        for child in node:
            walk(child)
            if child.tail:
                pieces.append(child.tail)

    walk(root)
    return "".join(pieces).replace("\r\n", "\n").replace("\r", "\n")


class Inspector:
    def __init__(self, path: Path, limits: Limits):
        self.path = path
        self.limits = limits
        self.report = Inspection(source_sha256=sha256_file(path))
        self.files: dict[str, bytes] = {}
        self.documents: dict[str, etree._Element] = {}
        self.ids: dict[str, set[str]] = {}

    def add(self, code: str, severity: str, message: str, location: str | None = None):
        self.report.findings.append(
            Finding(code=code, severity=severity, message=message, location=location)
        )

    def xml(self, path: str):
        try:
            return parse_xml(self.files[path])
        except (etree.XMLSyntaxError, ValueError) as exc:
            self.add("xml.invalid", "error", str(exc), path)
            return None

    def run(self) -> Inspection:
        if self.path.stat().st_size > self.limits.max_archive_mb * 1024**2:
            self.add("zip.limit", "error", "Archive exceeds configured size limit.")
            return self.report
        try:
            with zipfile.ZipFile(self.path) as archive:
                members = archive.infolist()
                names = [item.filename for item in members]
                if (
                    len(members) > self.limits.max_members
                    or sum(i.file_size for i in members) > self.limits.max_uncompressed_mb * 1024**2
                ):
                    self.add(
                        "zip.limit",
                        "error",
                        "Archive exceeds member count or uncompressed size limit.",
                    )
                    return self.report
                if len(set(names)) != len(names):
                    self.add("zip.duplicate", "error", "Duplicate ZIP member names are ambiguous.")
                    return self.report
                for item in members:
                    p = PurePosixPath(item.filename)
                    if (
                        p.is_absolute()
                        or ".." in p.parts
                        or "\\" in item.filename
                        or "\x00" in item.filename
                        or str(p) != item.filename.rstrip("/")
                    ):
                        self.add(
                            "zip.path",
                            "error",
                            "Unsafe or noncanonical ZIP member path.",
                            item.filename,
                        )
                        return self.report
                    if item.flag_bits & 1 or ((item.external_attr >> 16) & 0o170000) == 0o120000:
                        self.add(
                            "zip.encrypted_or_symlink",
                            "error",
                            "Encrypted ZIP entries and symlinks are unsupported.",
                            item.filename,
                        )
                        return self.report
                    if item.file_size > self.limits.max_member_mb * 1024**2:
                        self.add(
                            "zip.limit",
                            "error",
                            "Member exceeds configured size limit.",
                            item.filename,
                        )
                        return self.report
                if (
                    not members
                    or members[0].filename != "mimetype"
                    or members[0].compress_type != zipfile.ZIP_STORED
                ):
                    self.add(
                        "zip.mimetype_order",
                        "error",
                        "mimetype must be the first, uncompressed ZIP entry.",
                    )
                if "mimetype" in names:
                    mime = archive.getinfo("mimetype")
                    if mime.header_offset != 0 or local_extra_bytes(self.path, mime) != 0:
                        self.add(
                            "zip.mimetype_header",
                            "error",
                            "mimetype must start at byte zero and have no local ZIP extra fields.",
                        )
                for item in members:
                    if not item.is_dir():
                        self.files[item.filename] = archive.read(item)  # verifies CRC
        except (
            zipfile.BadZipFile,
            RuntimeError,
            NotImplementedError,
            OSError,
            zlib.error,
            EOFError,
        ) as exc:
            self.add("zip.invalid", "error", f"Cannot read EPUB: {exc}")
            return self.report

        self.report.resource_hashes = {
            p: hashlib.sha256(data).hexdigest() for p, data in sorted(self.files.items())
        }
        if self.files.get("mimetype") != b"application/epub+zip":
            self.add("zip.mimetype", "error", "mimetype must contain exactly application/epub+zip.")
        if "META-INF/encryption.xml" in self.files:
            encryption = self.xml("META-INF/encryption.xml")
            if encryption is not None:
                algorithms = encryption.xpath("//*[local-name()='EncryptionMethod']/@Algorithm")
                font_obfuscation = {
                    "http://www.idpf.org/2008/embedding",
                    "http://ns.adobe.com/pdf/enc#RC",
                }
                if not algorithms or any(a not in font_obfuscation for a in algorithms):
                    self.add(
                        "encryption.unsupported",
                        "error",
                        "Encrypted content cannot be inspected; DRM removal is not supported.",
                    )
                else:
                    self.add("fonts.obfuscated", "info", "Embedded font obfuscation is preserved.")
        if "META-INF/container.xml" not in self.files:
            self.add("container.missing", "error", "META-INF/container.xml is missing.")
            return self.report
        container = self.xml("META-INF/container.xml")
        if container is None:
            return self.report
        roots = container.xpath(
            "/c:container/c:rootfiles/c:rootfile[@media-type='application/oebps-package+xml']/@full-path",
            namespaces=NS,
        )
        if len(roots) != 1:
            self.add(
                "container.renditions",
                "error",
                "Exactly one package rendition is supported; manual review is needed.",
            )
            return self.report
        package = roots[0]
        if package not in self.files:
            self.add("package.missing", "error", "Package document is missing.", package)
            return self.report
        self.report.package_path = package
        opf = self.xml(package)
        if opf is None:
            return self.report
        if opf.tag != f"{{{NS['opf']}}}package":
            self.add("package.root", "error", "Invalid OPF package root.", package)
            return self.report
        version = opf.get("version", "")
        self.report.epub_version = version
        if version not in {"2.0", "3.0"}:
            self.add("package.version", "error", f"Unsupported package version: {version}", package)
        for field in [
            "title",
            "creator",
            "language",
            "identifier",
            "publisher",
            "date",
            "description",
        ]:
            values = opf.xpath(f"opf:metadata/dc:{field}", namespaces=NS)
            self.report.metadata[field] = ["".join(x.itertext()).strip() for x in values]
        for field in ["title", "language", "identifier"]:
            if not any(self.report.metadata[field]):
                self.add(
                    "metadata.missing", "error", f"Missing required metadata: {field}", package
                )
        uid = opf.get("unique-identifier")
        if not uid or not opf.xpath("opf:metadata/dc:identifier[@id=$uid]", namespaces=NS, uid=uid):
            self.add(
                "metadata.unique_identifier",
                "error",
                "unique-identifier must point to a dc:identifier.",
                package,
            )

        manifest = {}
        paths = set()
        for item in opf.xpath("opf:manifest/opf:item", namespaces=NS):
            iid, href = item.get("id", ""), item.get("href", "")
            if not iid or not href or not item.get("media-type"):
                self.add(
                    "manifest.attributes",
                    "error",
                    "Manifest item lacks id, href, or media-type.",
                    package,
                )
                continue
            if iid in manifest:
                self.add("manifest.duplicate_id", "error", f"Duplicate manifest id: {iid}", package)
            try:
                resolved = local_target(package, href)
            except ValueError as exc:
                self.add("manifest.path", "error", str(exc), href)
                continue
            if resolved is None:
                self.add(
                    "manifest.remote",
                    "error",
                    "Remote resources cannot be preserved or validated offline.",
                    href,
                )
                continue
            target, fragment = resolved
            if fragment:
                self.add(
                    "manifest.fragment", "error", "Manifest href must not include a fragment.", href
                )
            if target in paths:
                self.add(
                    "manifest.duplicate_path",
                    "error",
                    "Multiple manifest items reference the same file.",
                    target,
                )
            paths.add(target)
            manifest[iid] = (target, item)
            if target not in self.files:
                self.add("manifest.missing", "error", "Manifest resource is missing.", target)
                continue
            media = item.get("media-type")
            if media in DOCUMENT_TYPES:
                document = self.xml(target)
                if document is not None:
                    self.documents[target] = document
                    ids = document.xpath(
                        "//@id | //@xml:id",
                        namespaces={"xml": "http://www.w3.org/XML/1998/namespace"},
                    )
                    for identifier, count in Counter(ids).items():
                        if count > 1:
                            self.add(
                                "navigation.duplicate_id",
                                "error",
                                f"Duplicate element id: {identifier}",
                                target,
                            )
                    self.ids[target] = set(ids)
            elif media == "text/css":
                self.css(self.files[target], target)
            elif media and media.startswith("image/"):
                self.image(self.files[target], target)

        spines = opf.xpath("opf:spine", namespaces=NS)
        if len(spines) != 1 or not spines[0].findall("opf:itemref", NS):
            self.add("spine.missing", "error", "Package must contain a nonempty spine.", package)
        else:
            for ref in spines[0].findall("opf:itemref", NS):
                iid = ref.get("idref")
                if iid not in manifest:
                    self.add(
                        "spine.missing_item",
                        "error",
                        f"Spine idref has no manifest item: {iid}",
                        package,
                    )
                else:
                    target, item = manifest[iid]
                    self.report.spine.append(target)
                    if item.get("media-type") not in {"application/xhtml+xml", "image/svg+xml"}:
                        self.add(
                            "spine.unsupported",
                            "error",
                            "Non-XHTML/SVG spine content requires manual review.",
                            target,
                        )
            if len(set(self.report.spine)) != len(self.report.spine):
                self.add(
                    "spine.duplicate",
                    "error",
                    "Spine references the same document more than once.",
                    package,
                )

        navs = [
            path for path, item in manifest.values() if "nav" in item.get("properties", "").split()
        ]
        if version == "3.0":
            if len(navs) != 1:
                self.add(
                    "navigation.missing",
                    "error",
                    "EPUB 3 requires exactly one navigation document.",
                    package,
                )
            elif navs[0] in self.documents:
                toc = self.documents[navs[0]].xpath(
                    "//h:nav[contains(concat(' ', @epub:type, ' '), ' toc ')]//h:a[@href]",
                    namespaces=NS,
                )
                if not toc:
                    self.add(
                        "navigation.empty",
                        "error",
                        "Navigation document has no linked table of contents.",
                        navs[0],
                    )
        elif spines:
            ncx_id = spines[0].get("toc")
            if (
                ncx_id not in manifest
                or manifest[ncx_id][1].get("media-type") != "application/x-dtbncx+xml"
            ):
                self.add(
                    "navigation.missing",
                    "error",
                    "EPUB 2 spine must reference an NCX table of contents.",
                    package,
                )
            elif manifest[ncx_id][0] in self.documents:
                ncx_path = manifest[ncx_id][0]
                if not self.documents[ncx_path].xpath(
                    "//ncx:navMap/ncx:navPoint//ncx:content[@src]", namespaces=NS
                ):
                    self.add(
                        "navigation.empty",
                        "error",
                        "NCX has no linked table of contents.",
                        ncx_path,
                    )

        texts = {}
        for path, document in self.documents.items():
            self.document(path, document)
            if document.tag == f"{{{NS['h']}}}html":
                texts[path] = body_text(document)
                if "\ufffd" in texts[path]:
                    self.add(
                        "text.replacement_character",
                        "warning",
                        "Text contains a Unicode replacement character; inspect the source.",
                        path,
                    )
                if (
                    path in self.report.spine
                    and not texts[path].strip()
                    and not document.xpath(
                        "//*[local-name()='img' or local-name()='svg' or local-name()='image']"
                    )
                ):
                    self.add(
                        "text.empty",
                        "warning",
                        "Spine document contains no visible text or illustrations.",
                        path,
                    )
        all_text = [(path, texts[path]) for path in sorted(texts)]
        spine_text = [(path, texts.get(path, "")) for path in self.report.spine]
        self.report.text_sha256 = hashlib.sha256(
            json.dumps(all_text, ensure_ascii=False).encode()
        ).hexdigest()
        self.report.spine_text_sha256 = hashlib.sha256(
            json.dumps(spine_text, ensure_ascii=False).encode()
        ).hexdigest()
        self.report.text_characters = sum(len(t) for t in texts.values())
        groups: dict[str, list[str]] = {}
        for path, text in spine_text:
            if len(text.strip()) > 100:
                groups.setdefault(text, []).append(path)
        for duplicates in groups.values():
            if len(duplicates) > 1:
                self.add(
                    "text.duplicate",
                    "warning",
                    "Identical text in spine documents: " + ", ".join(duplicates),
                )
        return self.report

    def link(self, source: str, href: str, resource: bool = False):
        try:
            resolved = local_target(source, href)
        except ValueError as exc:
            self.add("navigation.unsafe", "error", str(exc), source)
            return
        if resolved is None:
            if resource and not href.startswith("data:"):
                self.add(
                    "resource.remote",
                    "error",
                    "Remote embedded resource is not available offline.",
                    source,
                )
            return
        target, fragment = resolved
        if target not in self.files:
            self.add("navigation.missing_target", "error", f"Missing link target: {href}", source)
        elif fragment and target in self.ids and fragment not in self.ids[target]:
            self.add(
                "navigation.missing_fragment", "error", f"Missing fragment target: {href}", source
            )

    def document(self, path: str, document: etree._Element):
        inline_count = 0
        for element in document.iter():
            if not isinstance(element.tag, str):
                continue
            tag = etree.QName(element).localname
            if tag == "script" or any(key.lower().startswith("on") for key in element.attrib):
                self.add(
                    "content.scripted", "error", "Scripted content requires manual review.", path
                )
            if element.get("{http://www.w3.org/XML/1998/namespace}base") or tag == "base":
                self.add(
                    "content.base",
                    "error",
                    "Custom document bases are unsupported for offline validation.",
                    path,
                )
            for attr in ["href", "src", "poster", "data", "{http://www.w3.org/1999/xlink}href"]:
                href = element.get(attr)
                if href is not None and (attr != "data" or tag == "object"):
                    self.link(path, href, resource=tag not in {"a", "content"})
            if element.get("srcset"):
                from emend.image_references import srcset_urls

                try:
                    for _, _, url in srcset_urls(element.get("srcset")):
                        self.link(path, url, resource=True)
                except ValueError as exc:
                    self.add("resource.srcset", "error", str(exc), path)
            if tag == "img" and element.get("alt") is None:
                self.add("accessibility.alt", "warning", "Image lacks alt text.", path)
            if element.get("style"):
                inline_count += 1
                self.css(element.get("style").encode(), path, inline=True)
            if tag == "style":
                self.css((element.text or "").encode(), path)
        if inline_count > 30:
            self.add(
                "typography.inline",
                "warning",
                f"{inline_count} inline styles; retained unchanged.",
                path,
            )

    def css(self, data: bytes, path: str, inline: bool = False):
        if inline:
            nodes = tinycss2.parse_declaration_list(
                data.decode("utf-8", errors="replace"), skip_comments=True, skip_whitespace=True
            )
        else:
            nodes, _ = tinycss2.parse_stylesheet_bytes(
                data, skip_comments=True, skip_whitespace=True
            )
        flags = set()

        def walk(tokens):
            for token in tokens:
                if token.type == "error":
                    flags.add("parse_error")
                if token.type == "url":
                    self.link(path, token.value, resource=True)
                if token.type == "function" and token.lower_name == "url":
                    args = [x for x in token.arguments if x.type not in {"whitespace", "comment"}]
                    if len(args) == 1 and args[0].type == "string":
                        self.link(path, args[0].value, resource=True)
                if token.type == "at-rule" and token.lower_at_keyword == "import":
                    values = [x for x in token.prelude if x.type not in {"whitespace", "comment"}]
                    if values and values[0].type == "string":
                        self.link(path, values[0].value, resource=True)
                if token.type == "declaration":
                    if token.lower_name in {"color", "background-color"}:
                        flags.add("forced_color")
                    if token.lower_name == "font-size" and any(
                        x.type == "dimension" and x.lower_unit in {"px", "pt"} for x in token.value
                    ):
                        flags.add("fixed_size")
                if token.type == "qualified-rule":
                    walk(
                        tinycss2.parse_declaration_list(
                            token.content, skip_comments=True, skip_whitespace=True
                        )
                    )
                for attr in ["prelude", "content", "arguments", "value"]:
                    children = getattr(token, attr, None)
                    if isinstance(children, list):
                        walk(children)

        walk(nodes)
        for flag in sorted(flags):
            self.add(
                f"typography.{flag}",
                "warning",
                "Styles may need review; publisher CSS is preserved unchanged.",
                path,
            )

    def image(self, data: bytes, path: str):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    image.verify()
        except (
            OSError,
            ValueError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ) as exc:
            self.add("image.invalid", "error", f"Image cannot be verified: {exc}", path)


def inspect_epub(path: Path, limits: Limits | None = None) -> Inspection:
    limits = limits or Limits()
    if not path.is_file():
        raise ValueError(f"EPUB not found: {path}")
    if path.stat().st_size > limits.max_archive_mb * 1024**2:
        raise ValueError("Input exceeds the configured archive size limit.")
    return Inspector(path, limits).run()
