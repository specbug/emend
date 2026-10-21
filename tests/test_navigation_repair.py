import copy
import os
import zipfile
from pathlib import Path

import pytest

from emend.inspect import NS, parse_xml, sha256_file
from emend.navigation_repair import ACTION, navigation_preserved
from emend.pipeline import get_report, ingest
from emend.repairs import RepairRejectedError, apply_repairs, prepare_repairs
from emend.storage import Store
from tests.epub_factory import book_files, write_epub


def fixture_files(version="3.0", href=b"CHAPTER.xhtml#start"):
    files = book_files(version)
    nav = "EPUB/nav.xhtml" if version == "3.0" else "EPUB/toc.ncx"
    files[nav] = files[nav].replace(b"chapter.xhtml#start", href)
    files[nav] = files[nav].replace(b"?>", b"?><!--preserved comment--><?keep value?>", 1)
    return files, nav


def setup(tmp_path, config, version="3.0", **kwargs):
    files, nav = fixture_files(version, **kwargs)
    source = write_epub(tmp_path / "book.epub", files)
    before = ingest(source, config)
    assert before.state == "FAILED"
    plan, review = prepare_repairs(before.book_id, config)
    return source, before, plan, review, nav


def apply(plan, config, approve=None):
    return apply_repairs(plan.book_id, config, plan_id=plan.id, approve=approve or [ACTION])


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_reviewed_case_only_navigation_repair_preserves_content(tmp_path, config, version):
    source, before, plan, review, nav = setup(tmp_path, config, version)
    assert plan.actions == [ACTION] and len(plan.links) == 1
    link = plan.links[0]
    assert link.before == "CHAPTER.xhtml#start" and link.after == "chapter.xhtml#start"
    assert link.target == "EPUB/chapter.xhtml"
    assert link.before in review.read_text() and link.after in review.read_text()
    assert get_report(Store(config.root), before.book_id) == before
    after = apply(plan, config)
    artifact = config.root / "curated" / f"{before.book_id}.epub"
    assert after.state == "VALIDATED"
    assert after.repair_changes[-1].action == ACTION
    assert before.inspection.spine == after.inspection.spine
    assert before.inspection.text_sha256 == after.inspection.text_sha256
    assert before.inspection.spine_text_sha256 == after.inspection.spine_text_sha256
    assert before.inspection.metadata == after.inspection.metadata
    assert sha256_file(source) == before.book_id
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(artifact) as new:
        assert old.namelist() == new.namelist()
        for name in old.namelist():
            if name not in {nav, "EPUB/package.opf"}:
                assert old.read(name) == new.read(name)
        if version == "2.0":
            assert old.read("EPUB/package.opf") == new.read("EPUB/package.opf")
        else:
            opf = parse_xml(new.read("EPUB/package.opf"))
            assert opf.xpath(
                "opf:metadata/opf:meta[@property='dcterms:modified']/text()", namespaces=NS
            ) != ["2026-01-01T00:00:00Z"]
        assert b"<!--preserved comment-->" in new.read(nav)
        assert b"<?keep value?>" in new.read(nav)
    assert navigation_preserved(source, artifact, before.inspection, plan.links)
    assert apply(plan, config) == after


@pytest.mark.parametrize(
    "defect",
    [
        "ambiguous",
        "missing_fragment",
        "fragment_case",
        "unrelated",
        "query",
        "remote",
        "outside_nav",
        "not_spine",
        "duplicate_id",
        "too_many",
        "mixed",
        "missing_timestamp",
    ],
)
def test_uncertain_or_unsupported_navigation_never_gets_a_proposal(tmp_path, config, defect):
    files, nav = fixture_files()
    if defect == "ambiguous":
        files["EPUB/Chapter.xhtml"] = files["EPUB/chapter.xhtml"]
    elif defect in {"missing_fragment", "fragment_case"}:
        files[nav] = files[nav].replace(
            b"#start", b"#missing" if defect == "missing_fragment" else b"#START"
        )
    elif defect == "unrelated":
        files[nav] = files[nav].replace(b"CHAPTER.xhtml", b"absent.xhtml")
    elif defect == "query":
        files[nav] = files[nav].replace(b"CHAPTER.xhtml", b"CHAPTER.xhtml?query=1")
    elif defect == "remote":
        files[nav] = files[nav].replace(b"CHAPTER.xhtml", b"https://example.invalid/CHAPTER.xhtml")
    elif defect == "outside_nav":
        files[nav] = files[nav].replace(b"CHAPTER.xhtml", b"chapter.xhtml")
        files["EPUB/chapter.xhtml"] = files["EPUB/chapter.xhtml"].replace(
            b"notes.xhtml#note1", b"NOTES.xhtml#note1"
        )
    elif defect == "not_spine":
        files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
            b'<itemref idref="chapter"/>', b""
        )
    elif defect == "duplicate_id":
        files["EPUB/chapter.xhtml"] = files["EPUB/chapter.xhtml"].replace(
            b"<p>", b'<p id="start">', 1
        )
    elif defect == "too_many":
        files[nav] = files[nav].replace(
            b"</ol>", b'<li><a href="CHAPTER.xhtml#start">Repeated entry</a></li>' * 200 + b"</ol>"
        )
    elif defect == "mixed":
        files[nav] = files[nav].replace(b"notes.xhtml#note1", b"absent.xhtml#note1")
    else:
        files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
            b'<meta property="dcterms:modified">2026-01-01T00:00:00Z</meta>', b""
        )
    before = ingest(write_epub(tmp_path / "uncertain.epub", files), config)
    plan, _ = prepare_repairs(before.book_id, config)
    assert plan.actions == [] and plan.links == []
    assert get_report(Store(config.root), before.book_id) == before


@pytest.mark.parametrize("href", [b"CHAPTER.xhtml", b"./CHAPTER.xhtml#st%61rt"])
def test_fragment_absence_and_encoded_fragment_are_preserved(tmp_path, config, href):
    _, _, plan, _, _ = setup(tmp_path, config, href=href)
    assert plan.actions == [ACTION]
    assert plan.links[0].after == href.decode().replace("CHAPTER", "chapter").removeprefix("./")
    assert apply(plan, config).state == "VALIDATED"


def test_nested_navigation_and_percent_encoded_resource_path(tmp_path, config):
    files, nav = fixture_files(href=b"../CHAPTER%20ONE.xhtml#start")
    files["EPUB/navigation/nav.xhtml"] = files.pop(nav)
    files["EPUB/chapter one.xhtml"] = files.pop("EPUB/chapter.xhtml")
    files["EPUB/package.opf"] = (
        files["EPUB/package.opf"]
        .replace(b'href="nav.xhtml"', b'href="navigation/nav.xhtml"')
        .replace(b'href="chapter.xhtml"', b'href="chapter%20one.xhtml"')
    )
    files["EPUB/notes.xhtml"] = files["EPUB/notes.xhtml"].replace(
        b"chapter.xhtml#ref1", b"chapter%20one.xhtml#ref1"
    )
    files["EPUB/navigation/nav.xhtml"] = files["EPUB/navigation/nav.xhtml"].replace(
        b"notes.xhtml#note1", b"../notes.xhtml#note1"
    )
    before = ingest(write_epub(tmp_path / "nested.epub", files), config)
    plan, _ = prepare_repairs(before.book_id, config)
    assert plan.links[0].after == "../chapter%20one.xhtml#start"
    assert apply(plan, config).state == "VALIDATED"


@pytest.mark.parametrize(
    "mutation", ["label", "other_href", "css", "metadata", "add_entry", "prolog", "timestamp"]
)
def test_independent_preservation_rejects_unapproved_changes(
    tmp_path, config, monkeypatch, mutation
):
    import emend.repairs as repairs

    _, before, plan, _, nav = setup(tmp_path, config)
    real = repairs.rewrite_navigation

    def rewrite(source, candidate, inspection, links):
        real(source, candidate, inspection, links)
        with zipfile.ZipFile(candidate) as archive:
            infos = archive.infolist()
            data = {i.filename: archive.read(i) for i in infos}
        if mutation == "label":
            data[nav] = data[nav].replace(b"At the Observatory</a>", b"Wrong title</a>")
        elif mutation == "other_href":
            data[nav] = data[nav].replace(b"notes.xhtml#note1", b"notes.xhtml")
        elif mutation == "css":
            data["EPUB/style.css"] += b"p{color:red}"
        elif mutation == "metadata":
            data["EPUB/package.opf"] = data["EPUB/package.opf"].replace(
                b"<dc:title>The Quiet Observatory", b"<dc:title>Changed"
            )
        elif mutation == "timestamp":
            root = parse_xml(data["EPUB/package.opf"])
            root.xpath("opf:metadata/opf:meta[@property='dcterms:modified']", namespaces=NS)[
                0
            ].text = "invalid timestamp"
            from emend.navigation_repair import serialize

            data["EPUB/package.opf"] = serialize(root)
        elif mutation == "prolog":
            data[nav] = data[nav].replace(b"<?keep value?>", b"<?keep changed?>")
        with zipfile.ZipFile(candidate, "w") as archive:
            for info in infos:
                archive.writestr(copy.copy(info), data[info.filename])
            if mutation == "add_entry":
                archive.writestr("unexpected.bin", b"unapproved")

    monkeypatch.setattr(repairs, "rewrite_navigation", rewrite)
    with pytest.raises(RepairRejectedError):
        apply(plan, config)
    assert get_report(Store(config.root), before.book_id) == before
    assert not (config.root / "curated" / f"{before.book_id}.epub").exists()


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_official_epubcheck_navigation_repairs(tmp_path, config, version):
    jar = os.environ.get("EMEND_TEST_EPUBCHECK_JAR")
    if not jar:
        pytest.skip("Set EMEND_TEST_EPUBCHECK_JAR for official validation")
    config.tools.epubcheck_jar = Path(jar)
    _, _, plan, _, _ = setup(tmp_path, config, version)
    after = apply(plan, config)
    assert next(g.status for g in after.gates if g.name == "epubcheck") == "passed"


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_all_proposed_links_need_the_navigation_action_approval(tmp_path, config, version):
    files, nav = fixture_files(version)
    files[nav] = files[nav].replace(b"notes.xhtml#note1", b"NOTES.xhtml#note1")
    before = ingest(write_epub(tmp_path / "two.epub", files), config)
    plan, _ = prepare_repairs(before.book_id, config)
    assert len(plan.links) == 2
    with pytest.raises(ValueError, match="Explicitly approve"):
        apply(plan, config, approve=["container.mimetype"])
    after = apply(plan, config)
    assert after.state == "VALIDATED"
    assert "NOTES.xhtml#note1 → notes.xhtml#note1" in after.changes[-1]


def test_epub2_public_doctype_comments_and_processing_instructions_are_retained(tmp_path, config):
    files, nav = fixture_files("2.0")
    files[nav] = files[nav].replace(
        b"<ncx ",
        b'<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN" "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd"><ncx ',
        1,
    )
    before = ingest(write_epub(tmp_path / "doctype.epub", files), config)
    plan, _ = prepare_repairs(before.book_id, config)
    assert plan.actions == [ACTION]
    apply(plan, config)
    with zipfile.ZipFile(config.root / "curated" / f"{before.book_id}.epub") as archive:
        data = archive.read(nav)
        assert b"<!DOCTYPE ncx PUBLIC" in data
        assert b"<!--preserved comment-->" in data and b"<?keep value?>" in data


def test_noncanonicalizable_xml_is_not_offered(tmp_path, config):
    files, nav = fixture_files()
    files[nav] = files[nav].replace(
        b'<nav epub:type="toc"', b'<nav xmlns:odd="relative" epub:type="toc"'
    )
    before = ingest(write_epub(tmp_path / "relative-ns.epub", files), config)
    plan, _ = prepare_repairs(before.book_id, config)
    assert plan.actions == []


def test_navigation_cli_json_and_approval(tmp_path, config):
    import json

    import yaml
    from typer.testing import CliRunner

    from emend.cli import app

    _, before, _, _, _ = setup(tmp_path, config)
    settings = tmp_path / "config.yaml"
    settings.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    runner = CliRunner()
    result = runner.invoke(app, ["repair-plan", before.book_id, "--json", "-c", str(settings)])
    assert result.exit_code == 0, result.output
    plan = json.loads(result.output)
    assert plan["links"][0]["before"] == "CHAPTER.xhtml#start"
    result = runner.invoke(
        app,
        [
            "apply-repairs",
            before.book_id,
            "--plan",
            plan["id"],
            "--approve",
            ACTION,
            "--json",
            "-c",
            str(settings),
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["repair_changes"][-1]["action"] == ACTION
