"""Missing-cover creation uses only original corpus books and generated artwork."""

import os
import zipfile
from pathlib import Path

import pytest

from emend.cover_create import create_cover
from emend.covers import cover_review, declared_cover
from emend.inspect import sha256_file
from emend.pipeline import get_report, ingest
from emend.storage import Store
from tests.epub_factory import book_files, write_epub
from tests.test_covers import candidate, cover_book


def prepare(tmp_path, config, version="3.0", size=(800, 1200), files=None):
    before = ingest(write_epub(tmp_path / "missing.epub", files or book_files(version)), config)
    review = cover_review(
        before.book_id,
        config,
        image=candidate(tmp_path, size),
        source_note="Original generated test artwork",
    )
    assert review.original is None
    return before, review


def apply(before, review, config, **kw):
    return create_cover(
        before.book_id, config, review_id=review.id, approve=review.candidates[0].id, **kw
    )


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_creation_preserves_existing_bytes_relative_order_and_provenance(tmp_path, config, version):
    before, review = prepare(tmp_path, config, version)
    after = apply(before, review, config)
    assert after.state == "VALIDATED"
    assert after.inspection.spine[1:] == before.inspection.spine
    assert after.inspection.metadata == before.inspection.metadata
    artifact = config.root / "curated" / f"{before.book_id}.epub"
    original = config.root / "originals" / artifact.name
    assert sha256_file(original) == before.book_id
    change = after.cover_additions[0]
    with zipfile.ZipFile(original) as old, zipfile.ZipFile(artifact) as new:
        assert new.namelist() == [*old.namelist(), change.image_path, change.page_path]
        for name in old.namelist():
            if name != before.inspection.package_path:
                assert old.read(name) == new.read(name)
        assert new.read(change.image_path) == Path(review.candidates[0].path).read_bytes()
    assert declared_cover(artifact, before.inspection.package_path)[0] == change.image_path
    assert change.source == "Original generated test artwork"
    assert apply(before, review, config) == after
    assert (
        "Created missing cover"
        in Path(Store(config.root).resolve(before.book_id)["report_path"])
        .with_suffix(".html")
        .read_text()
    )


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_missing_cover_passes_real_epubcheck(tmp_path, config, version):
    jar = os.environ.get("EMEND_TEST_EPUBCHECK_JAR")
    if not jar:
        pytest.skip("Official EPUBCheck not configured")
    config.tools.epubcheck = None
    config.tools.epubcheck_jar = Path(jar)
    before, review = prepare(tmp_path, config, version)
    after = apply(before, review, config)
    assert next(g for g in after.gates if g.name == "epubcheck").status == "passed"


def test_small_source_requires_recorded_exception(tmp_path, config):
    before, review = prepare(tmp_path, config, size=(320, 480))
    with pytest.raises(ValueError, match="allow-small"):
        apply(before, review, config)
    assert apply(before, review, config, allow_small=True).cover_additions[0].allowed_small


@pytest.mark.parametrize("hint", ["fixed", "landmark", "guide"])
def test_existing_cover_hints_and_fixed_layout_refused(tmp_path, config, hint):
    files = book_files()
    if hint == "fixed":
        files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
            b"</metadata>", b'<meta property="rendition:layout">pre-paginated</meta></metadata>'
        )
    elif hint == "guide":
        files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
            b"</package>",
            b'<guide><reference type="cover" href="chapter.xhtml" title="Cover"/></guide></package>',
        )
    else:
        files["EPUB/nav.xhtml"] = files["EPUB/nav.xhtml"].replace(
            b"</body>",
            b'<nav epub:type="landmarks"><ol><li><a epub:type="cover" href="chapter.xhtml">Cover</a></li></ol></nav></body>',
        )
    before, review = prepare(tmp_path, config, files=files)
    with pytest.raises(ValueError, match="reflowable|cover-page hint|semantic cover"):
        apply(before, review, config)
    assert get_report(Store(config.root), before.book_id) == before


def test_declared_cover_and_unknown_approval_refused(tmp_path, config):
    before = cover_book(tmp_path, config)
    review = cover_review(before.book_id, config, image=candidate(tmp_path, (800, 1200)))
    with pytest.raises(ValueError, match="already exists"):
        apply(before, review, config)
    with pytest.raises(ValueError, match="candidate ID"):
        create_cover(before.book_id, config, review_id=review.id, approve="invented")


@pytest.mark.parametrize("fault", ["validator", "database", "payload", "activation"])
def test_failures_retain_approved_artifact(tmp_path, config, monkeypatch, fault):
    import emend.cover_create as module

    before, review = prepare(tmp_path, config)
    if fault == "validator":
        config.tools.epubcheck = tmp_path / "absent"
    elif fault == "database":

        def fail(*args, **kw):
            raise OSError("database unavailable")

        monkeypatch.setattr(Store, "transition", fail)
    elif fault == "activation":
        real = module.atomic_copy

        def fail(src, dst):
            if dst.parent.name == "curated" and src.name == "candidate.epub":
                dst.chmod(0o600)
                dst.write_bytes(b"interrupted")
                raise OSError("activation interrupted")
            return real(src, dst)

        monkeypatch.setattr(module, "atomic_copy", fail)
    else:
        real = module.rewrite

        def change(src, dst, *args):
            real(src, dst, *args)
            with zipfile.ZipFile(dst) as archive:
                files = {n: archive.read(n) for n in archive.namelist()}
            files["EPUB/style.css"] += b"p{display:none}"
            write_epub(dst, files)

        monkeypatch.setattr(module, "rewrite", change)
    with pytest.raises((ValueError, OSError), match="rejected|unavailable|interrupted"):
        apply(before, review, config)
    assert get_report(Store(config.root), before.book_id) == before
    assert sha256_file(config.root / "curated" / f"{before.book_id}.epub") == before.book_id
    assert sha256_file(config.root / "originals" / f"{before.book_id}.epub") == before.book_id


def test_cover_review_and_candidate_changes_are_rejected(tmp_path, config):
    before, review = prepare(tmp_path, config)
    asset = Path(review.candidates[0].path)
    asset.chmod(0o600)
    asset.write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        apply(before, review, config)


def test_workspace_missing_cover_action(workspace, tmp_path, config):
    from tests.test_review_ui import fields, login, post, request

    before, review = prepare(tmp_path, config)
    cookie = login(workspace)
    status, _, body = request(workspace, "/books/" + before.book_id, headers={"Cookie": cookie})
    assert status == 200 and "Create reviewed missing cover" in body
    assert (
        post(
            workspace,
            cookie,
            fields(
                workspace,
                before,
                "create-cover",
                review=review.id,
                candidate=review.candidates[0].id,
            ),
        )[0]
        == 303
    )
    assert get_report(Store(config.root), before.book_id).cover_additions
