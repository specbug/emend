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
}
SPACING_PRESETS = {
    "compact": (1.35, 0.35),
    "comfortable": (1.55, 0.55),
    "roomy": (1.75, 0.75),
}


ACTION_PANELS = {
    "typography-trial": ("spacing", "spacing"),
    "apply-typography": ("spacing", "spacing"),
    "reading": ("reading", "reading"),
    **{
        action: ("details", "book-details")
        for action in ("research", "discover", "choose-edition", "apply-metadata", "ai-review")
    },
    **{
        action: ("details", "cover-review")
        for action in ("cover-review", "apply-cover", "create-cover", "cover-local")
    },
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
