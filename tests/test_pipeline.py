import json
import sys
from pathlib import Path

import pytest

from emend.pipeline import get_report, ingest
from emend.publish import publish
from emend.storage import Store
from tests.epub_factory import book_files, write_epub


def test_ingest_is_byte_preserving_and_retryable(epub, config):
    before = epub.read_bytes()
    first = ingest(epub, config)
    assert first.state == "VALIDATED"
    assert (config.root / "curated" / f"{first.book_id}.epub").read_bytes() == before
    assert (config.root / "originals" / f"{first.book_id}.epub").read_bytes() == before
    assert epub.read_bytes() == before
    second = ingest(epub, config)
    assert second.book_id == first.book_id
    store = Store(config.root)
    assert len(store.books()) == 1
    assert len(list((config.root / "reports" / first.book_id).iterdir())) == 2
    assert get_report(store, first.book_id[:12]).state == "VALIDATED"
    directory = Path(store.resolve(first.book_id)["report_path"]).parent
    assert (directory / "report.html").is_file()
    assert (directory / "epubcheck.json").is_file()


def test_missing_required_validator_is_not_passed(epub, config):
    config.tools.epubcheck = config.root / "missing-tool"
    report = ingest(epub, config)
    assert report.state == "REVIEW_REQUIRED"
    assert next(g for g in report.gates if g.name == "epubcheck").status == "unavailable"
    assert not list((config.root / "curated").iterdir())
    with pytest.raises(ValueError, match="has not passed"):
        publish(report.book_id, ["calibre"], config)


def test_required_accessibility_blocks_when_unavailable(epub, config):
    config.quality.require_accessibility_audit = True
    report = ingest(epub, config)
    assert report.state == "REVIEW_REQUIRED"


def test_malformed_input_preserved_but_never_curated(tmp_path, config):
    bad = tmp_path / "invalid.epub"
    bad.write_bytes(b"broken zip")
    report = ingest(bad, config)
    assert report.state == "FAILED"
    assert (config.root / "originals" / f"{report.book_id}.epub").read_bytes() == b"broken zip"
    assert not list((config.root / "curated").iterdir())


@pytest.mark.parametrize(
    ("contents", "exit_code"),
    [
        ({"messages": [{"severity": "ERROR", "message": "broken"}]}, 0),
        ({"messages": []}, 1),
        ({}, 0),
        ({"messages": "not-a-list"}, 0),
    ],
)
def test_validator_requires_both_usable_evidence_and_success(epub, config, contents, exit_code):
    config.tools.epubcheck.write_text(
        f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\nPath(sys.argv[sys.argv.index('--json')+1]).write_text(json.dumps({contents!r}))\nsys.exit({exit_code})\n"
    )
    report = ingest(epub, config)
    assert report.state == "FAILED"


def test_validator_timeout_produces_failure(epub, config):
    config.tools.epubcheck.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(5)\n")
    config.limits.tool_timeout_seconds = 1
    report = ingest(epub, config)
    assert report.state == "FAILED"
    assert "timeout" in next(g.detail for g in report.gates if g.name == "epubcheck")


def test_revalidation_failure_revokes_old_artifact(epub, config):
    report = ingest(epub, config)
    config.tools.epubcheck = config.root / "missing"
    assert ingest(epub, config).state == "REVIEW_REQUIRED"
    assert not (config.root / "curated" / f"{report.book_id}.epub").exists()


def test_html_report_escapes_book_metadata(tmp_path, config):
    files = book_files()
    files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
        b"The Quiet Observatory", b"&lt;script&gt;alert(1)&lt;/script&gt;"
    )
    report = ingest(write_epub(tmp_path / "x.epub", files), config)
    html = (
        Path(Store(config.root).resolve(report.book_id)["report_path"])
        .with_suffix(".html")
        .read_text()
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_corrupt_original_is_not_silently_replaced(epub, config):
    report = ingest(epub, config)
    original = config.root / "originals" / f"{report.book_id}.epub"
    original.chmod(0o600)
    original.write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="original hash mismatch"):
        ingest(epub, config)


def test_publication_is_gated_idempotent_and_verifies_hashes(epub, fake_publishers):
    config = fake_publishers
    report = ingest(epub, config)
    results = publish(report.book_id, ["calibre", "drive"], config)
    assert all(item["status"] == "published" for item in results.values())
    results = publish(report.book_id, ["calibre", "drive"], config)
    assert all(item["status"] == "already_published" for item in results.values())
    assert (
        len(json.loads((config.publish.calibre_library / "fake-published.json").read_text())) == 1
    )
    assert (
        config.root.parent / "fake-drive" / "curated" / f"{report.book_id}.epub"
    ).read_bytes() == epub.read_bytes()
    artifact = config.root / "curated" / f"{report.book_id}.epub"
    artifact.chmod(0o600)
    artifact.write_bytes(b"altered")
    with pytest.raises(ValueError, match="hash mismatch"):
        publish(report.book_id, ["drive"], config)


def test_policy_change_requires_revalidation(epub, fake_publishers):
    report = ingest(epub, fake_publishers)
    fake_publishers.quality.require_accessibility_audit = True
    with pytest.raises(ValueError, match="policy or engine changed"):
        publish(report.book_id, ["calibre"], fake_publishers)


def test_failed_destination_remains_retryable(epub, fake_publishers):
    config = fake_publishers
    report = ingest(epub, config)
    rclone = config.tools.rclone
    config.tools.rclone = config.root / "missing-rclone"
    first = publish(report.book_id, ["calibre", "drive"], config)
    assert first["calibre"]["status"] == "published"
    assert first["drive"]["status"] == "pending"
    config.tools.rclone = rclone
    retry = publish(report.book_id, ["calibre", "drive"], config)
    assert retry["calibre"]["status"] == "already_published"
    assert retry["drive"]["status"] == "published"


def test_calibre_reconciles_success_without_a_local_receipt(epub, fake_publishers):
    config = fake_publishers
    report = ingest(epub, config)
    publish(report.book_id, ["calibre"], config)
    store = Store(config.root)
    with store.db() as db:
        db.execute("DELETE FROM publications")
    publish(report.book_id, ["calibre"], config)
    assert (
        len(json.loads((config.publish.calibre_library / "fake-published.json").read_text())) == 1
    )


def test_changed_destination_creates_new_delivery(epub, fake_publishers):
    config = fake_publishers
    report = ingest(epub, config)
    publish(report.book_id, ["drive"], config)
    config.publish.drive_remote = "gdrive:new-folder"
    assert publish(report.book_id, ["drive"], config)["drive"]["status"] == "published"


def test_calibre_changed_format_does_not_receive_success_receipt(epub, fake_publishers):
    config = fake_publishers
    report = ingest(epub, config)
    publish(report.book_id, ["calibre"], config)
    stored = config.publish.calibre_library / "Imported" / f"{report.book_id}.epub"
    stored.write_bytes(b"changed by a library import plugin")
    with Store(config.root).db() as db:
        db.execute("DELETE FROM publications")
    result = publish(report.book_id, ["calibre"], config)
    assert result["calibre"]["status"] == "pending"
    assert "differs" in result["calibre"]["receipt"]["error"]


def test_unconfigured_target_does_not_block_other_target(epub, fake_publishers):
    config = fake_publishers
    report = ingest(epub, config)
    config.publish.drive_remote = None
    result = publish(report.book_id, ["drive", "calibre"], config)
    assert result["drive"]["status"] == "pending"
    assert result["calibre"]["status"] == "published"
