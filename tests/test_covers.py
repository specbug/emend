import io
import os
import urllib.request
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from emend.cover_apply import apply_cover
from emend.covers import CoverRedirect, cover_review, image_details, load_cover_review
from emend.curation import apply_proposals
from emend.inspect import sha256_file
from emend.pipeline import get_report, ingest
from emend.research.service import research
from emend.storage import Store
from tests.epub_factory import book_files, write_epub


def cover_book(tmp_path, config, version="3.0", declaration=True):
    files = book_files(version)
    if declaration:
        if version == "3.0":
            files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
                b'id="image"', b'id="image" properties="cover-image"'
            )
        else:
            files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
                b"</metadata>", b'<meta name="cover" content="image"/></metadata>'
            )
    report = ingest(write_epub(tmp_path / "cover-book.epub", files), config)
    assert report.state == "VALIDATED"
    return report


def candidate(tmp_path, size=(320, 480), format="PNG", name="candidate.png"):
    path = tmp_path / name
    image = Image.new("RGB", size, "#c2a050")
    image.paste("#2b3344", (size[0] // 4, size[1] // 4, size[0] * 3 // 4, size[1] * 3 // 4))
    image.save(path, format=format)
    return path


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_cover_application_preserves_resources_history_and_is_idempotent(tmp_path, config, version):
    before = cover_book(tmp_path, config, version)
    review = cover_review(
        before.book_id, config, image=candidate(tmp_path), source_note="Publisher-provided fixture"
    )
    assert review.original and len(review.candidates) == 1 and not review.errors
    after = apply_cover(
        before.book_id, config, review_id=review.id, approve=review.candidates[0].id
    )
    assert after.state == "VALIDATED" and after.artifact_sha256 != before.book_id
    assert after.inspection.text_sha256 == before.inspection.text_sha256
    assert after.inspection.metadata == before.inspection.metadata
    assert after.cover_changes[0].source == "Publisher-provided fixture"
    original = config.root / "originals" / f"{before.book_id}.epub"
    artifact = config.root / "curated" / original.name
    assert sha256_file(original) == before.book_id
    with zipfile.ZipFile(original) as old, zipfile.ZipFile(artifact) as new:
        assert old.namelist() == new.namelist()
        for name in old.namelist():
            if name not in {"EPUB/package.opf", "EPUB/observatory.png"}:
                assert old.read(name) == new.read(name)
    assert (
        apply_cover(before.book_id, config, review_id=review.id, approve=review.candidates[0].id)
        == after
    )
    assert len(list((config.root / "versions" / before.book_id).glob("*.epub"))) == 2
    html = (
        Path(Store(config.root).resolve(before.book_id)["report_path"]).with_suffix(".html")
    ).read_text()
    assert "Cover changes" in html and "Publisher-provided fixture" in html


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_cover_replacement_passes_official_epubcheck(tmp_path, config, version):
    jar = os.environ.get("EMEND_TEST_EPUBCHECK_JAR")
    if not jar:
        pytest.skip("Official EPUBCheck JAR not configured")
    config.tools.epubcheck = None
    config.tools.epubcheck_jar = Path(jar)
    before = cover_book(tmp_path, config, version)
    review = cover_review(before.book_id, config, image=candidate(tmp_path))
    after = apply_cover(
        before.book_id, config, review_id=review.id, approve=review.candidates[0].id
    )
    assert next(g for g in after.gates if g.name == "epubcheck").status == "passed"


@pytest.mark.parametrize(
    ("size", "flag", "match"),
    [((80, 120), "allow_smaller", "smaller"), ((320, 320), "allow_aspect_change", "aspect ratio")],
)
def test_dimension_changes_require_explicit_exceptions(tmp_path, config, size, flag, match):
    before = cover_book(tmp_path, config)
    review = cover_review(before.book_id, config, image=candidate(tmp_path, size))
    with pytest.raises(ValueError, match=match):
        apply_cover(before.book_id, config, review_id=review.id, approve=review.candidates[0].id)
    after = apply_cover(
        before.book_id, config, review_id=review.id, approve=review.candidates[0].id, **{flag: True}
    )
    assert getattr(after.cover_changes[0], flag.replace("allow_", "allowed_"))


def test_format_mismatch_and_missing_cover_are_not_silently_repaired(tmp_path, config):
    before = cover_book(tmp_path, config)
    review = cover_review(before.book_id, config, image=candidate(tmp_path, format="JPEG"))
    with pytest.raises(ValueError, match="same JPEG/PNG format"):
        apply_cover(before.book_id, config, review_id=review.id, approve=review.candidates[0].id)
    missing = cover_book(tmp_path, config, declaration=False)
    review = cover_review(missing.book_id, config, image=candidate(tmp_path))
    assert review.original is None
    with pytest.raises(ValueError, match="missing, ambiguous"):
        apply_cover(missing.book_id, config, review_id=review.id, approve=review.candidates[0].id)


def test_corrupt_candidates_and_unsafe_images_are_rejected(tmp_path, config):
    with pytest.raises(ValueError, match="damaged"):
        image_details(b"not an image")
    image = Image.new("RGB", (320, 480))
    data = io.BytesIO()
    exif = Image.Exif()
    exif[274] = 6
    image.save(data, format="JPEG", exif=exif)
    assert image_details(data.getvalue())["orientation"] == 6
    path = tmp_path / "animated.png"
    Image.new("RGB", (320, 480), "red").save(
        path,
        save_all=True,
        append_images=[Image.new("RGB", (320, 480), "blue")],
        duration=100,
        loop=0,
    )
    with pytest.raises(ValueError, match="Animated"):
        image_details(path.read_bytes())


def test_cover_plan_and_asset_tampering_are_rejected(tmp_path, config):
    before = cover_book(tmp_path, config)
    review = cover_review(before.book_id, config, image=candidate(tmp_path))
    image = Path(review.candidates[0].path)
    image.chmod(0o600)
    image.write_bytes(b"changed")
    with pytest.raises(ValueError, match="asset hash mismatch"):
        load_cover_review(Store(config.root), before.book_id, review.id)
    review = cover_review(
        before.book_id, config, image=candidate(tmp_path, name="second.png", size=(640, 960))
    )
    path = Path(Store(config.root).document(before.book_id, "cover-review", review.id)["path"])
    path.write_text(path.read_text().replace("User-supplied", "Misrepresented"))
    with pytest.raises(ValueError, match="changed after registration"):
        load_cover_review(Store(config.root), before.book_id, review.id)


def test_required_validator_failure_retains_current_artifact(tmp_path, config):
    before = cover_book(tmp_path, config)
    review = cover_review(before.book_id, config, image=candidate(tmp_path))
    config.tools.epubcheck = tmp_path / "missing"
    with pytest.raises(ValueError, match="candidate rejected"):
        apply_cover(before.book_id, config, review_id=review.id, approve=review.candidates[0].id)
    assert get_report(Store(config.root), before.book_id) == before
    assert sha256_file(config.root / "curated" / f"{before.book_id}.epub") == before.book_id


def test_database_failure_rolls_back_cover_swap(tmp_path, config, monkeypatch):
    before = cover_book(tmp_path, config)
    review = cover_review(before.book_id, config, image=candidate(tmp_path))

    def fail(*args, **kwargs):
        raise OSError("database unavailable")

    monkeypatch.setattr(Store, "transition", fail)
    with pytest.raises(OSError, match="database unavailable"):
        apply_cover(before.book_id, config, review_id=review.id, approve=review.candidates[0].id)
    assert get_report(Store(config.root), before.book_id) == before
    assert sha256_file(config.root / "curated" / f"{before.book_id}.epub") == before.book_id


def test_unrelated_resource_changes_are_rejected(tmp_path, config, monkeypatch):
    import emend.cover_apply as module

    before = cover_book(tmp_path, config)
    review = cover_review(before.book_id, config, image=candidate(tmp_path))
    replace = module.replace_cover

    def bad(source, target, package, cover_path, data):
        digest = replace(source, target, package, cover_path, data)
        with zipfile.ZipFile(target) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        files["EPUB/style.css"] += b"p{display:none}"
        write_epub(target, files)
        return digest

    monkeypatch.setattr(module, "replace_cover", bad)
    with pytest.raises(ValueError, match="candidate rejected"):
        apply_cover(before.book_id, config, review_id=review.id, approve=review.candidates[0].id)
    assert get_report(Store(config.root), before.book_id) == before


def test_cover_history_survives_later_metadata_edits(tmp_path, config, search_responses):
    from emend.research.discovery import discover

    before = cover_book(tmp_path, config)
    review = cover_review(before.book_id, config, image=candidate(tmp_path))
    after = apply_cover(
        before.book_id, config, review_id=review.id, approve=review.candidates[0].id
    )
    discovery = discover(before.book_id, config)
    plan = research(
        before.book_id, config, discovery_id=discovery.id, edition_id="openlibrary:/books/OL1M"
    )
    publisher = next(p for p in plan.proposals if p.field == "publisher")
    edited = apply_proposals(before.book_id, config, approve=[publisher.id])
    assert edited.cover_changes == after.cover_changes
    with pytest.raises(ValueError, match="stale"):
        apply_cover(before.book_id, config, review_id=review.id, approve=review.candidates[0].id)


def test_cover_redirect_restrictions():
    req = urllib.request.Request("https://covers.openlibrary.org/b/id/123-L.jpg?default=false")
    with pytest.raises(ValueError, match="recorded Open Library"):
        CoverRedirect().redirect_request(
            req, None, 302, "Found", {}, "https://attacker.invalid/image.jpg"
        )


def test_comparison_escapes_source_note_and_contains_images(tmp_path, config):
    before = cover_book(tmp_path, config)
    review = cover_review(
        before.book_id, config, image=candidate(tmp_path), source_note="<script>bad()</script>"
    )
    path = Path(
        Store(config.root).document(before.book_id, "cover-review", review.id)["path"]
    ).with_suffix(".html")
    html = path.read_text()
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert html.count('src="data:image/png;base64,') == 2
