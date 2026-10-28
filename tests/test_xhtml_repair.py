"""Original fixtures for scoped markup repair, preservation and browser workflow."""

import copy
import json
import os
import zipfile
from pathlib import Path

import pytest

from emend.inspect import NS, parse_xml, sha256_file
from emend.pipeline import get_report, ingest
from emend.repairs import RepairRejectedError, apply_repairs, prepare_repairs
from emend.revalidation import revalidate
from emend.storage import Store
from emend.xhtml_repair import ACTION, XML_LANG, markup_preserved
from tests.epub_factory import book_files, write_epub


def markup_files(version="2.0"):
    files = book_files(version)
    for name in ("EPUB/chapter.xhtml", "EPUB/notes.xhtml"):
        files[name] = files[name].replace(b'xml:lang="en"', b'lang="" xml:lang=""')
    files["EPUB/chapter.xhtml"] = (
        files["EPUB/chapter.xhtml"]
        .replace(b"?>", b"?><!--keep this--><?keep value?>", 1)
        .replace(b"<body><h1", b'<body><p class="chapter-wrap" id="wrapper"><h1')
        .replace(b"</h1>", b'</h1><ul><li><a href="notes.xhtml#note1">Notes</a></li></ul></p>', 1)
        .replace(b"one observation", b'one <span xml:lang="fr">observation</span>')
    )
    files["EPUB/style.css"] += b" .chapter-wrap { display:block; margin:1em 0; }"
    return files


def setup(tmp_path, config, version="2.0", files=None):
    source = write_epub(tmp_path / "markup.epub", files or markup_files(version))
    before = ingest(source, config)
    plan, review = prepare_repairs(before.book_id, config)
    return source, before, plan, review


def apply(plan, config):
    return apply_repairs(plan.book_id, config, plan_id=plan.id, approve=[ACTION])


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_reviewed_markup_preserves_text_attributes_and_unrelated_bytes(tmp_path, config, version):
    files = markup_files(version)
    files["untouched.bin"] = b"Keep unmanifested bytes, too"
    files["empty/"] = b""
    source, before, plan, review = setup(tmp_path, config, version, files)
    assert plan.actions == [ACTION] and len(plan.markup) == (5 if version == "2.0" else 1)
    assert "Proposed HTML changes" in review.read_text()
    assert "paragraph" in review.read_text()
    assert get_report(Store(config.root), before.book_id) == before
    assert sha256_file(source) == before.book_id
    after = apply(plan, config)
    assert after.state == "VALIDATED"
    assert after.repair_changes[-1].action == ACTION
    assert before.inspection.spine == after.inspection.spine
    assert before.inspection.text_sha256 == after.inspection.text_sha256
    assert before.inspection.spine_text_sha256 == after.inspection.spine_text_sha256
    assert before.inspection.metadata == after.inspection.metadata
    artifact = config.root / "curated" / f"{before.book_id}.epub"
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(artifact) as new:
        assert old.namelist() == new.namelist()
        for name in old.namelist():
            if name not in {"EPUB/chapter.xhtml", "EPUB/notes.xhtml", "EPUB/package.opf"}:
                assert old.read(name) == new.read(name)
        if version == "2.0":
            assert old.read("EPUB/package.opf") == new.read("EPUB/package.opf")
        tree = parse_xml(new.read("EPUB/chapter.xhtml"))
        assert tree.get("lang") == tree.get(XML_LANG) == ("en" if version == "2.0" else "")
        wrapper = tree.xpath('//*[@id="wrapper"]')[0]
        assert wrapper.tag == f"{{{NS['h']}}}div" and wrapper.get("class") == "chapter-wrap"
        assert tree.xpath("//h:span/@xml:lang", namespaces=NS) == ["fr"]
        assert b"<!--keep this-->" in new.read("EPUB/chapter.xhtml")
        assert b"<?keep value?>" in new.read("EPUB/chapter.xhtml")
    assert markup_preserved(source, artifact, before.inspection, plan.markup)
    assert sha256_file(config.root / "originals" / f"{before.book_id}.epub") == before.book_id
    assert apply(plan, config) == after
    assert revalidate(before.book_id, config).repair_changes == after.repair_changes


@pytest.mark.parametrize(
    "root", [b'xml:lang="fr" lang=""', b'xml:lang="en_US"', b'xml:lang="fr"', b""]
)
def test_nonempty_missing_or_conflicting_root_languages_are_not_changed(tmp_path, config, root):
    files = book_files()
    for name in ("EPUB/chapter.xhtml", "EPUB/notes.xhtml", "EPUB/nav.xhtml"):
        files[name] = files[name].replace(b'xml:lang="en"', root)
    _, _, plan, _ = setup(tmp_path, config, files=files)
    assert plan.actions == []


@pytest.mark.parametrize("language", [b"en-US", b"zh-Hant", b"pt-BR"])
def test_declared_language_is_used_without_language_detection(tmp_path, config, language):
    files = book_files("2.0")
    files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
        b"<dc:language>en</dc:language>", b"<dc:language>" + language + b"</dc:language>"
    )
    files["EPUB/chapter.xhtml"] = files["EPUB/chapter.xhtml"].replace(
        b'xml:lang="en"', b'xml:lang=""'
    )
    _, _, plan, _ = setup(tmp_path, config, files=files)
    assert len(plan.markup) == 1 and plan.markup[0].after == language.decode()
    assert apply(plan, config).state == "VALIDATED"


@pytest.mark.parametrize("declaration", [b"en_US", b"", b"en</dc:language><dc:language>fr"])
def test_uncertain_package_language_never_fills_empty_attributes(tmp_path, config, declaration):
    files = book_files("2.0")
    files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
        b"<dc:language>en</dc:language>", b"<dc:language>" + declaration + b"</dc:language>"
    )
    files["EPUB/chapter.xhtml"] = files["EPUB/chapter.xhtml"].replace(
        b'xml:lang="en"', b'lang="" xml:lang=""'
    )
    _, _, plan, _ = setup(tmp_path, config, files=files)
    assert plan.actions == []


@pytest.mark.parametrize(
    "wrapper",
    [
        b'<p>Prose<h1 id="start">Heading</h1></p>',
        b'<p><h1 id="start">Heading</h1>After</p>',
        b'<p><span>Prose</span><h1 id="start">Heading</h1></p>',
        b'<p><table id="start"><tr><td>Cell</td></tr></table></p>',
        b'<p id="start"><em>Ordinary prose</em></p>',
        b'<span><p><h1 id="start">Heading</h1></p></span>',
    ],
)
def test_mixed_prose_other_blocks_and_uncertain_parents_are_not_rewritten(
    tmp_path, config, wrapper
):
    files = book_files()
    files["EPUB/chapter.xhtml"] = files["EPUB/chapter.xhtml"].replace(
        b'<h1 id="start">At the Observatory</h1>', wrapper
    )
    _, _, plan, _ = setup(tmp_path, config, files=files)
    assert plan.actions == []


@pytest.mark.parametrize(
    "limit", ["MAX_DOCUMENTS", "MAX_DOCUMENT_BYTES", "MAX_ELEMENTS", "MAX_EDITS"]
)
def test_limits_do_not_offer_partial_repairs(tmp_path, config, monkeypatch, limit):
    monkeypatch.setattr("emend.xhtml_repair." + limit, 1)
    _, _, plan, _ = setup(tmp_path, config)
    assert plan.actions == []


@pytest.mark.parametrize(
    "mutation", ["prose", "attribute", "style", "unplanned_tag", "timestamp", "extra"]
)
def test_independent_preservation_refuses_unapproved_changes(
    tmp_path, config, monkeypatch, mutation
):
    import emend.repairs as repairs

    _, before, plan, _ = setup(tmp_path, config, "3.0")
    real = repairs.rewrite_markup

    def rewrite(source, candidate, inspection, edits):
        real(source, candidate, inspection, edits)
        with zipfile.ZipFile(candidate) as archive:
            infos = archive.infolist()
            data = {i.filename: archive.read(i) for i in infos}
        if mutation == "prose":
            data["EPUB/chapter.xhtml"] = data["EPUB/chapter.xhtml"].replace(b"At dusk", b"At dawn")
        elif mutation == "attribute":
            data["EPUB/chapter.xhtml"] = data["EPUB/chapter.xhtml"].replace(
                b'id="wrapper"', b'id="different"'
            )
        elif mutation == "style":
            data["EPUB/style.css"] += b"p{color:red}"
        elif mutation == "unplanned_tag":
            data["EPUB/notes.xhtml"] = (
                data["EPUB/notes.xhtml"].replace(b"<h1>", b"<h2>").replace(b"</h1>", b"</h2>")
            )
        elif mutation == "timestamp":
            from emend.navigation_repair import serialize

            tree = parse_xml(data["EPUB/package.opf"])
            tree.xpath("opf:metadata/opf:meta[@property='dcterms:modified']", namespaces=NS)[
                0
            ].text = "invalid"
            data["EPUB/package.opf"] = serialize(tree)
        with zipfile.ZipFile(candidate, "w") as archive:
            for info in infos:
                archive.writestr(copy.copy(info), data[info.filename])
            if mutation == "extra":
                archive.writestr("unexpected.bin", b"unapproved")

    monkeypatch.setattr(repairs, "rewrite_markup", rewrite)
    with pytest.raises(RepairRejectedError):
        apply(plan, config)
    assert get_report(Store(config.root), before.book_id) == before


@pytest.mark.parametrize(
    "failure", ["validator", "unavailable", "database", "report", "required_ace"]
)
def test_rejected_markup_repair_retains_previous_artifact(tmp_path, config, monkeypatch, failure):
    import emend.repairs as repairs

    _, before, plan, _ = setup(tmp_path, config)
    artifact = config.root / "curated" / f"{before.book_id}.epub"
    prior = artifact.read_bytes()
    if failure == "validator":
        config.tools.epubcheck.write_text(
            config.tools.epubcheck.read_text().replace(
                "'messages': []", "'messages': [{'severity':'ERROR'}]"
            )
        )
    elif failure == "unavailable":
        config.tools.epubcheck = tmp_path / "missing"
    elif failure == "required_ace":
        config.quality.require_accessibility_audit = True
        plan, _ = prepare_repairs(before.book_id, config)
    else:

        def fail(*args, **kwargs):
            raise OSError("injected failure")

        if failure == "database":
            monkeypatch.setattr(Store, "transition", fail)
        else:
            monkeypatch.setattr(repairs, "write_reports", fail)
    with pytest.raises((RepairRejectedError, OSError)):
        apply(plan, config)
    assert artifact.read_bytes() == prior
    assert get_report(Store(config.root), before.book_id) == before


def test_markup_approval_and_stale_plan_are_rejected(tmp_path, config):
    _, before, plan, _ = setup(tmp_path, config)
    with pytest.raises(ValueError, match="Explicitly approve"):
        apply_repairs(before.book_id, config, plan_id=plan.id, approve=["container.mimetype"])
    revalidate(before.book_id, config)
    with pytest.raises(ValueError, match="stale"):
        apply(plan, config)


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_official_epubcheck_rejects_source_and_accepts_engine_repair(tmp_path, config, version):
    jar = os.environ.get("EMEND_TEST_EPUBCHECK_JAR")
    if not jar:
        pytest.skip("Set EMEND_TEST_EPUBCHECK_JAR for official validation")
    config.tools.epubcheck_jar = Path(jar)
    _, before, plan, _ = setup(tmp_path, config, version)
    assert before.state == "FAILED"
    evidence = next(g for g in before.gates if g.name == "epubcheck").evidence
    messages = json.loads(Path(evidence).read_text())["messages"]
    if version == "2.0":
        assert any('attribute "xml:lang"' in m["message"] for m in messages)
    assert any('element "h1"' in m["message"] for m in messages)
    after = apply(plan, config)
    assert after.state == "VALIDATED"
    assert next(g for g in after.gates if g.name == "epubcheck").status == "passed"


def test_epub3_empty_languages_are_valid_and_not_offered_for_repair(tmp_path, config):
    files = book_files()
    files["EPUB/chapter.xhtml"] = files["EPUB/chapter.xhtml"].replace(
        b'xml:lang="en"', b'lang="" xml:lang=""'
    )
    _, _, plan, _ = setup(tmp_path, config, files=files)
    assert plan.actions == []


def test_browser_rerun_replaces_old_empty_plan_with_markup_fix_then_download(
    workspace, tmp_path, browser_config, monkeypatch
):
    from playwright.sync_api import sync_playwright

    jar = os.environ.get("EMEND_TEST_EPUBCHECK_JAR")
    if not jar:
        pytest.skip("Set EMEND_TEST_EPUBCHECK_JAR for official validation")
    browser_config.tools.epubcheck_jar = Path(jar)
    source = write_epub(tmp_path / "markup.epub", markup_files())
    before = ingest(source, browser_config)
    assert before.state == "FAILED"
    # Simulate a saved "no supported repair" scan from the earlier catalog.
    with monkeypatch.context() as old_catalog:
        old_catalog.setattr("emend.repairs.discover_markup", lambda *args: [])
        old_plan, _ = prepare_repairs(before.book_id, browser_config)
        assert old_plan.actions == []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=str(browser_config.tools.chromium)
            if browser_config.tools.chromium
            else None
        )
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        page.goto(workspace.entry_url)
        page.goto(f"http://127.0.0.1:{workspace.server_port}/books/{before.book_id}")
        # The stale empty scan is shown as tried; a fresh run finds and applies the repair.
        assert page.get_by_text("No safe repair", exact=True).first.is_visible()
        for width in (1280, 390):
            page.set_viewport_size({"width": width, "height": 1000})
            page.evaluate("document.fonts.ready")
            assert page.evaluate("document.documentElement.scrollWidth") == width
            page.screenshot(path=str(tmp_path / f"markup-repair-{width}.png"), full_page=True)
        page.set_viewport_size({"width": 1280, "height": 1000})
        page.get_by_role("button", name="Run everything again", exact=True).click()
        page.get_by_role("link", name="Download EPUB", exact=True).wait_for(timeout=120000)
        assert page.get_by_text("Checked and ready to send", exact=True).is_visible()
        page.get_by_role("link", name="Repair").first.click()
        assert page.get_by_text("Fix HTML markup", exact=True).is_visible()
        after = get_report(Store(browser_config.root), before.book_id)
        with page.expect_download() as event:
            page.get_by_role("link", name="Download EPUB", exact=True).click()
        target = tmp_path / "downloaded.epub"
        event.value.save_as(target)
        assert sha256_file(target) == after.artifact_sha256 != sha256_file(source)
        assert after.repair_changes[-1].action == ACTION
        browser.close()
