"""Explicit raster-format conversion with independently checked references/pixels."""

import hashlib
import html
import io
import math
import posixpath
import tempfile
import uuid
import warnings
import zipfile
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from lxml.etree import XMLSyntaxError
from PIL import Image, ImageChops, ImageStat
from pydantic import BaseModel, ConfigDict, Field

from emend import ENGINE_VERSION
from emend.archive import copy_entry
from emend.config import Config
from emend.image_references import css_preserved, css_rewrite, srcset_urls
from emend.inspect import NS, inspect_epub, local_target, parse_xml, sha256_file
from emend.models import ConvertedImage, Gate, ImageConversion, SkippedImage
from emend.navigation_repair import same_xml, serialize
from emend.pagination import check_package, fixed_layout, validate_layout
from emend.pipeline import atomic_copy, get_report
from emend.reports import write_reports
from emend.research.discovery import current_source
from emend.revalidation import snapshot
from emend.storage import Store, now
from emend.validation import validate_ace, validate_epubcheck
from emend.visual import PROFILES, Sample, render, select_documents

SUPPORTED = {".jpg", ".jpeg", ".png", ".gif"}
MEDIA = {"image/jpeg", "image/png", "image/gif"}
MAX_BYTES = 20 * 1024**2
MAX_PIXELS = 40_000_000
MAX_TOTAL_PIXELS = 256_000_000
MAX_IMAGES = 1000
MAX_REFS = 20000
MAX_DOCUMENT = 8 * 1024**2
JPEG_QUALITY = 98
JPEG_PSNR_FLOOR = 40.0


class ImageConversionRejectedError(ValueError):
    """The previous book is retained; rejection evidence is saved."""


class ImageEdit(ConvertedImage):
    old_path: str
    new_path: str
    byte_size: int
    new_byte_size: int
    width: int
    height: int
    mode: str
    alpha: bool
    lossless: bool | None
    psnr: float | None = None
    reference_indices: list[int] = Field(default_factory=list)


class Reference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document: str
    kind: str
    element: int = 0
    attribute: str = ""
    before: str
    after: str
    target: str = ""


class ConversionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    images: list[ImageEdit] = Field(default_factory=list)
    references: list[Reference] = Field(default_factory=list)
    skipped: list[SkippedImage] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ImageConversionTrial(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    book_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    artifact_sha256: str
    candidate_sha256: str
    html_sha256: str = ""
    policy_sha256: str
    engine_version: str
    created_at: str
    browser_version: str
    title: str
    plan: ConversionPlan
    before_bytes: int
    after_bytes: int
    layout: str
    page_margin: int | None = None
    selected_documents: list[str]
    profiles: list[str]
    status: str
    before: list[Sample]
    after: list[Sample]


def image_data(data):
    if len(data) > MAX_BYTES:
        raise ValueError("Image exceeds the 20 MiB conversion limit.")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        image = Image.open(io.BytesIO(data))
        if image.width * image.height > MAX_PIXELS:
            raise ValueError("Image exceeds the 40 MP conversion limit.")
        if getattr(image, "n_frames", 1) != 1:
            raise ValueError("Animated/multiframe image stays unchanged.")
        if image.mode not in {"RGB", "RGBA", "L", "LA", "P", "1"}:
            raise ValueError("Colour mode needs a separate colour-profile conversion.")
        image.load()
        return image


def is_lossless(data, image):
    if image.format == "WEBP":
        offset = 12
        while offset + 8 <= len(data):
            chunk = data[offset : offset + 4]
            if chunk in {b"VP8L", b"VP8 "}:
                return chunk == b"VP8L"
            length = int.from_bytes(data[offset + 4 : offset + 8], "little")
            offset += 8 + length + length % 2
        return None
    return (
        False
        if image.format == "JPEG"
        else True
        if image.format in {"PNG", "BMP", "TIFF", "GIF"}
        else None
    )


def encode(data):
    image = image_data(data)
    alpha = "A" in image.getbands() or "transparency" in image.info
    lossless = is_lossless(data, image)
    line_art = (
        image.mode in {"P", "1", "L", "LA"}
        or image.getcolors(256) is not None
        or image.convert("RGB").entropy() < 5.0
    )
    png = alpha or lossless is not False or line_art
    reason = (
        "alpha"
        if alpha
        else "lossless source"
        if lossless
        else "palette/line art"
        if line_art
        else "lossy photographic source"
    )
    # Keep grayscale source modes/profile associations; never turn colour into gray.
    normalized = (
        image.copy()
        if png and image.mode in {"1", "L", "LA"}
        else image.convert("RGBA" if alpha else "RGB")
    )
    output = io.BytesIO()
    options = {key: image.info[key] for key in ("icc_profile", "exif") if image.info.get(key)}
    if png:
        normalized.save(output, format="PNG", interlace=0, **options)
    else:
        normalized.save(
            output, format="JPEG", quality=JPEG_QUALITY, subsampling=0, progressive=False, **options
        )
    result = output.getvalue()
    check = image_data(result)
    difference = ImageChops.difference(normalized.convert("RGBA"), check.convert("RGBA"))
    mse = sum(ImageStat.Stat(difference.convert("RGB")).sum2) / (3 * image.width * image.height)
    psnr = 10 * math.log10(255**2 / mse) if mse else None
    if (png and any(difference.getextrema()[i] != (0, 0) for i in range(4))) or (
        not png and psnr is not None and psnr < JPEG_PSNR_FLOOR
    ):
        raise ValueError("Encoded image failed its pixel-fidelity bound.")
    return result, image, alpha, lossless, reason, psnr, ".png" if png else ".jpg"


def resolver(document, images):
    paths = {i.old_path: i.new_path for i in images}

    def resolve(value):
        target = local_target(document, value)
        if not target or target[0] not in paths:
            return value
        url = urlsplit(value)
        path = quote(posixpath.relpath(paths[target[0]], posixpath.dirname(document)), safe="/-._~")
        return urlunsplit(("", "", path, url.query, url.fragment))

    return resolve


def rewritten_srcset(value, resolve):
    result = value
    for start, end, url in reversed(srcset_urls(value)):
        result = result[:start] + resolve(url) + result[end:]
    return result


def references(archive, inspection, images):
    refs = []
    package = parse_xml(archive.read(inspection.package_path))
    manifest = {
        local_target(inspection.package_path, n.get("href", ""))[0]: n.get("media-type", "")
        for n in package.xpath("opf:manifest/opf:item", namespaces=NS)
        if local_target(inspection.package_path, n.get("href", ""))
    }
    for info in archive.infolist():
        name, media = info.filename, manifest.get(info.filename, "")
        ext = posixpath.splitext(name)[1].lower()
        if ext in {".js", ".mjs"} or media in {"text/javascript", "application/javascript"}:
            raise ValueError(
                "Scripts cannot be rewritten reliably; the whole conversion is refused."
            )
        xml = media in {
            "application/xhtml+xml",
            "image/svg+xml",
            "application/x-dtbncx+xml",
            "application/oebps-package+xml",
        } or ext in {".xml", ".xhtml", ".html", ".htm", ".svg", ".opf", ".ncx"}
        css = media == "text/css" or ext == ".css"
        if not xml and not css:
            continue
        if info.file_size > MAX_DOCUMENT:
            raise ValueError("A reference document exceeds the conversion limit.")
        data = archive.read(info)
        resolve = resolver(name, images)
        if css:
            text = data.decode("utf-8-sig")
            after, _ = css_rewrite(text, resolve)
            if after != text:
                refs.append(Reference(document=name, kind="css", before=text, after=after))
            continue
        root = parse_xml(data)
        if not same_xml(root, root):
            raise ValueError("A document cannot be compared independently.")
        for index, node in enumerate(root.iter()):
            if not isinstance(node.tag, str):
                continue
            tag = node.tag.rsplit("}", 1)[-1]
            if tag in {"script", "base"} or any(
                k.lower().startswith("on") or k == "{http://www.w3.org/XML/1998/namespace}base"
                for k in node.attrib
            ):
                raise ValueError(
                    "Scripts or custom document bases make references unsafe to rewrite."
                )
            if tag == "style":
                text = node.text or ""
                after, _ = css_rewrite(text, resolve)
                if after != text:
                    raise ValueError(
                        "An embedded style element references a converted image; move it to a stylesheet first."
                    )
            for key, value in node.attrib.items():
                if key in {
                    "id",
                    "class",
                    "alt",
                    "title",
                    "role",
                    "aria-label",
                    "aria-describedby",
                    "lang",
                    "{http://www.w3.org/XML/1998/namespace}lang",
                    "{http://www.w3.org/XML/1998/namespace}id",
                }:
                    continue
                if name == inspection.package_path and (
                    key in {"idref", "toc", "properties", "media-type", "name", "media-overlay"}
                    or (tag == "meta" and node.get("name") == "cover" and key == "content")
                ):
                    continue
                if name == inspection.package_path and tag == "item" and key == "href":
                    continue
                if key == "fallback" and name == inspection.package_path:
                    for image in images:
                        if value == image.manifest_id:
                            refs.append(
                                Reference(
                                    document=name,
                                    kind="fallback",
                                    element=index,
                                    attribute=key,
                                    before=value,
                                    after=value,
                                    target=image.old_path,
                                )
                            )
                    continue
                if key == "style":
                    after, _ = css_rewrite(value, resolve)
                    kind = "style"
                elif key == "srcset":
                    after = rewritten_srcset(value, resolve)
                    kind = "srcset"
                else:
                    after, kind = resolve(value), "attribute"
                    if after != value and (
                        key
                        not in {
                            "src",
                            "href",
                            "poster",
                            "data",
                            "{http://www.w3.org/1999/xlink}href",
                        }
                        or name == inspection.package_path
                    ):
                        raise ValueError(
                            "An image reference is in an unsupported attribute; the whole conversion is refused."
                        )
                if after != value:
                    refs.append(
                        Reference(
                            document=name,
                            kind=kind,
                            element=index,
                            attribute=key,
                            before=value,
                            after=after,
                        )
                    )
        if len(refs) > MAX_REFS:
            raise ValueError("Too many image references for a complete checked conversion.")
    if len(refs) > MAX_REFS:
        raise ValueError("Too many image references for a complete checked conversion.")
    return refs


def discover(source, inspection):
    plan = ConversionPlan()
    if not inspection.package_path or any(
        f.severity == "error" and f.code != "image.invalid" for f in inspection.findings
    ):
        raise ValueError("Resolve EPUB structure/script/reference errors before image conversion.")
    with zipfile.ZipFile(source) as archive:
        package = parse_xml(archive.read(inspection.package_path))
        if fixed_layout(package):
            raise ValueError("Fixed-layout books are not converted.")
        names = {name.casefold() for name in archive.namelist()}
        pixels = 0
        items = package.xpath(
            "opf:manifest/opf:item[starts-with(@media-type, 'image/')]", namespaces=NS
        )
        if len(items) > MAX_IMAGES:
            raise ValueError("Manifest exceeds the 1,000-image conversion limit.")
        for item in items:
            href, media, identifier = (
                item.get("href", ""),
                item.get("media-type", ""),
                item.get("id", ""),
            )
            path = local_target(inspection.package_path, href)[0]
            if posixpath.splitext(path)[1].lower() in SUPPORTED and media in MEDIA:
                continue
            try:
                if media == "image/svg+xml" or path.lower().endswith(".svg"):
                    raise ValueError("SVG remains vector; rasterising is outside this conversion.")
                if archive.getinfo(path).file_size > MAX_BYTES:
                    raise ValueError("Image exceeds the 20 MiB conversion limit.")
                data = archive.read(path)
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(io.BytesIO(data)) as header:
                        if pixels + header.width * header.height > MAX_TOTAL_PIXELS:
                            raise ValueError("Total converted pixels exceed the 256 MP plan limit.")
                result, image, alpha, lossless, reason, psnr, extension = encode(data)
                base = posixpath.splitext(path)[0]
                new_path, index = base + extension, 1
                while new_path.casefold() in names:
                    new_path = f"{base}.emend-{index}{extension}"
                    index += 1
                url = urlsplit(href)
                raw_stem, raw_extension = posixpath.splitext(url.path)
                if (
                    url.query
                    or url.fragment
                    or not raw_extension
                    or posixpath.splitext(unquote(url.path))[0] != unquote(raw_stem)
                ):
                    raise ValueError(
                        "Manifest image URI has an ambiguous extension, query or fragment."
                    )
                new_href = raw_stem + new_path[len(base) :]
                plan.images.append(
                    ImageEdit(
                        manifest_id=identifier,
                        old_href=href,
                        new_href=new_href,
                        old_media_type=media,
                        new_media_type="image/png" if extension == ".png" else "image/jpeg",
                        old_sha256=hashlib.sha256(data).hexdigest(),
                        new_sha256=hashlib.sha256(result).hexdigest(),
                        reason=reason,
                        old_path=path,
                        new_path=new_path,
                        byte_size=len(data),
                        new_byte_size=len(result),
                        width=image.width,
                        height=image.height,
                        mode=image.mode,
                        alpha=alpha,
                        lossless=lossless,
                        psnr=psnr,
                    )
                )
                names.add(new_path.casefold())
                pixels += image.width * image.height
                if (
                    image.width * image.height > 8388608
                    or image.width > 2048
                    or image.height > 3072
                    or (extension == ".png" and image.width * image.height > 2048 * 1536)
                ):
                    plan.notes.append(
                        f"{href}: dimensions stay {image.width} × {image.height}; above one or more CrossPoint source limits. No resizing is done."
                    )
            except (
                OSError,
                ValueError,
                Image.DecompressionBombError,
                Image.DecompressionBombWarning,
            ) as exc:
                plan.skipped.append(
                    SkippedImage(manifest_id=identifier, href=href, reason=str(exc)[:500])
                )
        if plan.images:
            try:
                plan.references = references(archive, inspection, plan.images)
            except XMLSyntaxError as exc:
                raise ValueError(
                    "A reference document cannot be parsed; the whole conversion is refused."
                ) from exc
            by_path = {image.old_path: image for image in plan.images}
            for index, ref in enumerate(plan.references):
                if ref.kind == "fallback":
                    targets = {ref.target}
                else:
                    if ref.kind in {"css", "style"}:
                        from emend.image_references import css_nodes

                        values = [value for _, value in css_nodes(ref.before)[1]]
                    elif ref.kind == "srcset":
                        values = [value for _, _, value in srcset_urls(ref.before)]
                    else:
                        values = [ref.before]
                    targets = {
                        target[0]
                        for value in values
                        if (target := local_target(ref.document, value))
                    }
                for target in targets & by_path.keys():
                    by_path[target].reference_indices.append(index)
    return plan


def rewrite(source, candidate, inspection, plan):
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(candidate, "w") as new:
        replacements, renamed = {}, {}
        for image in plan.images:
            result, *_ = encode(old.read(image.old_path))
            if hashlib.sha256(result).hexdigest() != image.new_sha256:
                raise ValueError("Image encoder no longer matches the reviewed candidate.")
            replacements[image.old_path], renamed[image.old_path] = result, image.new_path
        trees = {}
        for ref in plan.references:
            if ref.kind == "fallback":
                continue
            if ref.kind == "css":
                original = old.read(ref.document)
                prefix = b"\xef\xbb\xbf" if original.startswith(b"\xef\xbb\xbf") else b""
                replacements[ref.document] = prefix + ref.after.encode("utf-8")
            else:
                if ref.document not in trees:
                    trees[ref.document] = parse_xml(old.read(ref.document))
                node = list(trees[ref.document].iter())[ref.element]
                if node.get(ref.attribute) != ref.before:
                    raise ValueError("Image reference changed after discovery.")
                node.set(ref.attribute, ref.after)
        package = parse_xml(old.read(inspection.package_path))
        for image in plan.images:
            items = package.xpath(
                "opf:manifest/opf:item[@id=$id]", namespaces=NS, id=image.manifest_id
            )
            if len(items) != 1:
                raise ValueError("Manifest image identity is ambiguous.")
            items[0].set("href", image.new_href)
            items[0].set("media-type", image.new_media_type)
        trees[inspection.package_path] = package
        replacements.update({name: serialize(tree) for name, tree in trees.items()})
        new.comment = old.comment
        for info in old.infolist():
            copy_entry(
                new,
                info,
                replacements.get(info.filename, old.read(info)),
                renamed.get(info.filename, info.filename),
            )


def preserved(source, candidate, inspection, plan):
    """Re-read both archives; mask only approved manifest/reference values."""
    images = {image.old_path: image for image in plan.images}
    by_document = {}
    for ref in plan.references:
        by_document.setdefault(ref.document, []).append(ref)
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(candidate) as new:
        if (
            new.namelist() != [images[n].new_path if n in images else n for n in old.namelist()]
            or old.comment != new.comment
        ):
            return False
        for info in old.infolist():
            image = images.get(info.filename)
            other = new.getinfo(image.new_path if image else info.filename)
            if any(
                getattr(info, k) != getattr(other, k)
                for k in ("compress_type", "date_time", "extra", "comment", "external_attr")
            ):
                return False
            a, b = old.read(info), new.read(other)
            if image:
                if (
                    hashlib.sha256(a).hexdigest() != image.old_sha256
                    or hashlib.sha256(b).hexdigest() != image.new_sha256
                ):
                    return False
                left, right = image_data(a), image_data(b)
                if right.format != ("PNG" if image.new_media_type == "image/png" else "JPEG"):
                    return False
                if left.size != right.size or left.info.get("icc_profile") != right.info.get(
                    "icc_profile"
                ):
                    return False
                if right.format == "PNG" and (b[:8] != b"\x89PNG\r\n\x1a\n" or b[28] != 0):
                    return False
                if right.format == "JPEG" and (
                    right.info.get("progressive") or right.info.get("progression")
                ):
                    return False
                diff = ImageChops.difference(left.convert("RGBA"), right.convert("RGBA"))
                mse = sum(ImageStat.Stat(diff.convert("RGB")).sum2) / (3 * left.width * left.height)
                if image.new_media_type == "image/png":
                    if any(x != (0, 0) for x in diff.getextrema()):
                        return False
                elif mse and 10 * math.log10(255**2 / mse) < JPEG_PSNR_FLOOR:
                    return False
                continue
            refs = by_document.get(info.filename, [])
            if info.filename != inspection.package_path and not refs:
                if a != b:
                    return False
                continue
            resolve = resolver(info.filename, plan.images)
            if any(r.kind == "css" for r in refs):
                prefix = b"\xef\xbb\xbf"
                if a.startswith(prefix) != b.startswith(prefix) or not css_preserved(
                    a.decode("utf-8-sig"), b.decode("utf-8-sig"), resolve
                ):
                    return False
                continue
            left, right = parse_xml(a), parse_xml(b)
            x, y = list(left.iter()), list(right.iter())
            if len(x) != len(y):
                return False
            for ref in refs:
                if (
                    x[ref.element].get(ref.attribute) != ref.before
                    or y[ref.element].get(ref.attribute) != ref.after
                ):
                    return False
                if ref.kind == "style" and not css_preserved(
                    ref.before, y[ref.element].get(ref.attribute), resolve
                ):
                    return False
                y[ref.element].set(ref.attribute, ref.before)
            if info.filename == inspection.package_path:
                for edit in plan.images:
                    matches = right.xpath(
                        "opf:manifest/opf:item[@id=$id]", namespaces=NS, id=edit.manifest_id
                    )
                    if (
                        len(matches) != 1
                        or matches[0].get("href") != edit.new_href
                        or matches[0].get("media-type") != edit.new_media_type
                    ):
                        return False
                    matches[0].set("href", edit.old_href)
                    matches[0].set("media-type", edit.old_media_type)
            if not same_xml(left, right):
                return False
    return True


def summary(plan):
    png = sum(i.new_media_type == "image/png" for i in plan.images)
    return f"Convert {len(plan.images)} images: {png} to PNG, {len(plan.images) - png} to JPEG; {len(plan.skipped)} skipped"


def image_conversion_trial(
    book_id, config: Config, *, profiles=None, max_documents=5, layout="paginated", page_margin=40
):
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
        identifier = uuid.uuid4().hex
        directory = config.root / "image-conversion" / row["id"] / identifier
        directory.mkdir(parents=True)
        try:
            with tempfile.TemporaryDirectory(dir=config.root / ".staging") as staging:
                current, candidate = (
                    Path(staging) / "current.epub",
                    Path(staging) / "candidate.epub",
                )
                snapshot(source, current, config.limits.max_archive_mb * 1024**2)
                inspection = inspect_epub(current, config.limits)
                if inspection.source_sha256 != digest:
                    raise ValueError("Current EPUB changed before image discovery.")
                plan = discover(current, inspection)
                if not plan.images:
                    reasons = "; ".join(s.reason for s in plan.skipped[:5])
                    raise ValueError("No eligible images to convert. " + reasons)
                if len(plan.model_dump_json().encode()) > 16 * 1024**2:
                    raise ValueError("Image conversion plan exceeds the 16 MiB review limit.")
                rewrite(current, candidate, inspection, plan)
                candidate_inspection = inspect_epub(candidate, config.limits)
                if candidate_inspection.blocking or not preserved(
                    current, candidate, inspection, plan
                ):
                    raise ImageConversionRejectedError(
                        "Image candidate failed inspection or independent preservation."
                    )
                candidate_sha = sha256_file(candidate)
                media = {}
                with zipfile.ZipFile(current) as archive:
                    package = parse_xml(archive.read(inspection.package_path))
                    if layout == "paginated":
                        check_package(package)
                    for item in package.xpath("opf:manifest/opf:item", namespaces=NS):
                        target = local_target(inspection.package_path, item.get("href", ""))
                        if target:
                            media[target[0]] = item.get("media-type", "application/octet-stream")
                after_media = dict(media)
                for image in plan.images:
                    after_media.pop(image.old_path, None)
                    after_media[image.new_path] = image.new_media_type
                # Sample referenced spine pages as well as the beginning/middle/end.
                image_pages = [
                    r.document for r in plan.references if r.document in inspection.spine
                ]
                selected = select_documents(inspection.spine, image_pages, max_documents)
                phases = []
                browser_version = None
                for phase, epub, types in [
                    ("before", current, media),
                    ("after", candidate, after_media),
                ]:
                    (directory / phase).mkdir()
                    version, samples = render(
                        epub,
                        directory / phase,
                        selected,
                        types,
                        profiles,
                        config,
                        layout=layout,
                        page_margin=page_margin,
                    )
                    if browser_version is not None and version != browser_version:
                        raise ValueError("Browser version changed between image captures.")
                    browser_version = version
                    phases.append(samples)
                _, _, latest = current_source(store, row["id"], config)
                if (
                    latest != digest
                    or sha256_file(current) != digest
                    or sha256_file(candidate) != candidate_sha
                ):
                    raise ValueError("An EPUB changed during image comparison.")
                expected = {(n, p) for n in selected for p in profiles}
                complete = all(
                    len(samples) == len(expected)
                    and {(s.document, s.profile) for s in samples} == expected
                    and all(not s.error and s.screenshots for s in samples)
                    for samples in phases
                )
                result = ImageConversionTrial(
                    id=identifier,
                    book_id=row["id"],
                    artifact_sha256=digest,
                    candidate_sha256=candidate_sha,
                    policy_sha256=config.policy_hash(),
                    engine_version=ENGINE_VERSION,
                    created_at=now(),
                    browser_version=browser_version,
                    title="; ".join(inspection.metadata.get("title", [])),
                    plan=plan,
                    before_bytes=current.stat().st_size,
                    after_bytes=candidate.stat().st_size,
                    layout=layout,
                    page_margin=page_margin if layout == "paginated" else None,
                    selected_documents=selected,
                    profiles=profiles,
                    status="complete" if complete else "incomplete",
                    before=phases[0],
                    after=phases[1],
                )
                path = directory / "trial.json"
                write_trial_html(result, directory)
                result.html_sha256 = sha256_file(directory / "trial.html")
                path.write_text(result.model_dump_json(indent=2))
                store.save_document(
                    identifier, row["id"], "image-conversion-trial", path, sha256_file(path)
                )
                return result
        except Exception as exc:
            (directory / "failure.txt").write_text(f"{type(exc).__name__}: {exc}\n")
            raise


def load_image_conversion_trial(store, book_id, identifier=None):
    document = store.document(book_id, "image-conversion-trial", identifier)
    path = Path(document["path"])
    expected = store.root / "image-conversion" / book_id / document["id"] / "trial.json"
    if (
        path.resolve() != expected.resolve()
        or path.stat().st_size > 32 * 1024**2
        or sha256_file(path) != document["sha256"]
    ):
        raise ValueError("Image comparison location, size or registered hash is invalid.")
    result = ImageConversionTrial.model_validate_json(path.read_text())
    if result.id != document["id"] or result.book_id != book_id:
        raise ValueError("Image comparison identity mismatch.")
    if not result.html_sha256 or sha256_file(path.with_suffix(".html")) != result.html_sha256:
        raise ValueError("Image comparison report hash mismatch; compare again.")
    for phase in ("before", "after"):
        for sample in getattr(result, phase):
            for shot in sample.screenshots:
                if (
                    Path(shot.file).name != shot.file
                    or sha256_file(path.parent / phase / shot.file) != shot.sha256
                ):
                    raise ValueError("Image comparison screenshot hash mismatch.")
    return result


def write_trial_html(trial, directory):
    import base64

    esc = html.escape
    parts = [
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Image conversion comparison</title></head><body><h1>{esc(trial.title)}</h1><p>{esc(summary(trial.plan))}</p>',
        f"<p>Book size: {trial.before_bytes:,} → {trial.after_bytes:,} bytes. No book changes until approved.</p>",
    ]
    for image in trial.plan.images:
        parts.append(
            f"<p>{esc(image.old_href)} → {esc(image.new_href)}: {esc(image.reason)}; {image.width} × {image.height}.</p>"
        )
    for skipped in trial.plan.skipped:
        parts.append(f"<p>Skipped {esc(skipped.href)}: {esc(skipped.reason)}</p>")
    for phase, title in [("before", "Baseline"), ("after", "Converted")]:
        parts.append(f"<h2>{title}</h2>")
        for sample in getattr(trial, phase):
            for shot in sample.screenshots:
                data = base64.b64encode((directory / phase / shot.file).read_bytes()).decode()
                parts.append(
                    f'<figure><img src="data:image/png;base64,{data}" width="300" height="{round(sample.height * 300 / sample.width)}" alt="{esc(sample.document)}"><figcaption>{esc(sample.document)}</figcaption></figure>'
                )
    (directory / "trial.html").write_text("".join(parts) + "</body></html>")


def apply_image_conversion(book_id, config: Config, *, trial_id, approve):
    if not approve:
        raise ValueError("Review the image comparison and pass --approve to apply it.")
    store = Store(config.root)
    with store.locked():
        row, source, digest = current_source(store, book_id, config)
        previous = get_report(store, row["id"])
        trial = load_image_conversion_trial(store, row["id"], trial_id)
        if any(
            g.required and g.status != "passed" and g.name in {"recovery", "revalidation"}
            for g in previous.gates
        ):
            raise ValueError("Finish pending recovery/revalidation before image conversion.")
        if (
            previous.engine_version != ENGINE_VERSION
            or previous.policy_sha256 != config.policy_hash()
        ):
            raise ValueError("Revalidate the current EPUB after policy or engine changes.")
        if (
            trial.status != "complete"
            or trial.engine_version != ENGINE_VERSION
            or trial.policy_sha256 != config.policy_hash()
        ):
            raise ValueError(
                "A complete current-engine/current-policy image comparison is required."
            )
        if any(
            c.trial_id == trial.id and c.after_sha256 == digest for c in previous.image_conversions
        ):
            if (
                row["state"] not in {"VALIDATED", "PUBLISHED"}
                or previous.state != "VALIDATED"
                or any(g.required and g.status != "passed" for g in previous.gates)
            ):
                raise ValueError(
                    "Revalidate failed or pending checks before retrying this applied conversion."
                )
            return previous
        if trial.artifact_sha256 != digest:
            raise ValueError("Image comparison is stale; compare the current EPUB again.")
        if (
            previous.engine_version != ENGINE_VERSION
            or previous.policy_sha256 != config.policy_hash()
        ):
            raise ValueError("Revalidate the current EPUB after policy or engine changes.")
        book_id = row["id"]
        artifact = config.root / "curated" / f"{book_id}.epub"
        if row["artifact_path"] and source.resolve() != artifact.resolve():
            raise ValueError("Current artifact is not at its expected path.")
        directory = config.root / "reports" / book_id / uuid.uuid4().hex
        directory.mkdir(parents=True)
        with tempfile.TemporaryDirectory(dir=config.root / ".staging") as staging:
            current, candidate = Path(staging) / "current.epub", Path(staging) / "candidate.epub"
            snapshot(source, current, config.limits.max_archive_mb * 1024**2)
            baseline = inspect_epub(current, config.limits)
            if baseline.source_sha256 != digest:
                raise ValueError("Current EPUB changed before conversion.")
            plan = discover(current, baseline)
            if plan != trial.plan:
                raise ValueError(
                    "Current images/references no longer match the reviewed conversion."
                )
            versions = config.root / "versions" / book_id
            retained = versions / f"{digest}.epub"
            if retained.exists() and sha256_file(retained) != digest:
                raise ValueError("Stored revision hash mismatch.")
            atomic_copy(current, retained)
            replaced = False
            try:
                rewrite(current, candidate, baseline, plan)
                inspection = inspect_epub(candidate, config.limits)
                intact = (
                    not inspection.blocking
                    and preserved(current, candidate, baseline, plan)
                    and sha256_file(candidate) == trial.candidate_sha256
                    and baseline.metadata == inspection.metadata
                    and baseline.spine == inspection.spine
                    and baseline.text_sha256 == inspection.text_sha256
                    and baseline.spine_text_sha256 == inspection.spine_text_sha256
                )
                gates = [
                    Gate(
                        name="structure",
                        status="failed" if inspection.blocking else "passed",
                        required=True,
                        detail="Image conversion inspected.",
                    ),
                    Gate(
                        name="preservation",
                        status="passed" if intact else "failed",
                        required=True,
                        detail="Only approved raster pixels, resource names, references and manifest href/media-type values may change. Unrelated bytes, reading text, metadata and spine are preserved.",
                    ),
                ]
                if intact:
                    epubcheck = validate_epubcheck(candidate, directory, config)
                    epubcheck.required = True
                    gates += [epubcheck, validate_ace(candidate, directory, config)]
                else:
                    gates += [
                        Gate(
                            name=n,
                            status="skipped",
                            required=r,
                            detail="Candidate failed independent preservation.",
                        )
                        for n, r in [
                            ("epubcheck", True),
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
                    gates[1].status = "failed"
                    gates[1].detail = "An input changed during validation."
                failures = [g for g in gates if g.required and g.status != "passed"]
                report = previous.model_copy(
                    deep=True,
                    update={
                        "created_at": now(),
                        "state": "FAILED" if failures else "VALIDATED",
                        "inspection": inspection,
                        "gates": gates,
                        "revalidation": False,
                        "artifact_sha256": inspection.source_sha256 if not failures else None,
                        "parent_artifact_sha256": digest,
                        "parent_report_path": row["report_path"],
                        "image_conversions": [
                            *previous.image_conversions,
                            ImageConversion(
                                trial_id=trial.id,
                                images=[
                                    ConvertedImage(
                                        **i.model_dump(include=set(ConvertedImage.model_fields))
                                    )
                                    for i in plan.images
                                ],
                                skipped=plan.skipped,
                                before_sha256=digest,
                                after_sha256=inspection.source_sha256,
                            ),
                        ],
                        "changes": [
                            *previous.changes,
                            summary(plan) + f" (comparison {trial.id}).",
                        ],
                    },
                )
                path = write_reports(report, directory)
                if failures:
                    raise ImageConversionRejectedError(
                        f"Image conversion rejected; previous book retained. Evidence: {path}"
                    )
                version = versions / f"{report.artifact_sha256}.epub"
                if version.exists() and sha256_file(version) != report.artifact_sha256:
                    raise ValueError("Stored candidate revision hash mismatch.")
                atomic_copy(candidate, version)
                replaced = True
                atomic_copy(candidate, artifact)
                if sha256_file(artifact) != report.artifact_sha256:
                    raise ValueError("Activated image-conversion hash mismatch.")
                store.transition(
                    book_id,
                    row["source_name"],
                    "VALIDATED",
                    f"Applied image comparison {trial.id}",
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
                store.event(book_id, "IMAGE_CONVERSION_REJECTED", f"{exc}; evidence: {directory}")
                if isinstance(exc, ImageConversionRejectedError):
                    raise
                raise ImageConversionRejectedError(
                    f"Image conversion failed; previous book retained. Evidence: {directory}"
                ) from exc
