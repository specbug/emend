import copy
import os
import zipfile
from pathlib import Path

import pytest

from emend.curation import apply_proposals
from emend.inspect import NS, parse_xml, sha256_file
from emend.pipeline import get_report, ingest
from emend.research.service import research
from emend.revalidation import revalidate
from emend.storage import Store
from emend.typography import TypographySettings, typography_trial
from emend.typography_apply import TypographyRejectedError, apply_typography
from emend.visual import visual_review
from tests.epub_factory import book_files, write_epub
from tests.test_curation import setup_book
from tests.test_research import evidence_client


def setup(tmp_path, config, version="3.0", **kwargs):
    before = ingest(write_epub(tmp_path / "book.epub", version=version), config)
    trial = typography_trial(
        before.book_id,
        config,
        TypographySettings(line_height=1.7, paragraph_gap=0.6),
        profiles=["compact"],
        max_documents=1,
        **kwargs,
    )
    return before, trial


def apply(trial, config, **kwargs):
    return apply_typography(trial.book_id, config, trial_id=trial.id, approve=True, **kwargs)


@pytest.mark.parametrize("version", ["2.0", "3.0"])
@pytest.mark.parametrize("all_spine", [False, True])
def test_explicit_typography_keeps_text_and_unrelated_bytes(
    tmp_path, config, fake_capture, version, all_spine
):
    before, trial = setup(tmp_path, config, version)
    after = apply(trial, config, all_spine=all_spine)
    assert after.state == "VALIDATED"
    assert after.inspection.text_sha256 == before.inspection.text_sha256
    assert after.inspection.metadata == before.inspection.metadata
    assert after.inspection.spine == before.inspection.spine
    change = after.typography_changes[-1]
    expected = before.inspection.spine if all_spine else trial.selected_documents
    assert change.documents == expected and change.all_spine == all_spine
    assert change.css_sha256 == trial.css_sha256
    original = config.root / "originals" / f"{before.book_id}.epub"
    artifact = config.root / "curated" / original.name
    with zipfile.ZipFile(original) as old, zipfile.ZipFile(artifact) as new:
        assert new.read(change.resource_path).decode() == trial.settings.css()
        for name in old.namelist():
            if name not in {before.inspection.package_path, *expected}:
                assert old.read(name) == new.read(name)
        for name in expected:
            head = parse_xml(new.read(name)).find("h:head", NS)
            assert head[-1].tag == f"{{{NS['h']}}}link"
    assert sha256_file(original) == before.book_id
    assert apply(trial, config, all_spine=all_spine) == after
    assert revalidate(before.book_id, config).typography_changes == after.typography_changes


def test_no_implicit_approval(tmp_path, config, fake_capture):
    before, trial = setup(tmp_path, config)
    with pytest.raises(ValueError, match="approve"):
        apply_typography(before.book_id, config, trial_id=trial.id, approve=False)
    assert get_report(Store(config.root), before.book_id) == before


@pytest.mark.parametrize("mutation", ["text", "style", "extra_link", "manifest", "new_file", "css"])
def test_independent_preservation_rejects_unapproved_changes(
    tmp_path, config, fake_capture, monkeypatch, mutation
):
    import emend.typography_apply as module

    before, trial = setup(tmp_path, config)
    real = module.rewrite

    def rewrite(source, candidate, *args):
        resource = real(source, candidate, *args)
        with zipfile.ZipFile(candidate) as archive:
            infos = archive.infolist()
            data = {i.filename: archive.read(i) for i in infos}
        if mutation == "text":
            data["EPUB/chapter.xhtml"] = data["EPUB/chapter.xhtml"].replace(b"At dusk", b"One day")
        elif mutation == "style":
            data["EPUB/style.css"] += b"p{color:red}"
        elif mutation == "extra_link":
            data["EPUB/chapter.xhtml"] = data["EPUB/chapter.xhtml"].replace(
                b"</head>",
                b'<link xmlns="http://www.w3.org/1999/xhtml" rel="stylesheet" href="style.css"/></head>',
            )
        elif mutation == "manifest":
            data["EPUB/package.opf"] = data["EPUB/package.opf"].replace(
                b"<dc:publisher>Emend", b"<dc:publisher>Other"
            )
        elif mutation == "css":
            data[resource] += b"p{font-size:1px}"
        with zipfile.ZipFile(candidate, "w") as archive:
            for info in infos:
                archive.writestr(copy.copy(info), data[info.filename])
            if mutation == "new_file":
                archive.writestr("unexpected.bin", b"bad")
        return resource

    monkeypatch.setattr(module, "rewrite", rewrite)
    with pytest.raises(TypographyRejectedError):
        apply(trial, config)
    assert get_report(Store(config.root), before.book_id) == before
    assert sha256_file(config.root / "curated" / f"{before.book_id}.epub") == before.book_id


@pytest.mark.parametrize("failure", ["validator", "database", "copy", "stale", "incomplete"])
def test_failed_or_stale_application_retains_current_book(
    tmp_path, config, fake_capture, monkeypatch, failure
):
    import emend.typography_apply as module

    if failure == "incomplete":
        monkeypatch.setattr("emend.typography.render", lambda *a, **kw: ("fixture", []))
    before, trial = setup(tmp_path, config)
    if failure == "validator":
        config.tools.epubcheck = tmp_path / "missing"
    elif failure == "database":
        monkeypatch.setattr(
            Store, "transition", lambda *a, **kw: (_ for _ in ()).throw(OSError("database failed"))
        )
    elif failure == "copy":
        real = module.atomic_copy
        failed = False

        def copy_then_fail(source, target):
            nonlocal failed
            real(source, target)
            if target.parent.name == "curated" and not failed:
                failed = True
                raise OSError("copy failure after replacement")

        monkeypatch.setattr(module, "atomic_copy", copy_then_fail)
    elif failure == "stale":
        trial_path = Path(
            Store(config.root).document(before.book_id, "typography-trial", trial.id)["path"]
        )
        (trial_path.parent / "trial.css").write_text("changed")
    with pytest.raises((ValueError, OSError)):
        apply(trial, config)
    assert get_report(Store(config.root), before.book_id) == before
    assert sha256_file(config.root / "curated" / f"{before.book_id}.epub") == before.book_id


def test_typography_history_survives_later_metadata_edits(tmp_path, config, fake_capture):
    before, _ = setup_book(tmp_path, config)
    trial = typography_trial(
        before.book_id, config, TypographySettings(line_height=1.6), max_documents=1
    )
    styled = apply(trial, config)
    research(before.book_id, config, client=evidence_client(tmp_path))
    edited = apply_proposals(before.book_id, config, safe=True)
    assert edited.typography_changes == styled.typography_changes
    with pytest.raises(ValueError, match="stale"):
        apply(trial, config)


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_official_epubcheck_accepts_typography_application(tmp_path, config, fake_capture, version):
    jar = os.environ.get("EMEND_TEST_EPUBCHECK_JAR")
    if not jar:
        pytest.skip("Set EMEND_TEST_EPUBCHECK_JAR")
    config.tools.epubcheck_jar = Path(jar)
    _, trial = setup(tmp_path, config, version)
    report = apply(trial, config, all_spine=True)
    assert next(g.status for g in report.gates if g.name == "epubcheck") == "passed"


def test_real_browser_applied_css_matches_reviewed_preview(tmp_path, browser_config):
    config = browser_config
    files = book_files()
    files["EPUB/chapter.xhtml"] = files["EPUB/chapter.xhtml"].replace(
        b"</body>", b"<p>Original text for line rhythm and paragraph spacing.</p>" * 40 + b"</body>"
    )
    before = ingest(write_epub(tmp_path / "book.epub", files), config)
    trial = typography_trial(
        before.book_id,
        config,
        TypographySettings(line_height=2, paragraph_gap=1),
        profiles=["compact"],
        max_documents=1,
    )
    assert trial.status == "complete"
    apply(trial, config)
    current = visual_review(before.book_id, config, profiles=["compact"], max_documents=1)
    assert current.status == "complete"
    assert current.samples[0].document_height == trial.after[0].document_height
    assert current.samples[0].typography == trial.after[0].typography
    assert current.samples[0].typography
