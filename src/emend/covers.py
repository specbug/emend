"""Review cover provenance and pixels before an explicit, bounded replacement."""

import base64
import hashlib
import html
import io
import re
import urllib.error
import urllib.request
import uuid
import warnings
import zipfile
from pathlib import Path

from PIL import Image, ImageOps
from pydantic import BaseModel, ConfigDict, Field

from emend import ENGINE_VERSION
from emend.config import Config
from emend.inspect import NS, inspect_epub, local_target, parse_xml, sha256_file
from emend.reports import report_styles
from emend.research.discovery import current_source
from emend.research.service import load_plan
from emend.storage import Store, now

MAX_BYTES = 20 * 1024**2
MAX_PIXELS = 40_000_000
FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png"}


class CoverAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    sha256: str
    path: str
    source: str
    source_url: str | None = None
    record_id: str | None = None
    media_type: str
    width: int
    height: int
    byte_count: int
    dhash: str
    mode: str
    orientation: int
    warnings: list[str] = Field(default_factory=list)
    edition_details: dict[str, list[str]] = Field(default_factory=dict)


class CoverReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    book_id: str
    artifact_sha256: str
    engine_version: str
    policy_sha256: str
    created_at: str
    plan_id: str | None
    plan_sha256: str | None
    cover_path: str | None
    original: CoverAsset | None
    candidates: list[CoverAsset]
    errors: list[str]
    metadata: dict[str, list[str]] = Field(default_factory=dict)
    notes: list[str] = Field(
        default_factory=lambda: [
            "Artwork and edition require your visual review. Dimensions and perceptual hashes cannot certify a match.",
            "Previews are scaled for display; stored replacement bytes are never upscaled, resized, or transcoded.",
            "Applying a cover requires one existing declared JPEG/PNG cover and a candidate in the same format.",
            "A local candidate has user-supplied provenance, not a verified publisher source.",
        ]
    )


def declared_cover(epub: Path, package: str) -> tuple[str, str] | None:
    with zipfile.ZipFile(epub) as archive:
        root = parse_xml(archive.read(package))
    items = root.findall("opf:manifest/opf:item", NS)
    ids = {item.get("id") for item in items if "cover-image" in item.get("properties", "").split()}
    ids.update(
        node.get("content")
        for node in root.xpath("opf:metadata/opf:meta[@name='cover']", namespaces=NS)
    )
    if not ids:
        return None
    if None in ids or len(ids) != 1:
        raise ValueError(
            "Cover declarations are missing an ID or disagree; manual repair is required."
        )
    matches = [item for item in items if item.get("id") in ids]
    if len(matches) != 1 or matches[0].get("media-type") not in FORMATS.values():
        raise ValueError("Cover must resolve to one declared JPEG or PNG image.")
    target = local_target(package, matches[0].get("href", ""))
    if target is None or target[1]:
        raise ValueError("Cover must be a local image without a fragment.")
    return target[0], matches[0].get("media-type")


def read_image(path: Path) -> bytes:
    with path.open("rb") as source:
        data = source.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Cover exceeds the 20 MiB size limit.")
    return data


def image_details(data: bytes) -> dict:
    if len(data) > MAX_BYTES:
        raise ValueError("Cover exceeds the 20 MiB size limit.")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            with Image.open(io.BytesIO(data)) as image:
                image_format, size = image.format, image.size
                if image_format not in FORMATS or size[0] * size[1] > MAX_PIXELS or min(size) < 2:
                    raise ValueError(
                        "Use a JPEG or PNG cover between 2 pixels per side and 40 megapixels."
                    )
                if getattr(image, "n_frames", 1) != 1:
                    raise ValueError("Animated covers are unsupported.")
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                mode = image.mode
                orientation = image.getexif().get(274, 1)
                oriented = ImageOps.exif_transpose(image)
                width, height = oriented.size
                gray = oriented.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
                pixels = list(gray.tobytes())
                bits = sum(
                    (pixels[y * 9 + x] > pixels[y * 9 + x + 1]) << (y * 8 + x)
                    for y in range(8)
                    for x in range(8)
                )
                notices = []
                if min(width, height) < 600:
                    notices.append("Short edge below 600 px; inspect readability at device size.")
                if width >= height:
                    notices.append(
                        "Square or landscape artwork; check the existing cover-page layout."
                    )
                if orientation != 1:
                    notices.append(
                        "EXIF orientation requires rotation; replacement is blocked until prepared explicitly."
                    )
                if mode not in {"RGB", "RGBA", "L", "LA", "P"}:
                    notices.append(
                        "Unusual colour mode; replacement is blocked until prepared explicitly."
                    )
                return dict(
                    media_type=FORMATS[image_format],
                    width=width,
                    height=height,
                    byte_count=len(data),
                    dhash=f"{bits:016x}",
                    mode=mode,
                    orientation=orientation,
                    warnings=notices,
                )
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            OSError,
            SyntaxError,
        ) as exc:
            raise ValueError("Cover image is damaged, oversized, or unsupported.") from exc


def store_asset(
    data: bytes,
    directory: Path,
    *,
    source: str,
    source_url: str | None = None,
    record_id: str | None = None,
    edition_details: dict[str, list[str]] | None = None,
) -> CoverAsset:
    details = image_details(data)
    digest = hashlib.sha256(data).hexdigest()
    extension = ".jpg" if details["media_type"] == "image/jpeg" else ".png"
    path = directory / (digest + extension)
    if path.exists() and sha256_file(path) != digest:
        raise ValueError("Stored cover asset hash mismatch.")
    if not path.exists():
        with path.open("xb") as out:
            out.write(data)
        path.chmod(0o444)
    return CoverAsset(
        id=hashlib.sha256(
            (digest + (source_url or source) + (record_id or "")).encode()
        ).hexdigest()[:16],
        sha256=digest,
        path=str(path),
        source=source,
        source_url=source_url,
        record_id=record_id,
        edition_details=edition_details or {},
        **details,
    )


def checked_cover_url(url: str):
    if not re.fullmatch(
        r"https://covers\.openlibrary\.org/b/id/[1-9][0-9]*-L\.jpg\?default=false", url
    ):
        raise ValueError("Only recorded Open Library cover-image URLs are supported.")


class CoverRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        checked_cover_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_cover(url: str, timeout: int) -> bytes:
    checked_cover_url(url)
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "Emend/0.3 cover review", "Accept": "image/jpeg,image/png"}
        )
        with urllib.request.build_opener(CoverRedirect()).open(
            request, timeout=timeout
        ) as response:
            checked_cover_url(response.geturl())
            data = response.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("Cover download exceeds 20 MiB.")
        return data
    except urllib.error.HTTPError as exc:
        raise ValueError(f"Cover provider returned HTTP {exc.code}.") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ValueError("Cover download failed or timed out.") from None


def cover_review(
    book_id: str,
    config: Config,
    *,
    plan_id: str | None = None,
    image: Path | None = None,
    source_note: str | None = None,
) -> CoverReview:
    store = Store(config.root)
    with store.locked():
        row, epub, digest = current_source(store, book_id, config)
        book_id = row["id"]
        inspection = inspect_epub(epub, config.limits)
        if inspection.blocking or not inspection.package_path:
            raise ValueError(
                "Book inspection failed; resolve structural issues before cover review."
            )
        identifier = uuid.uuid4().hex
        directory = config.root / "covers" / book_id / identifier
        directory.mkdir(parents=True)
        current, cover_path, candidates, errors, plan_digest = None, None, [], [], None
        try:
            declaration = declared_cover(epub, inspection.package_path)
            if declaration:
                cover_path, media_type = declaration
                with zipfile.ZipFile(epub) as archive:
                    if archive.getinfo(cover_path).file_size > MAX_BYTES:
                        raise ValueError("Current cover exceeds review size limits.")
                    current = store_asset(
                        archive.read(cover_path), directory, source="Current EPUB cover"
                    )
                if current.media_type != media_type:
                    raise ValueError("Current cover bytes disagree with their declared format.")
        except ValueError as exc:
            errors.append(str(exc))
            current, cover_path = None, None
        if image is not None:
            if plan_id:
                raise ValueError(
                    "Choose either a local image or a research plan for one cover review."
                )
            if source_note and (len(source_note) > 1000 or any(ord(c) < 32 for c in source_note)):
                raise ValueError("Source note must be plain text up to 1000 characters.")
            candidates.append(
                store_asset(
                    read_image(image.expanduser()),
                    directory,
                    source=source_note or f"User-supplied file: {image.name}",
                )
            )
        else:
            if source_note:
                raise ValueError("--source-note applies only with --image.")
            plan = load_plan(store, book_id, plan_id)
            if (
                plan.artifact_sha256 != digest
                or plan.engine_version != ENGINE_VERSION
                or plan.policy_sha256 != config.metadata.fingerprint()
            ):
                raise ValueError("Research plan is stale; research the current artifact again.")
            plan_id, plan_digest = plan.id, store.plan(book_id, plan.id)["sha256"]
            seen = set()
            for edition in plan.editions:
                for candidate in edition.covers:
                    if candidate.url in seen or len(seen) >= 8:
                        continue
                    seen.add(candidate.url)
                    try:
                        data = download_cover(
                            candidate.url, config.metadata.request_timeout_seconds
                        )
                        candidates.append(
                            store_asset(
                                data,
                                directory,
                                source=edition.source_url,
                                source_url=candidate.url,
                                record_id=edition.record_id,
                                edition_details={
                                    "title": [edition.title],
                                    "creator": edition.creators,
                                    "publisher": edition.publisher,
                                    "date": edition.date,
                                    "language": edition.language,
                                    "isbn": [edition.isbn] if edition.isbn else [],
                                },
                            )
                        )
                    except ValueError as exc:
                        errors.append(f"{candidate.url}: {exc}")
        # Present compatible, larger assets first; this ordering is not an edition-match score.
        candidates.sort(
            key=lambda c: (
                current is None or c.media_type == current.media_type,
                c.width * c.height,
            ),
            reverse=True,
        )
        result = CoverReview(
            id=identifier,
            book_id=book_id,
            artifact_sha256=digest,
            engine_version=ENGINE_VERSION,
            policy_sha256=config.policy_hash(),
            created_at=now(),
            plan_id=plan_id,
            plan_sha256=plan_digest,
            cover_path=cover_path,
            original=current,
            candidates=candidates,
            errors=errors,
            metadata=inspection.metadata,
        )
        path = directory / "review.json"
        path.write_text(result.model_dump_json(indent=2))
        write_cover_html(result, directory / "review.html")
        store.save_document(identifier, book_id, "cover-review", path, sha256_file(path))
        store.event(
            book_id,
            "COVERS_REVIEWED",
            f"Cover review {identifier}: {len(candidates)} candidates; no replacement applied.",
        )
        return result


def load_cover_review(store: Store, book_id: str, identifier: str | None = None) -> CoverReview:
    row = store.document(book_id, "cover-review", identifier)
    path = Path(row["path"])
    if sha256_file(path) != row["sha256"]:
        raise ValueError("Cover review changed after registration; prepare another review.")
    review = CoverReview.model_validate_json(path.read_text())
    if review.id != row["id"] or review.book_id != book_id:
        raise ValueError("Cover review identity mismatch.")
    for asset in [review.original, *review.candidates]:
        if asset and sha256_file(Path(asset.path)) != asset.sha256:
            raise ValueError("Stored cover asset hash mismatch; prepare another review.")
    return review


def comparison(original: CoverAsset | None, candidate: CoverAsset) -> list[str]:
    notes = list(candidate.warnings)
    if original:
        if candidate.media_type != original.media_type:
            notes.append(
                "Format differs from the existing cover; this replacement path cannot apply it."
            )
        if candidate.width < original.width or candidate.height < original.height:
            notes.append("At least one dimension is smaller than the existing cover.")
        if aspect_changed(original, candidate):
            notes.append(
                "Aspect ratio differs by more than 5%; existing cover-page geometry may distort it."
            )
        distance = (int(original.dhash, 16) ^ int(candidate.dhash, 16)).bit_count()
        notes.append(
            f"Perceptual hash distance: {distance}/64. A low distance suggests similar layout, not confirmed identical artwork."
        )
        if candidate.sha256 == original.sha256:
            notes.append("Identical bytes to the current cover; no replacement needed.")
    return notes


def aspect_changed(original: CoverAsset, candidate: CoverAsset) -> bool:
    return abs((candidate.width / candidate.height) / (original.width / original.height) - 1) > 0.05


def write_cover_html(review: CoverReview, path: Path):
    esc = html.escape
    figures = []
    for asset, label in [
        (review.original, "Current cover"),
        *[(c, f"Candidate {c.id}") for c in review.candidates],
    ]:
        if asset is None:
            figures.append(
                "<section><h2>Current cover</h2><p>No supported unambiguous cover declaration.</p></section>"
            )
            continue
        with Image.open(io.BytesIO(read_image(Path(asset.path)))) as image:
            preview = ImageOps.exif_transpose(image).convert("RGB")
            preview.thumbnail((500, 700), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            preview.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode()
        notes = asset.warnings if asset is review.original else comparison(review.original, asset)
        details_html = "".join(
            f"<p><strong>{esc(field.capitalize())}:</strong> {esc('; '.join(values) or '(missing)')}</p>"
            for field, values in asset.edition_details.items()
        )
        source_html = (
            f'<a href="{esc(asset.source, quote=True)}">Edition record</a>'
            if re.fullmatch(r"https://openlibrary\.org/books/OL[0-9]+M", asset.source)
            else esc(asset.source)
        )
        figures.append(
            f'<section><h2>{esc(label)}</h2><img src="data:image/png;base64,{encoded}" alt="{esc(label)} artwork"><p>{asset.width} × {asset.height} px · {asset.byte_count:,} bytes · {esc(asset.media_type)}</p><p class="sources">{source_html}</p>{details_html}<code>{asset.sha256}</code><ul>{"".join(f"<li>{esc(note)}</li>" for note in notes)}</ul></section>'
        )
    path.write_text(f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:"><title>Emend cover review</title><style>{report_styles()}
.cover-grid {{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,20rem),1fr));gap:2rem}}
.cover-grid section {{min-width:0;border-bottom:1px solid var(--rule);padding-bottom:2rem}}
.cover-grid img {{max-width:100%;height:auto;max-height:32rem;display:block;margin:1rem auto}}
</style></head><body><main><header><div class="brand">EMEND / COVER REVIEW</div><h1>Review the artwork</h1><p>{esc("; ".join(review.metadata.get("title", [])))}</p><p>Compare the edition, artwork, crop, and legibility before choosing a candidate.</p><code>{review.book_id}</code></header><div class="cover-grid">{"".join(figures)}</div><h2>Review notes</h2><ul>{"".join(f"<li>{esc(note)}</li>" for note in [*review.notes, *review.errors])}</ul><p>After reviewing, apply explicitly:</p><code>emend apply-cover {review.book_id[:12]} --review {review.id} --approve CANDIDATE_ID</code><footer>Generated {esc(review.created_at)}. This report does not certify image quality, edition identity, or device rendering.</footer></main></body></html>""")


def cover_review_text(review: CoverReview) -> str:
    lines = [f"Cover review {review.id}", f"Book {review.book_id}", "No cover changed."]
    for candidate in review.candidates:
        lines += [
            f"{candidate.id} · {candidate.width} × {candidate.height} · {candidate.media_type}",
            f"  Source: {candidate.source}",
            *[f"  Review: {n}" for n in comparison(review.original, candidate)],
        ]
    lines += [f"UNAVAILABLE: {error}" for error in review.errors]
    lines += review.notes
    return "\n".join(lines)
