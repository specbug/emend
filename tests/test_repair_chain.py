"""Supported repairs combined into one plan: each step checked, only the final copy used."""

import os
import zipfile
from itertools import pairwise
from pathlib import Path

import pytest

from emend.inspect import sha256_file
from emend.navigation_repair import ACTION as LINKS
from emend.pipeline import get_report, ingest
from emend.repairs import ACTION as PACKAGING
from emend.repairs import RepairRejectedError, apply_repairs, prepare_repairs
from emend.storage import Store
from emend.xhtml_repair import ACTION as MARKUP
from tests.epub_factory import write_epub
from tests.test_repairs import damaged_epub
from tests.test_xhtml_repair import markup_files

KINDS = {
    "packaging+markup": [PACKAGING, MARKUP],
    "packaging+links": [PACKAGING, LINKS],
    "links+markup": [LINKS, MARKUP],
    "packaging+links+markup": [PACKAGING, LINKS, MARKUP],
}


def files_for(kind, version="2.0", href=b"CHAPTER.xhtml#start"):
    if "markup" in kind:
        files = markup_files(version)
    else:
        from tests.epub_factory import book_files

        files = book_files(version)
    if "links" in kind:
        nav = "EPUB/nav.xhtml" if version == "3.0" else "EPUB/toc.ncx"
        files[nav] = files[nav].replace(b"chapter.xhtml#start", href)
    return files


def book(tmp_path, config, kind, **options):
    files = files_for(kind, **options)
    path = tmp_path / "book.epub"
    source = damaged_epub(path, files=files) if "packaging" in kind else write_epub(path, files)
    before = ingest(source, config)
    assert before.state == "FAILED"
    return source, before


@pytest.mark.parametrize("kind", list(KINDS))
def test_combined_repairs_apply_as_one_checked_plan(tmp_path, config, kind):
    source, before = book(tmp_path, config, kind)
    plan, review = prepare_repairs(before.book_id, config)
    assert plan.actions == KINDS[kind]
    assert plan.detail.startswith("Step 1: ") and "Step 2: " in plan.detail
    assert "checked one by one" in review.read_text()
    # Preparing simulates in staging only; the book and its report are untouched.
    assert get_report(Store(config.root), before.book_id) == before
    assert not (config.root / "curated" / f"{before.book_id}.epub").exists()
    after = apply_repairs(before.book_id, config, plan_id=plan.id, approve=plan.actions)
    assert after.state == "VALIDATED"
    changes = after.repair_changes
    assert [c.action for c in changes] == KINDS[kind]
    assert {c.plan_id for c in changes} == {plan.id}
    # The steps form one unbroken chain from the original to the activated copy.
    assert changes[0].before_sha256 == before.book_id
    assert all(a.after_sha256 == b.before_sha256 for a, b in pairwise(changes))
    artifact = config.root / "curated" / f"{before.book_id}.epub"
    assert changes[-1].after_sha256 == after.artifact_sha256 == sha256_file(artifact)
    assert sha256_file(source) == before.book_id
    assert sha256_file(config.root / "originals" / f"{before.book_id}.epub") == before.book_id
    assert after.inspection.metadata == before.inspection.metadata
    assert after.inspection.spine == before.inspection.spine
    assert after.inspection.text_sha256 == before.inspection.text_sha256
    assert after.inspection.spine_text_sha256 == before.inspection.spine_text_sha256
    changed = {"mimetype", "EPUB/package.opf"}
    if MARKUP in plan.actions:
        changed |= {"EPUB/chapter.xhtml", "EPUB/notes.xhtml"}
    if LINKS in plan.actions:
        changed.add("EPUB/toc.ncx")
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(artifact) as new:
        assert sorted(old.namelist()) == sorted(new.namelist())
        for name in old.namelist():
            if name not in changed:
                assert old.read(name) == new.read(name), name
    # Retrying the applied plan is idempotent and does not stack more changes.
    again = apply_repairs(before.book_id, config, plan_id=plan.id, approve=plan.actions)
    assert again.repair_changes == changes


def test_every_step_of_a_combined_plan_needs_approval(tmp_path, config):
    _, before = book(tmp_path, config, "packaging+markup")
    plan, _ = prepare_repairs(before.book_id, config)
    for partial in ([PACKAGING], [MARKUP], []):
        with pytest.raises(ValueError, match="Explicitly approve"):
            apply_repairs(before.book_id, config, plan_id=plan.id, approve=partial)
    assert get_report(Store(config.root), before.book_id) == before


@pytest.mark.parametrize("step", ["container_preserved", "markup_preserved"])
def test_one_failed_step_rejects_the_whole_chain(tmp_path, config, monkeypatch, step):
    source, before = book(tmp_path, config, "packaging+markup")
    plan, _ = prepare_repairs(before.book_id, config)
    monkeypatch.setattr(f"emend.repairs.{step}", lambda *args: False)
    with pytest.raises(RepairRejectedError):
        apply_repairs(before.book_id, config, plan_id=plan.id, approve=plan.actions)
    assert get_report(Store(config.root), before.book_id) == before
    assert not (config.root / "curated" / f"{before.book_id}.epub").exists()
    assert sha256_file(source) == before.book_id


def test_packaging_is_not_rewritten_when_the_rest_cannot_be_repaired(tmp_path, config):
    # A missing (not merely miscased) contents target leaves the copy unreadable.
    _, before = book(tmp_path, config, "packaging+links", href=b"missing.xhtml#start")
    plan, _ = prepare_repairs(before.book_id, config)
    assert plan.actions == []
    assert (
        "No supported repair"
        in (config.root / "repairs" / before.book_id / plan.id / "review.html").read_text()
    )


def test_workspace_run_applies_a_combined_plan_in_one_step(workspace, tmp_path, config, fake_pages):
    from tests.test_review_ui import login, request, upload

    source = damaged_epub(tmp_path / "book.epub", files=markup_files("2.0"))
    cookie = login(workspace)
    assert upload(workspace, cookie, source)[0] == 303
    book_id = sha256_file(source)
    after = get_report(Store(config.root), book_id)
    assert after.state == "VALIDATED"
    assert [c.action for c in after.repair_changes] == [PACKAGING, MARKUP]
    run = workspace.app.runner.get(book_id)
    assert "Fix EPUB packaging" in run.step("repair").note
    assert "Fix HTML markup" in run.step("repair").note
    body = request(workspace, f"/books/{book_id}?step=repair", headers={"Cookie": cookie})[2]
    assert "Fix EPUB packaging" in body and "Fix HTML markup" in body


@pytest.mark.parametrize("kind", list(KINDS))
def test_official_epubcheck_accepts_combined_repairs(tmp_path, config, kind):
    jar = os.environ.get("EMEND_TEST_EPUBCHECK_JAR")
    if not jar:
        pytest.skip("Set EMEND_TEST_EPUBCHECK_JAR for official validation")
    config.tools.epubcheck_jar = Path(jar)
    _, before = book(tmp_path, config, kind)
    plan, _ = prepare_repairs(before.book_id, config)
    after = apply_repairs(before.book_id, config, plan_id=plan.id, approve=plan.actions)
    assert after.state == "VALIDATED"
    assert next(g for g in after.gates if g.name == "epubcheck").status == "passed"
