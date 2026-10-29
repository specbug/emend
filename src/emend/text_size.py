"""Reset publisher body-text sizes so readers use the size chosen on the device.

CrossPoint (the X4 Pro firmware) applies a book's relative ``font-size`` rules on top
of the reader's own size, ignores ``!important`` and resolves conflicts by selector
specificity, so an added override stylesheet cannot be relied on there. This module
therefore edits only the publisher's own ``font-size`` values in plain ``html``,
``body`` and ``p`` rules, after a compared preview and explicit approval. Every other
byte is checked independently before the candidate is validated and activated.
"""

import html
import shutil
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import tinycss2
from pydantic import BaseModel, ConfigDict, Field

from emend import ENGINE_VERSION
from emend.archive import copy_entry
from emend.config import Config
from emend.inspect import NS, inspect_epub, local_target, parse_xml, sha256_file
from emend.models import Gate, TextSizeChange
from emend.navigation_repair import same_xml, serialize
from emend.pagination import check_package, validate_layout
from emend.pipeline import atomic_copy, get_report
from emend.reports import report_styles, write_reports
from emend.research.discovery import current_source
from emend.revalidation import snapshot
from emend.storage import Store, now
from emend.validation import validate_ace, validate_epubcheck
from emend.visual import PROFILES, Sample, render, select_documents

ACTION = "css.body_text_size"
TARGETS = {"html", "body", "p"}
NEUTRAL = {"em": "1em", "rem": "1rem", "%": "100%", "keyword": "1em"}
# CrossPoint's own keyword scale (lib/Epub/Epub/css/CssParser.cpp, tryInterpretFontSize).
KEYWORDS = {
    "xx-small": 0.6,
    "x-small": 0.75,
    "small": 0.89,
    "medium": 1.0,
    "large": 1.2,
    "x-large": 1.5,
    "xx-large": 2.0,
    "smaller": 0.83,
    "larger": 1.2,
}
MAX_SHEET_BYTES = 2 * 1024**2
MAX_SHEETS = 200
MAX_EDITS = 200
BOM = b"\xef\xbb\xbf"


class TextSizeRejectedError(ValueError):
    """A candidate failed required quality gates; the current book is unchanged."""


class SizeEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stylesheet: str = Field(max_length=1000)
    rule: int = Field(ge=0)
    selector: str = Field(max_length=500)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    before: str = Field(max_length=64)
    after: str = Field(max_length=16)
    unit: Literal["em", "rem", "%", "keyword"]
    scale: float = Field(gt=0)


class TextSizeTrial(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    book_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    artifact_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    engine_version: str
    created_at: str
    browser_version: str
    title: str
    edits: list[SizeEdit]
    notes: list[str]
    layout: Literal["scroll", "paginated"]
    page_margin: int | None = None
    selected_documents: list[str]
    profiles: list[str]
    status: Literal["complete", "incomplete"]
    before: list[Sample]
    after: list[Sample]


def decode(data: bytes) -> tuple[bytes, str] | None:
    """UTF-8 stylesheets only, so every untouched byte can be written back exactly."""
    prefix = BOM if data.startswith(BOM) else b""
    try:
        text = data[len(prefix) :].decode("utf-8")
    except UnicodeDecodeError:
        return None
    if text.encode("utf-8") != data[len(prefix) :]:
        return None
    return prefix, text


def line_starts(text: str) -> list[int]:
    starts = [0]
    for index, character in enumerate(text):
        if character == "\n":
            starts.append(index + 1)
    return starts


def interpret(tokens) -> tuple[str, float] | None:
    """A single relative font-size value: (unit, scale). Absolute sizes return None."""
    if len(tokens) != 1:
        return None
    token = tokens[0]
    if token.type == "dimension" and token.lower_unit in {"em", "rem"} and token.value > 0:
        return token.lower_unit, float(token.value)
    if token.type == "percentage" and token.value > 0:
        return "%", float(token.value) / 100
    if token.type == "ident" and token.lower_value in KEYWORDS:
        return "keyword", KEYWORDS[token.lower_value]
    return None


def declarations(rule):
    return [
        node
        for node in tinycss2.parse_declaration_list(
            rule.content, skip_comments=True, skip_whitespace=True
        )
        if node.type == "declaration"
    ]


def selectors(rule) -> list[str]:
    return [" ".join(part.split()).lower() for part in tinycss2.serialize(rule.prelude).split(",")]


def stylesheet_names(archive, inspection, package) -> list[str]:
    names = []
    for item in package.xpath("opf:manifest/opf:item[@media-type='text/css']", namespaces=NS):
        target = local_target(inspection.package_path, item.get("href", ""))
        if target and target[0] in archive.namelist() and target[0] not in names:
            names.append(target[0])
    return names


def discover(source: Path, inspection) -> tuple[list[SizeEdit], list[str]]:
    """Read-only: planned edits plus plain-language notes about what stays unchanged."""
    notes: list[str] = []
    if inspection.blocking or not inspection.package_path:
        return [], ["Text size can be checked once the EPUB structure is readable."]
    edits: list[SizeEdit] = []
    rem_elsewhere = root_kept = False
    kept = {"class": 0, "media": 0, "absolute": 0, "shorthand": 0}
    with zipfile.ZipFile(source) as archive:
        package = parse_xml(archive.read(inspection.package_path))
        if inspection.epub_version == "3.0":
            modified = package.xpath(
                "opf:metadata/opf:meta[@property='dcterms:modified']", namespaces=NS
            )
            if len(modified) != 1 or len(modified[0]):
                return [], ["The package has no single plain modification date to update."]
        sheets = stylesheet_names(archive, inspection, package)
        if len(sheets) > MAX_SHEETS:
            return [], ["The book has too many stylesheets to check within the limits."]
        for name in sheets:
            info = archive.getinfo(name)
            if info.file_size > MAX_SHEET_BYTES:
                notes.append(f"{name} is too large to check and stays unchanged.")
                continue
            decoded = decode(archive.read(info))
            if decoded is None:
                notes.append(f"{name} is not UTF-8 and stays unchanged.")
                continue
            _, text = decoded
            starts = line_starts(text)
            sheet_edits, exact = [], True
            for index, node in enumerate(
                tinycss2.parse_stylesheet(text, skip_comments=True, skip_whitespace=True)
            ):
                if node.type == "at-rule":
                    if node.content and "font-size" in tinycss2.serialize(node.content):
                        kept["media"] += 1
                    continue
                if node.type != "qualified-rule":
                    continue
                group = selectors(node)
                target = all(selector in TARGETS for selector in group)
                for declaration in declarations(node):
                    if declaration.lower_name == "font" and target:
                        kept["shorthand"] += 1
                    if declaration.lower_name != "font-size":
                        continue
                    tokens = [
                        t for t in declaration.value if t.type not in {"whitespace", "comment"}
                    ]
                    value = interpret(tokens)
                    if not target:
                        rem_elsewhere = rem_elsewhere or (value is not None and value[0] == "rem")
                        if any(s.split(" ")[-1].split(".")[0] in TARGETS for s in group):
                            kept["class"] += 1
                        continue
                    if value is None:
                        kept["absolute"] += 1
                        root_kept = root_kept or "html" in group
                        continue
                    unit, scale = value
                    if abs(scale - 1) < 1e-9:
                        continue
                    token = tokens[0]
                    start = starts[token.source_line - 1] + token.source_column - 1
                    before = token.serialize()
                    if text[start : start + len(before)] != before or len(before) > 64:
                        exact = False
                        continue
                    sheet_edits.append(
                        SizeEdit(
                            stylesheet=name,
                            rule=index,
                            selector=", ".join(group)[:500],
                            start=start,
                            end=start + len(before),
                            before=before,
                            after=NEUTRAL[unit],
                            unit=unit,
                            scale=scale,
                        )
                    )
            if exact:
                edits.extend(sheet_edits)
            else:
                notes.append(f"{name} could not be read exactly and stays unchanged.")
    root_edited = any("html" in edit.selector.split(", ") for edit in edits)
    rem_edited = any(edit.unit == "rem" for edit in edits)
    if (root_edited and rem_elsewhere) or (root_kept and rem_edited):
        # Other rem sizes depend on the root; changing only part of the chain would
        # change their sizes too.
        return [], [
            "This book sizes text relative to its own root size, and other rules depend on "
            "it. Emend leaves its text sizes unchanged."
        ]
    if len(edits) > MAX_EDITS:
        return [], ["The book has too many size rules to change within the limits."]
    if kept["class"]:
        one = kept["class"] == 1
        notes.append(
            f"{kept['class']} size {'rule' if one else 'rules'} for classes, such as footnotes "
            f"or captions, {'stays' if one else 'stay'} as the publisher set "
            f"{'it' if one else 'them'}."
        )
    if kept["media"]:
        notes.append("Size rules inside @media blocks stay unchanged; CrossPoint skips them.")
    if kept["absolute"]:
        one = kept["absolute"] == 1
        notes.append(
            f"{kept['absolute']} absolute or unusual body {'size' if one else 'sizes'} "
            f"(px, pt or calc) {'stays' if one else 'stay'} unchanged."
        )
    if kept["shorthand"]:
        notes.append("A font shorthand on body text stays unchanged; CrossPoint does not read it.")
    return edits, notes


def effective_scale(edits: list[SizeEdit]) -> float | None:
    """Approximate body-text size relative to the reader's own size, when unambiguous."""
    body = [e for e in edits if "body" in e.selector.split(", ")]
    paragraph = [e for e in edits if "p" in e.selector.split(", ")]
    if len(body) > 1 or len(paragraph) > 1 or not (body or paragraph):
        return None
    scale = 1.0
    for edit in [*body, *paragraph]:
        # rem and absolute keywords restart from the reader's size; em, % and the
        # relative keywords (smaller, larger) multiply the size around them.
        restart = edit.unit == "rem" or (
            edit.unit == "keyword" and edit.before.lower() not in {"smaller", "larger"}
        )
        scale = edit.scale if restart else scale * edit.scale
    return scale


def rewrite(source: Path, target: Path, inspection, edits: list[SizeEdit]):
    by_sheet: dict[str, list[SizeEdit]] = {}
    for edit in edits:
        by_sheet.setdefault(edit.stylesheet, []).append(edit)
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(target, "w") as new:
        new.comment = old.comment
        replaced = {}
        for name, sheet_edits in by_sheet.items():
            decoded = decode(old.read(name))
            if decoded is None:
                raise ValueError("Stylesheet is no longer UTF-8.")
            prefix, text = decoded
            for edit in sorted(sheet_edits, key=lambda e: e.start, reverse=True):
                if text[edit.start : edit.end] != edit.before:
                    raise ValueError("Text-size plan no longer matches the stylesheet.")
                text = text[: edit.start] + edit.after + text[edit.end :]
            replaced[name] = prefix + text.encode("utf-8")
        if inspection.epub_version == "3.0":
            package = parse_xml(old.read(inspection.package_path))
            modified = package.xpath(
                "opf:metadata/opf:meta[@property='dcterms:modified']", namespaces=NS
            )
            if len(modified) != 1 or len(modified[0]):
                raise ValueError("Expected one plain EPUB 3 modification timestamp.")
            modified[0].text = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            replaced[inspection.package_path] = serialize(package)
        for info in old.infolist():
            copy_entry(new, info, replaced.get(info.filename, old.read(info)))


def sheet_preserved(old_bytes: bytes, new_bytes: bytes, edits: list[SizeEdit]) -> bool:
    """Bytes outside the planned spans are identical, and the parsed rules differ only there."""
    old, new = decode(old_bytes), decode(new_bytes)
    if old is None or new is None or old[0] != new[0]:
        return False
    old_text, new_text = old[1], new[1]
    shift, cursor_old, cursor_new = 0, 0, 0
    for edit in sorted(edits, key=lambda e: e.start):
        if edit.start < cursor_old or old_text[edit.start : edit.end] != edit.before:
            return False
        length = edit.start - cursor_old
        if old_text[cursor_old : edit.start] != new_text[cursor_new : cursor_new + length]:
            return False
        cursor_new += length
        if new_text[cursor_new : cursor_new + len(edit.after)] != edit.after:
            return False
        cursor_new += len(edit.after)
        cursor_old = edit.end
        shift += len(edit.after) - len(edit.before)
    if old_text[cursor_old:] != new_text[cursor_new:] or len(new_text) != len(old_text) + shift:
        return False
    before = tinycss2.parse_stylesheet(old_text, skip_comments=True, skip_whitespace=True)
    after = tinycss2.parse_stylesheet(new_text, skip_comments=True, skip_whitespace=True)
    if len(before) != len(after):
        return False
    planned = {}
    for edit in edits:
        planned.setdefault(edit.rule, []).append((edit.before, edit.after))
    for index, (a, b) in enumerate(zip(before, after, strict=True)):
        if index not in planned:
            if tinycss2.serialize([a]) != tinycss2.serialize([b]):
                return False
            continue
        if a.type != "qualified-rule" or b.type != "qualified-rule":
            return False
        if tinycss2.serialize(a.prelude) != tinycss2.serialize(b.prelude):
            return False
        old_declarations, new_declarations = declarations(a), declarations(b)
        if len(old_declarations) != len(new_declarations):
            return False
        expected = list(planned[index])
        for x, y in zip(old_declarations, new_declarations, strict=True):
            if x.lower_name != y.lower_name or x.important != y.important:
                return False
            old_value = tinycss2.serialize(x.value).strip()
            new_value = tinycss2.serialize(y.value).strip()
            if old_value == new_value:
                continue
            if x.lower_name != "font-size" or (old_value, new_value) not in expected:
                return False
            expected.remove((old_value, new_value))
        if expected:
            return False
    return True


def preserved(source: Path, candidate: Path, inspection, edits: list[SizeEdit]) -> bool:
    sheets = {edit.stylesheet for edit in edits}
    changed = {*sheets, *([inspection.package_path] if inspection.epub_version == "3.0" else [])}
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(candidate) as new:
        if old.namelist() != new.namelist() or old.comment != new.comment:
            return False
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
            elif info.filename in sheets:
                if not sheet_preserved(
                    a_bytes, b_bytes, [e for e in edits if e.stylesheet == info.filename]
                ):
                    return False
            else:
                a, b = parse_xml(a_bytes), parse_xml(b_bytes)
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
                if not same_xml(a, b):
                    return False
    return True


def text_size_trial(
    book_id: str,
    config: Config,
    *,
    profiles: list[str] | None = None,
    max_documents: int = 3,
    layout: str = "paginated",
    page_margin: int = 40,
) -> TextSizeTrial:
    """Render the current book and the reset candidate from one verified snapshot."""
    validate_layout(layout, page_margin)
    profiles = profiles if profiles is not None else ["compact"]
    if (
        not profiles
        or len(set(profiles)) != len(profiles)
        or any(p not in PROFILES for p in profiles)
    ):
        raise ValueError("Use compact and/or wide once each.")
    store = Store(config.root)
    with store.locked():
        row, source, digest = current_source(store, book_id, config)
        with tempfile.TemporaryDirectory(dir=config.root / ".staging") as staging:
            current = Path(staging) / "current.epub"
            candidate = Path(staging) / "candidate.epub"
            snapshot(source, current, config.limits.max_archive_mb * 1024**2)
            inspection = inspect_epub(current, config.limits)
            if inspection.source_sha256 != digest or inspection.blocking:
                raise ValueError("Resolve structural errors before comparing text size.")
            edits, notes = discover(current, inspection)
            if not edits:
                raise ValueError(
                    "Body text already follows the reader's size; there is nothing to compare."
                )
            rewrite(current, candidate, inspection, edits)
            media, nav = {}, []
            with zipfile.ZipFile(current) as archive:
                package = parse_xml(archive.read(inspection.package_path))
                if layout == "paginated":
                    check_package(package)
                for item in package.xpath("opf:manifest/opf:item", namespaces=NS):
                    target = local_target(inspection.package_path, item.get("href", ""))
                    if target:
                        media[target[0]] = item.get("media-type", "application/octet-stream")
                        if "nav" in item.get("properties", "").split():
                            nav.append(target[0])
            selected = select_documents(inspection.spine, nav, max_documents)
            if any(media.get(name) != "application/xhtml+xml" for name in selected):
                raise ValueError("Text-size comparisons support XHTML documents only.")
            identifier = uuid.uuid4().hex
            directory = config.root / "text-size" / row["id"] / identifier
            directory.mkdir(parents=True)
            try:
                for phase in ("before", "after"):
                    (directory / phase).mkdir()
                options = {"layout": layout, "page_margin": page_margin}
                version, before = render(
                    current, directory / "before", selected, media, profiles, config, **options
                )
                after_version, after = render(
                    candidate, directory / "after", selected, media, profiles, config, **options
                )
                if after_version != version:
                    raise ValueError("Browser version changed between comparison captures.")
                _, _, latest = current_source(store, row["id"], config)
                if latest != digest or sha256_file(current) != digest:
                    raise ValueError("The EPUB changed during the text-size comparison.")
                expected = {(name, profile) for name in selected for profile in profiles}
                complete = all(
                    len(samples) == len(expected)
                    and {(s.document, s.profile) for s in samples} == expected
                    and all(not s.error and s.screenshots for s in samples)
                    for samples in (before, after)
                )
                result = TextSizeTrial(
                    id=identifier,
                    book_id=row["id"],
                    artifact_sha256=digest,
                    engine_version=ENGINE_VERSION,
                    created_at=now(),
                    browser_version=version,
                    title="; ".join(inspection.metadata.get("title", [])),
                    edits=edits,
                    notes=notes,
                    layout=layout,
                    page_margin=page_margin if layout == "paginated" else None,
                    selected_documents=selected,
                    profiles=profiles,
                    status="complete" if complete else "incomplete",
                    before=before,
                    after=after,
                )
                path = directory / "trial.json"
                path.write_text(result.model_dump_json(indent=2))
                write_trial_html(result, directory)
                store.save_document(
                    identifier, row["id"], "text-size-trial", path, sha256_file(path)
                )
                return result
            except Exception:
                shutil.rmtree(directory)
                raise


def load_text_size_trial(
    store: Store, book_id: str, identifier: str | None = None
) -> TextSizeTrial:
    document = store.document(book_id, "text-size-trial", identifier)
    path = Path(document["path"])
    expected = store.root / "text-size" / book_id / document["id"] / "trial.json"
    if path.resolve() != expected.resolve() or path.stat().st_size > 8 * 1024**2:
        raise ValueError("Invalid text-size comparison location or size.")
    if sha256_file(path) != document["sha256"]:
        raise ValueError("Text-size comparison changed after registration.")
    result = TextSizeTrial.model_validate_json(path.read_text())
    if result.id != document["id"] or result.book_id != book_id:
        raise ValueError("Text-size comparison identity mismatch.")
    for phase in ("before", "after"):
        for sample in getattr(result, phase):
            for shot in sample.screenshots:
                if (
                    Path(shot.file).name != shot.file
                    or sha256_file(path.parent / phase / shot.file) != shot.sha256
                ):
                    raise ValueError("Text-size screenshot hash mismatch.")
    return result


def write_trial_html(trial: TextSizeTrial, directory: Path):
    import base64

    esc = html.escape
    sections = []
    for name in trial.selected_documents:
        for profile in trial.profiles:
            columns = []
            for phase, label in (("before", "Current size"), ("after", "Reader's size")):
                sample = next(
                    (
                        s
                        for s in getattr(trial, phase)
                        if s.document == name and s.profile == profile
                    ),
                    None,
                )
                figures, notes = [], []
                if sample:
                    notes = [*sample.observations, *([sample.error] if sample.error else [])]
                    for shot in sample.screenshots:
                        data = base64.b64encode(
                            (directory / phase / shot.file).read_bytes()
                        ).decode()
                        position = (
                            f"Page {shot.page_number} of {sample.page_count}"
                            if shot.page_number is not None
                            else f"Offset {shot.scroll_y} px"
                        )
                        caption = f"{label} · {position} · {sample.width} × {sample.height}"
                        figures.append(
                            f'<figure><img src="data:image/png;base64,{data}" alt="{esc(name + ": " + caption)}" width="{sample.width}" height="{sample.height}" loading="lazy"><figcaption>{esc(caption)}</figcaption></figure>'
                        )
                if not figures:
                    notes.append("No screenshots available for this sample.")
                columns.append(
                    f'<div class="comparison-side"><h3>{label}</h3><ul>{"".join(f"<li>{esc(n)}</li>" for n in notes)}</ul>{"".join(figures)}</div>'
                )
            sections.append(
                f'<section><h2>{esc(name)} · {esc(profile)}</h2><div class="comparison">{"".join(columns)}</div></section>'
            )
    changes = "".join(
        f"<li><code>{esc(e.selector)} {{ font-size: {esc(e.before)} }}</code> → <code>{esc(e.after)}</code> in <code>{esc(e.stylesheet)}</code></li>"
        for e in trial.edits
    )
    notes = "".join(f"<li>{esc(n)}</li>" for n in trial.notes)
    (
        directory / "trial.html"
    ).write_text(f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:"><title>Emend text-size comparison</title><style>{report_styles()}
.comparison {{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:2rem}} .comparison-side {{min-width:0}}
figure {{margin:0 0 2rem}} figure img {{max-width:100%;height:auto;display:block;border:1px solid var(--rule)}}
figcaption {{font: .75rem/1.6 var(--mono);margin-top:.5rem;color:var(--muted)}} section {{border-bottom:1px solid var(--rule);padding-bottom:2rem}}
h2, p, li {{overflow-wrap:anywhere}}
@media(max-width:650px) {{.comparison {{grid-template-columns:1fr}}}}
</style></head><body><main><header><div class="brand">EMEND / TEXT SIZE</div><h1>{esc(trial.title)}</h1><p>{esc(trial.status.title())} · {esc(trial.layout)} preview</p><code>Artifact {trial.artifact_sha256}</code></header>
<h2>Proposed changes</h2><ul>{changes}</ul><h2>Unchanged</h2><ul>{notes or "<li>Nothing else in the stylesheets changes.</li>"}</ul>
<h2>Scope</h2><p>Both views come from the same verified snapshot. The reader's-size view is rendered from a candidate EPUB in which only the listed font-size values differ. CrossPoint applies a book's relative sizes on top of the size chosen on the device, so resetting these makes body text follow that setting. Browser screenshots are not a device preview.</p><p>No approval, publication or quality state changed.</p>{"".join(sections)}<footer>Generated {esc(trial.created_at)} with Chromium {esc(trial.browser_version)}. Screenshot hashes are verified when reopening the comparison.</footer></main></body></html>""")


def apply_text_size(book_id: str, config: Config, *, trial_id: str, approve: bool):
    if not approve:
        raise ValueError("Inspect the text-size comparison and pass --approve to apply it.")
    store = Store(config.root)
    with store.locked():
        row, source, digest = current_source(store, book_id, config)
        previous = get_report(store, row["id"])
        trial = load_text_size_trial(store, row["id"], trial_id)
        if (
            row["state"] not in {"VALIDATED", "PUBLISHED"}
            or previous.state != "VALIDATED"
            or any(g.required and g.status != "passed" for g in previous.gates)
        ):
            raise ValueError("Text-size changes require a validated current book.")
        if (
            previous.engine_version != ENGINE_VERSION
            or previous.policy_sha256 != config.policy_hash()
        ):
            raise ValueError("Quality policy or engine changed; revalidate the current EPUB first.")
        if trial.status != "complete" or trial.engine_version != ENGINE_VERSION:
            raise ValueError("A complete current-engine text-size comparison is required.")
        if any(
            c.trial_id == trial.id and c.after_sha256 == digest for c in previous.text_size_changes
        ):
            return previous
        if trial.artifact_sha256 != digest:
            raise ValueError("Text-size comparison is stale; compare again for the current EPUB.")
        book_id = row["id"]
        artifact = config.root / "curated" / f"{book_id}.epub"
        if source.resolve() != artifact.resolve():
            raise ValueError("Current artifact is not at its expected library path.")
        with tempfile.TemporaryDirectory(dir=config.root / ".staging") as staging:
            current, candidate = Path(staging) / "current.epub", Path(staging) / "candidate.epub"
            snapshot(source, current, config.limits.max_archive_mb * 1024**2)
            baseline = inspect_epub(current, config.limits)
            if baseline.blocking or baseline.source_sha256 != digest or not baseline.package_path:
                raise ValueError("Current EPUB failed inspection or changed before editing.")
            edits, _ = discover(current, baseline)
            if edits != trial.edits:
                raise ValueError("Current EPUB no longer matches the text-size comparison.")
            directory = config.root / "reports" / book_id / uuid.uuid4().hex
            directory.mkdir(parents=True)
            versions = config.root / "versions" / book_id
            retained = versions / f"{digest}.epub"
            if retained.exists() and sha256_file(retained) != digest:
                raise ValueError("Stored revision hash mismatch.")
            atomic_copy(current, retained)
            replaced = False
            try:
                rewrite(current, candidate, baseline, edits)
                inspection = inspect_epub(candidate, config.limits)
                intact = (
                    not inspection.blocking
                    and preserved(current, candidate, baseline, edits)
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
                        detail="Text-size candidate inspected.",
                    ),
                    Gate(
                        name="preservation",
                        status="passed" if intact else "failed",
                        required=True,
                        detail="Only the approved font-size values and the EPUB 3 modification timestamp may change; reading text, other CSS bytes and unrelated resources must match.",
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
                change = TextSizeChange(
                    trial_id=trial.id,
                    stylesheets=sorted({e.stylesheet for e in edits}),
                    edits=len(edits),
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
                        "text_size_changes": [*previous.text_size_changes, change],
                        "changes": [
                            *previous.changes,
                            f"Reset {len(edits)} body-text font-size values to the reader's size "
                            f"(comparison {trial.id}): "
                            + "; ".join(
                                f"{e.stylesheet} {e.selector}: {e.before} → {e.after}"
                                for e in edits
                            ),
                        ],
                    },
                )
                path = write_reports(report, directory)
                if failures:
                    raise TextSizeRejectedError(
                        f"Text-size candidate rejected; previous artifact retained. Evidence: {path}"
                    )
                version = versions / f"{report.artifact_sha256}.epub"
                if version.exists() and sha256_file(version) != report.artifact_sha256:
                    raise ValueError("Stored candidate revision hash mismatch.")
                atomic_copy(candidate, version)
                replaced = True
                atomic_copy(candidate, artifact)
                if sha256_file(artifact) != report.artifact_sha256:
                    raise ValueError("Activated text-size hash mismatch.")
                store.transition(
                    book_id,
                    row["source_name"],
                    "VALIDATED",
                    f"Applied text-size comparison {trial.id}",
                    path,
                    artifact,
                )
                return report
            except Exception as exc:
                if replaced:
                    atomic_copy(retained, artifact)
                (directory / "failure.txt").write_text(f"{type(exc).__name__}: {exc}\n")
                store.event(book_id, "TEXT_SIZE_REJECTED", f"{exc}; evidence: {directory}")
                raise
