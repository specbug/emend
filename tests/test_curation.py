import json
import os
import zipfile
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from emend.cli import app
from emend.curation import apply_proposals
from emend.inspect import NS, parse_xml, sha256_file
from emend.pipeline import get_report, ingest
from emend.publish import publish
from emend.research.service import load_plan, research
from emend.storage import Store
from tests.epub_factory import book_files, write_epub
from tests.test_research import ISBN, evidence_client


def setup_book(tmp_path, config, version="3.0", extra=b""):
    files = book_files(version)
    files["EPUB/package.opf"] = (
        files["EPUB/package.opf"]
        .replace(b"<dc:publisher>Emend</dc:publisher>", b"")
        .replace(
            b"</metadata>",
            f"<dc:identifier>urn:isbn:{ISBN}</dc:identifier>".encode() + extra + b"</metadata>",
        )
    )
    report = ingest(write_epub(tmp_path / "book.epub", files), config)
    assert report.state == "VALIDATED"
    plan = research(report.book_id, config, client=evidence_client(tmp_path))
    return report, plan


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_apply_preserves_resources_and_original_and_is_retryable(tmp_path, config, version):
    before, plan = setup_book(tmp_path, config, version)
    after = apply_proposals(before.book_id, config, safe=True)
    assert after.state == "VALIDATED" and after.artifact_sha256 != before.artifact_sha256
    assert after.inspection.metadata["publisher"] == ["Observatory Press"]
    assert after.inspection.metadata["date"] == ["2020-03-04"]
    assert after.inspection.spine_text_sha256 == before.inspection.spine_text_sha256
    original = config.root / "originals" / f"{before.book_id}.epub"
    curated = config.root / "curated" / original.name
    assert sha256_file(original) == before.book_id
    with zipfile.ZipFile(original) as old, zipfile.ZipFile(curated) as new:
        assert old.namelist() == new.namelist()
        assert new.infolist()[0].compress_type == zipfile.ZIP_STORED
        for name in old.namelist():
            if name != "EPUB/package.opf":
                assert old.read(name) == new.read(name)
    assert apply_proposals(before.book_id, config, safe=True) == after
    assert len(list((config.root / "versions" / before.book_id).glob("*.epub"))) == 2
    assert load_plan(Store(config.root), before.book_id).id == plan.id
    with pytest.raises(ValueError, match="stale"):
        apply_proposals(before.book_id, config, approve=[plan.proposals[0].id])


def test_plan_tampering_and_policy_changes_are_rejected(tmp_path, config):
    before, plan = setup_book(tmp_path, config)
    config.metadata.cache_days = 10
    with pytest.raises(ValueError, match="Research policy"):
        apply_proposals(before.book_id, config, safe=True)
    config.metadata.cache_days = 7
    path = Path(Store(config.root).plan(before.book_id)["path"])
    path.write_text(path.read_text().replace("Observatory Press", "Unapproved Press"))
    with pytest.raises(ValueError, match="changed after"):
        apply_proposals(before.book_id, config, safe=True)


def test_existing_field_requires_explicit_selection_and_keeps_attributes(tmp_path, config):
    before, plan = setup_book(
        tmp_path, config, extra=b'<dc:publisher id="pub" xml:lang="en">Keep me</dc:publisher>'
    )
    publisher = next(p for p in plan.proposals if p.field == "publisher")
    assert publisher.decision == "review"
    after = apply_proposals(before.book_id, config, approve=[publisher.id])
    with zipfile.ZipFile(config.root / "curated" / f"{before.book_id}.epub") as archive:
        root = parse_xml(archive.read("EPUB/package.opf"))
        assert root.find("opf:metadata/dc:publisher", NS).get("id") == "pub"
    assert after.inspection.metadata["publisher"] == publisher.after
    assert "date" not in after.inspection.metadata or not after.inspection.metadata["date"]


def test_multivalued_metadata_is_not_collapsed(tmp_path, config):
    before, plan = setup_book(
        tmp_path, config, extra=b"<dc:publisher>One</dc:publisher><dc:publisher>Two</dc:publisher>"
    )
    publisher = next(p for p in plan.proposals if p.field == "publisher")
    with pytest.raises(ValueError, match="Multi-valued"):
        apply_proposals(before.book_id, config, approve=[publisher.id])
    assert get_report(Store(config.root), before.book_id) == before


def test_validator_failure_retains_previous_artifact_and_report(tmp_path, config):
    before, _ = setup_book(tmp_path, config)
    config.tools.epubcheck = tmp_path / "missing"
    with pytest.raises(ValueError, match="candidate rejected"):
        apply_proposals(before.book_id, config, safe=True)
    assert get_report(Store(config.root), before.book_id) == before
    assert sha256_file(config.root / "curated" / f"{before.book_id}.epub") == before.book_id
    attempts = list((config.root / "reports" / before.book_id).glob("*/report.json"))
    assert len(attempts) == 2
    assert any(json.loads(p.read_text())["state"] == "REVIEW_REQUIRED" for p in attempts)


def test_unplanned_resource_change_is_rejected(tmp_path, config, monkeypatch):
    import emend.curation as curation

    before, _ = setup_book(tmp_path, config)
    real_edit = curation.edit_metadata

    def bad_edit(source, target, package, proposals):
        digest = real_edit(source, target, package, proposals)
        with zipfile.ZipFile(target) as archive:
            contents = {name: archive.read(name) for name in archive.namelist()}
        contents["EPUB/style.css"] += b" p {display:none}"
        write_epub(target, contents)
        return digest

    monkeypatch.setattr(curation, "edit_metadata", bad_edit)
    with pytest.raises(ValueError, match="candidate rejected"):
        apply_proposals(before.book_id, config, safe=True)
    assert get_report(Store(config.root), before.book_id) == before


def test_new_revision_publishes_once_per_destination(tmp_path, fake_publishers):
    config = fake_publishers
    before, _ = setup_book(tmp_path, config)
    assert publish(before.book_id, ["calibre", "drive"], config)["drive"]["status"] == "published"
    after = apply_proposals(before.book_id, config, safe=True)
    assert publish(before.book_id, ["calibre", "drive"], config)["drive"]["status"] == "published"
    assert (
        publish(before.book_id, ["calibre", "drive"], config)["calibre"]["status"]
        == "already_published"
    )
    rows = json.loads((config.publish.calibre_library / "fake-published.json").read_text())
    assert len(rows) == 2
    assert {r["identifiers"]["emend"] for r in rows} == {before.book_id, after.artifact_sha256}
    assert (tmp_path / "fake-drive" / "curated" / f"{after.artifact_sha256}.epub").exists()


def test_cli_review_apply_and_reset(tmp_path, config):
    before, plan = setup_book(tmp_path, config)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    runner = CliRunner()
    args = [before.book_id[:12], "--config", str(path)]
    result = runner.invoke(app, ["proposals", *args, "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["id"] == plan.id
    result = runner.invoke(app, ["apply", *args, "--safe"])
    assert result.exit_code == 0, result.output
    assert "Observatory Press" in result.output
    result = runner.invoke(app, ["reset-original", *args])
    assert result.exit_code == 0, result.output
    assert get_report(Store(config.root), before.book_id).artifact_sha256 == before.book_id
    assert (config.root / "research" / before.book_id / f"{plan.id}.json").exists()


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_edited_epubs_pass_official_epubcheck(tmp_path, config, version):
    jar = os.environ.get("EMEND_TEST_EPUBCHECK_JAR")
    if not jar:
        pytest.skip("Set EMEND_TEST_EPUBCHECK_JAR to run the official EPUBCheck integration")
    config.tools.epubcheck = None
    config.tools.epubcheck_jar = Path(jar)
    before, _ = setup_book(tmp_path, config, version)
    after = apply_proposals(before.book_id, config, safe=True)
    assert next(g for g in after.gates if g.name == "epubcheck").status == "passed"
    assert after.inspection.text_sha256 == before.inspection.text_sha256


def test_unselected_package_changes_are_rejected(tmp_path, config, monkeypatch):
    import hashlib

    import emend.curation as curation

    before, _ = setup_book(tmp_path, config)
    real_edit = curation.edit_metadata

    def bad_edit(source, target, package, proposals):
        real_edit(source, target, package, proposals)
        with zipfile.ZipFile(target) as archive:
            contents = {name: archive.read(name) for name in archive.namelist()}
        contents[package] = contents[package].replace(b"Emend Test Corpus", b"Wrong Author")
        write_epub(target, contents)
        return hashlib.sha256(contents[package]).hexdigest()

    monkeypatch.setattr(curation, "edit_metadata", bad_edit)
    with pytest.raises(ValueError, match="candidate rejected"):
        apply_proposals(before.book_id, config, safe=True)
    assert get_report(Store(config.root), before.book_id) == before


def test_database_failure_rolls_back_swapped_artifact(tmp_path, config, monkeypatch):
    before, _ = setup_book(tmp_path, config)

    def failed_transition(*args, **kwargs):
        raise OSError("database unavailable")

    monkeypatch.setattr(Store, "transition", failed_transition)
    with pytest.raises(OSError, match="database unavailable"):
        apply_proposals(before.book_id, config, safe=True)
    assert get_report(Store(config.root), before.book_id) == before
    assert sha256_file(config.root / "curated" / f"{before.book_id}.epub") == before.book_id


def test_research_failure_keeps_book_approved_and_records_missing_evidence(tmp_path, config):
    before, _ = setup_book(tmp_path, config)
    plan = research(before.book_id, config, offline=True)
    assert len(plan.provider_errors) == 2 and not plan.proposals
    assert get_report(Store(config.root), before.book_id) == before
    with pytest.raises(ValueError, match="No proposals selected"):
        apply_proposals(before.book_id, config, safe=True)
