"""Read-only, bounded explanations for the local book workflow."""

import json
from pathlib import Path

from emend import ENGINE_VERSION

CHECK_NAMES = {
    "structure": "EPUB structure",
    "preservation": "Original and edited files",
    "epubcheck": "EPUB validation",
    "accessibility": "Accessibility",
    "revalidation": "Unfinished checks",
    "recovery": "Unfinished recovery",
    "visual_qa": "Page preview",
}
STATUS_NAMES = {
    "passed": "Passed",
    "failed": "Needs attention",
    "unavailable": "Tool unavailable",
    "skipped": "Not checked",
}
FIX_NAMES = {
    "container.mimetype": "Fix EPUB packaging",
    "navigation.case_paths": "Fix contents links",
    "xhtml.markup": "Fix HTML markup",
}
SPACING_PRESETS = {
    "compact": (1.35, 0.35),
    "comfortable": (1.55, 0.55),
    "roomy": (1.75, 0.75),
}


# The workspace step that shows an action's result or its error.
ACTION_STEPS = {
    "revalidate": "check",
    "visual": "preview",
    "reading": "preview",
    "typography-trial": "refine",
    "apply-typography": "refine",
    "restore-baseline": "refine",
    "text-size-trial": "refine",
    "apply-text-size": "refine",
    "image-conversion-trial": "refine",
    "apply-image-conversion": "refine",
    **{
        action: "refine"
        for action in (
            "research",
            "discover",
            "choose-edition",
            "apply-metadata",
            "ai-review",
            "cover-review",
            "apply-cover",
            "create-cover",
            "cover-local",
        )
    },
    "publish": "send",
    "reader-export": "send",
    "download": "send",
}


def blockers(row, report, digest, config):
    """UI guidance only; every delivery still uses the authoritative core checks."""
    result = [
        (g.name, CHECK_NAMES.get(g.name, g.name), g.detail)
        for g in report.gates
        if g.required and g.status != "passed"
    ]
    if report.engine_version != ENGINE_VERSION or report.policy_sha256 != config.policy_hash():
        result.insert(
            0,
            (
                "outdated",
                "Run checks again",
                "Emend or your check settings changed since this book was checked.",
            ),
        )
    if report.inspection.source_sha256 != digest:
        result.insert(
            0,
            (
                "identity",
                "Check the current file",
                "The saved checks describe a different file. Run checks again.",
            ),
        )
    if not result and (
        row["state"] not in {"VALIDATED", "PUBLISHED"}
        or report.state != "VALIDATED"
        or report.artifact_sha256 != digest
    ):
        result.append(
            ("approval", "Run checks again", "This book has no current approval for sending.")
        )
    return result


def epubcheck_messages(report, reports_root):
    """Never serve arbitrary evidence paths or trust an unbounded validator file."""
    gate = next((g for g in report.gates if g.name == "epubcheck"), None)
    if not gate or not gate.evidence:
        return [], None
    try:
        path = Path(gate.evidence).resolve()
        if not path.is_relative_to(reports_root.resolve()) or path.suffix != ".json":
            return [], "The detailed validator report is unavailable. Run checks again."
        limit = 2 * 1024**2
        if path.stat().st_size > limit:
            return (
                [],
                "The validator report is too large for this page. Its full details are saved with the check report.",
            )
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            return [], "The validator report changed while loading. Run checks again."
        messages = json.loads(raw)["messages"]
        if not isinstance(messages, list):
            raise ValueError("Invalid messages")
        result = []
        total = 0
        for message in messages:
            if not isinstance(message, dict) or str(message.get("severity", "")).upper() not in {
                "ERROR",
                "FATAL",
                "USAGE",
            }:
                continue
            total += 1
            if len(result) == 30:
                continue
            text = str(
                message.get("message")
                or message.get("ID")
                or "The EPUB validator reported a problem."
            )[:2000]
            locations = message.get("locations", [])
            where = []
            if isinstance(locations, list):
                for location in locations[:3]:
                    if isinstance(location, dict):
                        part = str(location.get("path", ""))[:500]
                        if location.get("line") is not None:
                            part += f":{str(location['line'])[:12]}"
                        if part:
                            where.append(part)
            result.append((text, "; ".join(where)))
        return result, f"Showing the first 30 of {total} validator errors." if total > 30 else None
    except (ValueError, OSError, KeyError, TypeError, RecursionError):
        return (
            [],
            "The detailed validator report could not be read. Run checks again to refresh it.",
        )


def other_format_cover(package):
    """(media type, href) when one declared cover image is a format other than JPEG/PNG."""
    from emend.inspect import NS

    items = package.findall("opf:manifest/opf:item", NS)
    ids = {item.get("id") for item in items if "cover-image" in item.get("properties", "").split()}
    ids.update(
        node.get("content")
        for node in package.xpath("opf:metadata/opf:meta[@name='cover']", namespaces=NS)
    )
    matches = [item for item in items if item.get("id") in ids]
    if len(ids) != 1 or len(matches) != 1:
        return None
    media = matches[0].get("media-type", "")
    if not media.startswith("image/") or media in {"image/jpeg", "image/png"}:
        return None
    return media, matches[0].get("href", "")


def cover_observation(epub, inspection):
    """Advisory embedded-cover facts, separate from required publication gates."""
    import io
    import warnings
    import zipfile

    from PIL import Image

    from emend.covers import MAX_BYTES, MAX_PIXELS, declared_cover
    from emend.inspect import NS, parse_xml
    from emend.pagination import fixed_layout

    if inspection.blocking or not inspection.package_path:
        return {
            "title": "Cover check waiting",
            "detail": "Resolve the EPUB structure problems first, then check the embedded cover.",
            "can_add": False,
            "warnings": [],
        }
    try:
        with zipfile.ZipFile(epub) as archive:
            with archive.open(inspection.package_path) as stream:
                package = stream.read(8 * 1024**2 + 1)
            if len(package) > 8 * 1024**2:
                raise ValueError("Package metadata exceeds the cover-check limit.")
            root = parse_xml(package)
            other = other_format_cover(root)
            if other:
                media, href = other
                return {
                    "title": "Cover needs review",
                    "detail": f"{media} · {href}",
                    "can_add": False,
                    "warnings": [
                        "CrossPoint decodes only JPEG, PNG and GIF covers, so the X4 Pro will "
                        "show no cover on its home screen or sleep screen. Apple Books shows it.",
                        "For an eligible raster, compare image conversion under Refine → Images and covers. SVG vectors and skipped rasters stay unchanged.",
                    ],
                }
            declared = declared_cover(epub, inspection.package_path)
            if declared is None:
                hinted = bool(root.xpath("opf:guide/opf:reference[@type='cover']", namespaces=NS))
                return {
                    "title": "No cover image declared in the EPUB",
                    "detail": "Calibre may show a separate library cover that is not embedded in the EPUB. If Calibre already has the right cover, use Polish books → Update the cover in the book files, then save an EPUB copy and import it here. Existing cover-page declarations may need Edit book.",
                    "can_add": not hinted and not fixed_layout(root),
                    "warnings": [],
                }
            path, media = declared
            with archive.open(path) as stream:
                data = stream.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ValueError("The cover exceeds the 20 MiB inspection limit.")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > MAX_PIXELS:
                    raise ValueError("The cover exceeds the 40-megapixel inspection limit.")
                fmt, width, height, mode = image.format, image.width, image.height, image.mode
                orientation = image.getexif().get(274, 1) if fmt == "JPEG" else 1
                image.verify()
        notices = []
        expected = {"JPEG": ("image/jpeg", {".jpg", ".jpeg"}), "PNG": ("image/png", {".png"})}.get(
            fmt
        )
        if not expected or media != expected[0]:
            notices.append(
                "The declared image type does not match a supported JPEG/PNG image. Repair the cover in Calibre’s Edit book."
            )
        elif Path(path).suffix.lower() not in expected[1]:
            notices.append(
                "The image filename does not match its format. CrossPoint 2.0.0-rc chooses its cover decoder by filename extension; correct the resource name and references in Calibre’s Edit book."
            )
        if mode not in {"RGB", "RGBA", "L", "LA", "P"}:
            notices.append(
                "The image uses an unusual colour mode. Reader decoding needs an actual device check."
            )
        if orientation != 1:
            notices.append(
                "The cover relies on EXIF rotation; verify its orientation on the reader."
            )
        return {
            "title": "Cover found in EPUB",
            "detail": f"{fmt} · {width} × {height} pixels · {path}",
            "can_add": False,
            "warnings": notices,
        }
    except (
        ValueError,
        OSError,
        KeyError,
        zipfile.BadZipFile,
        RuntimeError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        return {
            "title": "Cover needs review",
            "detail": str(exc)[:1000],
            "can_add": False,
            "warnings": [],
        }


CROSSPOINT_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif")
IMAGE_NAMES = {
    "image/webp": "WebP",
    "image/avif": "AVIF",
    "image/svg+xml": "SVG",
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/gif": "GIF",
}


def reader_notes(epub, inspection):
    """Bounded source observations, never device-test or approval evidence."""
    import io
    import posixpath
    import warnings
    import zipfile
    from collections import Counter

    from PIL import Image

    from emend.inspect import NS, local_target, parse_xml

    if not inspection.package_path:
        return ["Images can be checked once the package is readable."]
    try:
        with zipfile.ZipFile(epub) as archive:
            if archive.getinfo(inspection.package_path).file_size > 8 * 1024**2:
                return ["Image notes exceed the package inspection limit."]
            package = parse_xml(archive.read(inspection.package_path))
            images = package.xpath(
                "opf:manifest/opf:item[starts-with(@media-type, 'image/')]", namespaces=NS
            )
            if len(images) > 1000:
                return ["Image notes exceed the 1,000-image inspection limit."]
            unsupported = Counter()
            cover_ids = set(
                package.xpath("opf:metadata/opf:meta[@name='cover']/@content", namespaces=NS)
            ) | {n.get("id") for n in images if "cover-image" in n.get("properties", "").split()}
            cover_missing, over, unusual = None, 0, 0
            for item in images:
                target = local_target(inspection.package_path, item.get("href", ""))
                if not target:
                    continue
                path = target[0]
                media = item.get("media-type", "")
                if posixpath.splitext(path)[
                    1
                ].lower() not in CROSSPOINT_IMAGE_EXTENSIONS or media not in {
                    "image/jpeg",
                    "image/png",
                    "image/gif",
                }:
                    unsupported[IMAGE_NAMES.get(media, media)] += 1
                    if item.get("id") in cover_ids:
                        cover_missing = IMAGE_NAMES.get(media, media)
                if media == "image/svg+xml" or archive.getinfo(path).file_size > 20 * 1024**2:
                    continue
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter("error", Image.DecompressionBombWarning)
                        data = archive.read(path)
                        with Image.open(io.BytesIO(data)) as image:
                            over += (
                                image.width * image.height > 8388608
                                or image.width > 2048
                                or image.height > 3072
                                or image.width * image.height > 2048 * 1536
                            )
                            unusual += bool(
                                image.info.get("progressive")
                                or image.info.get("progression")
                                or (image.format == "PNG" and len(data) > 28 and data[28])
                            )
                except (
                    OSError,
                    ValueError,
                    Image.DecompressionBombWarning,
                    Image.DecompressionBombError,
                ):
                    continue
            notes = []
            if unsupported:
                total = sum(unsupported.values())
                kinds = ", ".join(f"{n} {kind}" for kind, n in sorted(unsupported.items()))
                notes.append(
                    f"{total} {'image uses a format' if total == 1 else 'images use formats'} CrossPoint cannot decode ({kinds}). On the X4 Pro they are left out, or replaced by their alt text. Compare explicit image conversion under Refine; unsupported or skipped images stay listed."
                )
            if cover_missing:
                notes.append(
                    f"The cover is a {cover_missing} image. CrossPoint decodes only JPEG, PNG and GIF, so the X4 Pro will show no cover on its home screen or sleep screen."
                )
            if over:
                notes.append(
                    f"{over} images exceed one or more CrossPoint page/cover/sleep source limits. Conversion keeps dimensions unchanged; device confirmation is pending."
                )
            if unusual:
                notes.append(
                    f"{unusual} JPEG/PNG images use progressive or interlaced encoding; CrossPoint may blur or omit them. This conversion targets unsupported formats, not existing JPEG/PNG encoding."
                )
            return notes
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        return ["Image notes could not be completed; inspect the quality report."]
