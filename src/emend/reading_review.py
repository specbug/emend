"""Bounded, offline evidence for font declarations, marked notes and reading direction."""

import html
import tempfile
import uuid
import zipfile
from pathlib import Path

import tinycss2
from pydantic import BaseModel, ConfigDict, Field

from emend import ENGINE_VERSION
from emend.config import Config
from emend.inspect import NS, inspect_epub, local_target, parse_xml, sha256_file
from emend.models import Finding
from emend.reports import report_styles
from emend.research.discovery import current_source
from emend.revalidation import snapshot
from emend.storage import Store, now

MAX_DOCUMENTS = 1000
MAX_REFERENCES = 5000
MAX_FONTS = 200
FONT_TYPES = {
    "font/ttf",
    "font/otf",
    "font/woff",
    "font/woff2",
    "application/vnd.ms-opentype",
    "application/font-sfnt",
    "application/x-font-ttf",
    "application/x-font-opentype",
}


class FontRecord(BaseModel):
    resource: str
    media_type: str
    bytes: int
    sha256: str
    obfuscated: bool = False
    declared_in: list[str] = Field(default_factory=list)


class NoteReference(BaseModel):
    document: str
    href: str
    reference_id: str | None = None
    target_document: str
    target_id: str
    semantic_note: bool
    reciprocal_backlink: bool


class ReadingDocument(BaseModel):
    document: str
    language: str | None = None
    direction: str


class ReadingReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    book_id: str
    artifact_sha256: str
    engine_version: str
    created_at: str
    title: str
    complete: bool
    documents: list[ReadingDocument]
    fonts: list[FontRecord]
    notes: list[NoteReference]
    findings: list[Finding]


def css_sources(data, source, findings):
    rules, _ = tinycss2.parse_stylesheet_bytes(data, skip_comments=True, skip_whitespace=True)
    sources = []

    def walk(nodes, depth=0):
        if depth > 16:
            findings.append(
                Finding(
                    code="review.limit",
                    severity="warning",
                    message="Nested CSS review depth limit reached.",
                    location=source,
                )
            )
            return
        for node in nodes:
            if len(sources) >= 5000:
                findings.append(
                    Finding(
                        code="review.limit",
                        severity="warning",
                        message="CSS font-source review limit reached.",
                        location=source,
                    )
                )
                return
            if node.type == "qualified-rule" and node.content:
                for declaration in tinycss2.parse_declaration_list(
                    node.content, skip_comments=True, skip_whitespace=True
                ):
                    if declaration.type == "declaration" and declaration.lower_name in {
                        "direction",
                        "writing-mode",
                    }:
                        value = tinycss2.serialize(declaration.value).strip()[:200]
                        findings.append(
                            Finding(
                                code="layout.direction_css",
                                severity="info",
                                message=f"Declared {declaration.lower_name}: {value}. Verify the applicable documents in the intended reader; this is not a resolved cascade.",
                                location=source,
                            )
                        )
            if node.type != "at-rule":
                continue
            if node.lower_at_keyword == "font-face" and node.content:
                declarations = tinycss2.parse_declaration_list(
                    node.content, skip_comments=True, skip_whitespace=True
                )
                for declaration in declarations:
                    if declaration.type != "declaration" or declaration.lower_name != "src":
                        continue
                    for token in declaration.value:
                        value = None
                        if token.type == "url":
                            value = token.value
                        elif token.type == "function" and token.lower_name == "url":
                            arguments = [
                                t
                                for t in token.arguments
                                if t.type not in {"whitespace", "comment"}
                            ]
                            if len(arguments) == 1 and arguments[0].type == "string":
                                value = arguments[0].value
                        elif token.type == "function" and token.lower_name == "local":
                            findings.append(
                                Finding(
                                    code="font.local",
                                    severity="info",
                                    message="Font face depends on a locally installed font; rendering can vary by reader.",
                                    location=source,
                                )
                            )
                        if value:
                            target = local_target(source, value)
                            if target:
                                sources.append(target[0])
            elif node.content and node.lower_at_keyword in {"media", "supports", "layer"}:
                walk(
                    tinycss2.parse_rule_list(
                        node.content, skip_comments=True, skip_whitespace=True
                    ),
                    depth + 1,
                )

    walk(rules)
    return sources


def reading_review(book_id: str, config: Config) -> ReadingReview:
    store = Store(config.root)
    with store.locked():
        row, source, digest = current_source(store, book_id, config)
        with tempfile.TemporaryDirectory(dir=config.root / ".staging") as staging:
            current = Path(staging) / "current.epub"
            snapshot(source, current, config.limits.max_archive_mb * 1024**2)
            inspection = inspect_epub(current, config.limits)
            if (
                inspection.source_sha256 != digest
                or inspection.blocking
                or not inspection.package_path
            ):
                raise ValueError(
                    "Resolve structural errors or changed inputs before reading review."
                )
            findings = []
            fonts = []
            notes = []
            documents = []
            complete = True
            with zipfile.ZipFile(current) as archive:
                package = parse_xml(archive.read(inspection.package_path))
                media = {}
                for item in package.xpath("opf:manifest/opf:item", namespaces=NS):
                    target = local_target(inspection.package_path, item.get("href", ""))
                    if target:
                        media[target[0]] = item.get("media-type", "")
                obfuscated = set()
                if "META-INF/encryption.xml" in inspection.resource_hashes:
                    encryption = parse_xml(archive.read("META-INF/encryption.xml"))
                    for uri in encryption.xpath("//*[local-name()='CipherReference']/@URI"):
                        target = local_target("", uri)
                        if target:
                            obfuscated.add(target[0])
                declarations = {}
                for name, kind in media.items():
                    if kind == "text/css":
                        for target in css_sources(archive.read(name), name, findings):
                            declarations.setdefault(target, []).append(name)
                roots = {}
                for name, kind in media.items():
                    if kind != "application/xhtml+xml":
                        continue
                    if len(roots) >= MAX_DOCUMENTS:
                        complete = False
                        break
                    root = parse_xml(archive.read(name))
                    roots[name] = root
                    lang = root.get("{http://www.w3.org/XML/1998/namespace}lang") or root.get(
                        "lang"
                    )
                    direction = root.get("dir", "ltr")
                    documents.append(
                        ReadingDocument(document=name, language=lang, direction=direction)
                    )
                    if not lang:
                        findings.append(
                            Finding(
                                code="document.language",
                                severity="warning",
                                message="No language on the XHTML root; confirm language and assistive-reading behavior.",
                                location=name,
                            )
                        )
                    if direction != "ltr":
                        findings.append(
                            Finding(
                                code="document.direction",
                                severity="info",
                                message=f"Declared direction: {direction}. Verify reading order and pagination in the target reader.",
                                location=name,
                            )
                        )
                    for style in root.xpath("//h:style", namespaces=NS):
                        for target in css_sources(
                            ("".join(style.itertext())).encode(), name, findings
                        ):
                            declarations.setdefault(target, []).append(name)
                for name, kind in media.items():
                    if kind not in FONT_TYPES and Path(name).suffix.lower() not in {
                        ".ttf",
                        ".otf",
                        ".woff",
                        ".woff2",
                    }:
                        continue
                    if len(fonts) >= MAX_FONTS:
                        complete = False
                        break
                    record = FontRecord(
                        resource=name,
                        media_type=kind,
                        bytes=archive.getinfo(name).file_size,
                        sha256=inspection.resource_hashes[name],
                        obfuscated=name in obfuscated,
                        declared_in=sorted(set(declarations.get(name, []))),
                    )
                    fonts.append(record)
                    if record.obfuscated:
                        findings.append(
                            Finding(
                                code="font.obfuscated",
                                severity="info",
                                message="Obfuscated font bytes are preserved; font loading and glyph coverage need reader verification.",
                                location=name,
                            )
                        )
                    if not record.declared_in:
                        findings.append(
                            Finding(
                                code="font.no_declaration",
                                severity="info",
                                message="No font-face source found in inspected manifest/inline stylesheets. Check imported/reader-specific font use.",
                                location=name,
                            )
                        )
                ids = {
                    name: {
                        value: node
                        for node in root.iter()
                        if isinstance(node.tag, str)
                        for value in (
                            node.get("id"),
                            node.get("{http://www.w3.org/XML/1998/namespace}id"),
                        )
                        if value
                    }
                    for name, root in roots.items()
                }
                references_seen = 0
                for name, root in roots.items():
                    for node in root.xpath("//h:a[@href]", namespaces=NS):
                        if (
                            "noteref" not in node.get(f"{{{NS['epub']}}}type", "").split()
                            and "doc-noteref" not in node.get("role", "").split()
                        ):
                            continue
                        if references_seen >= MAX_REFERENCES:
                            complete = False
                            break
                        references_seen += 1
                        href = node.get("href")
                        target = local_target(name, href)
                        if (
                            not target
                            or target[0] not in ids
                            or not target[1]
                            or target[1] not in ids[target[0]]
                        ):
                            findings.append(
                                Finding(
                                    code="note.unresolved",
                                    severity="warning",
                                    message="Marked note does not resolve to an inspected XHTML element. Review it manually.",
                                    location=name,
                                )
                            )
                            continue
                        note = ids[target[0]][target[1]]
                        region = note
                        semantic = False
                        for ancestor in (note, *note.iterancestors()):
                            tokens = (
                                ancestor.get(f"{{{NS['epub']}}}type", "").split()
                                + ancestor.get("role", "").split()
                            )
                            if any(
                                t in tokens
                                for t in ("footnote", "endnote", "doc-footnote", "doc-endnote")
                            ):
                                region = ancestor
                                semantic = True
                                break
                        ref_id = node.get("id") or node.get(
                            "{http://www.w3.org/XML/1998/namespace}id"
                        )
                        reciprocal = False
                        if ref_id:
                            for link in region.xpath(".//h:a[@href]", namespaces=NS):
                                back = local_target(target[0], link.get("href"))
                                if back == (name, ref_id):
                                    reciprocal = True
                                    break
                        notes.append(
                            NoteReference(
                                document=name,
                                href=href,
                                reference_id=ref_id,
                                target_document=target[0],
                                target_id=target[1],
                                semantic_note=semantic,
                                reciprocal_backlink=reciprocal,
                            )
                        )
                        if not semantic:
                            findings.append(
                                Finding(
                                    code="note.semantics",
                                    severity="info",
                                    message="Marked note target lacks explicit footnote/endnote semantics; inspect accessibility/reader behavior.",
                                    location=f"{target[0]}#{target[1]}",
                                )
                            )
                        if not reciprocal:
                            findings.append(
                                Finding(
                                    code="note.backlink",
                                    severity="warning",
                                    message="No reciprocal backlink to this marked reference was found in its note region. Reader back navigation may still exist; test it.",
                                    location=f"{name}#{ref_id or '(no id)'}",
                                )
                            )
            complete = complete and not any(f.code == "review.limit" for f in findings)
            if not complete:
                findings.append(
                    Finding(
                        code="review.limit",
                        severity="warning",
                        message="Reading-review bounds reached; this evidence is incomplete. Inspect remaining resources manually.",
                    )
                )
            _, _, latest = current_source(store, row["id"], config)
            if latest != digest or sha256_file(current) != digest:
                raise ValueError("An EPUB changed during reading review.")
            result = ReadingReview(
                id=uuid.uuid4().hex,
                book_id=row["id"],
                artifact_sha256=digest,
                engine_version=ENGINE_VERSION,
                created_at=now(),
                title="; ".join(inspection.metadata.get("title", [])),
                complete=complete,
                documents=documents,
                fonts=fonts,
                notes=notes,
                findings=findings,
            )
            encoded = result.model_dump_json(indent=2)
            if len(encoded.encode()) > 8 * 1024**2:
                raise ValueError(
                    "Reading-review evidence exceeds the 8 MiB limit; inspect this input manually."
                )
            directory = config.root / "reading" / row["id"] / result.id
            directory.mkdir(parents=True)
            path = directory / "review.json"
            path.write_text(encoded)
            write_html(result, directory)
            store.save_document(result.id, row["id"], "reading-review", path, sha256_file(path))
            return result


def load_reading_review(store, book_id, identifier=None):
    document = store.document(book_id, "reading-review", identifier)
    path = Path(document["path"])
    expected = store.root / "reading" / book_id / document["id"] / "review.json"
    if (
        path.resolve() != expected.resolve()
        or path.stat().st_size > 8 * 1024**2
        or sha256_file(path) != document["sha256"]
    ):
        raise ValueError("Reading review changed or has an invalid location/size.")
    result = ReadingReview.model_validate_json(path.read_text())
    if result.id != document["id"] or result.book_id != book_id:
        raise ValueError("Reading review identity mismatch.")
    return result


def write_html(review, directory):
    esc = html.escape
    fonts = "".join(
        f"<li><code>{esc(f.resource)}</code><p>{f.bytes:,} bytes · {esc(f.media_type)} · {'obfuscated' if f.obfuscated else 'not declared obfuscated'}</p><p>Declared in: {esc(', '.join(f.declared_in) or 'not found')}</p><code>{f.sha256}</code></li>"
        for f in review.fonts
    )
    notes = "".join(
        f"<li><code>{esc(n.document)} → {esc(n.href)}</code><p>Explicit note semantics: {n.semantic_note}. Reciprocal backlink: {n.reciprocal_backlink}.</p></li>"
        for n in review.notes
    )
    findings = "".join(
        f"<li><strong>{esc(f.code)}</strong><p>{esc(f.message)}</p><code>{esc(f.location or '')}</code></li>"
        for f in review.findings
    )
    docs = "".join(
        f"<li><code>{esc(d.document)}</code> · language {esc(d.language or 'unspecified')} · direction {esc(d.direction)}</li>"
        for d in review.documents
    )
    (directory / "review.html").write_text(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; font-src data:"><title>Emend reading review</title><style>{report_styles()} h2,p{{overflow-wrap:anywhere}}</style></head><body><main><header><div class="brand">EMEND / READING REVIEW</div><h1>{esc(review.title)}</h1><p>{"Complete bounded inspection" if review.complete else "Incomplete inspection"} · advisory evidence</p><code>Artifact {review.artifact_sha256}</code></header><h2>Scope</h2><p>Offline inventory of manifest fonts, font-face sources, explicitly marked note references, language and declared direction. This does not measure glyph coverage, screen-reader conformance, footnote popups, or actual reader behavior. Unmarked note links require manual inspection. CSS direction hints do not resolve the cascade. No EPUB, approval or publishing state changed.</p><h2>Findings</h2><ul>{findings or "<li>No additional findings in the inspected scope. This is not accessibility certification.</li>"}</ul><h2>Font inventory</h2><ul>{fonts or "<li>No manifest font resources found.</li>"}</ul><h2>Marked note references</h2><ul>{notes or "<li>No explicitly marked note references found. This does not establish that the book has no notes.</li>"}</ul><h2>Documents</h2><ul>{docs}</ul><footer>Generated {esc(review.created_at)}. Evidence belongs to the recorded artifact and becomes stale after an edit.</footer></main></body></html>"""
    )
