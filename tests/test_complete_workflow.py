"""Real tools across the complete local workflow; no personal books or accounts."""

import json
import os
import shutil
from pathlib import Path

import pytest
from PIL import Image

from emend.backup import backup_library, inventory, restore_backup, verify_backup
from emend.cover_create import create_cover
from emend.covers import cover_review
from emend.curation import apply_proposals
from emend.inspect import sha256_file
from emend.pipeline import ingest
from emend.publish import publish
from emend.reader_export import export_reader
from emend.reading_review import reading_review
from emend.recovery import recover
from emend.repairs import ACTION, apply_repairs, prepare_repairs
from emend.research.service import research
from emend.storage import Store
from emend.typography import TypographySettings, typography_trial
from emend.typography_apply import apply_typography
from emend.visual import visual_review
from tests.epub_factory import book_files
from tests.test_calibre_integration import rows
from tests.test_covers import candidate
from tests.test_repairs import damaged_epub
from tests.test_research import ISBN, evidence_client


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_complete_real_tool_workflow(tmp_path, real_calibre, browser_config, monkeypatch, version):
    config, run = real_calibre
    assert config is browser_config
    jar = os.environ.get("EMEND_TEST_EPUBCHECK_JAR")
    rclone = shutil.which("rclone")
    if not jar or not rclone:
        pytest.skip("Complete workflow requires official EPUBCheck and real rclone")
    config.tools.epubcheck = None
    config.tools.epubcheck_jar = Path(jar)
    files = book_files(version)
    files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
        b"<dc:publisher>Emend</dc:publisher>",
        f"<dc:identifier>urn:isbn:{ISBN}</dc:identifier>".encode(),
    )
    before = ingest(
        damaged_epub(tmp_path / "original.epub", version, files=files, extra=True), config
    )
    assert before.state == "FAILED"
    plan, _ = prepare_repairs(before.book_id, config)
    repaired = apply_repairs(before.book_id, config, plan_id=plan.id, approve=[ACTION])
    assert repaired.state == "VALIDATED"
    # Network-free bibliographic evidence, followed by actual package edits/checker.
    research(before.book_id, config, client=evidence_client(tmp_path))
    metadata = apply_proposals(before.book_id, config, safe=True)
    review = cover_review(
        before.book_id,
        config,
        image=candidate(tmp_path, (800, 1200)),
        source_note="Generated original workflow artwork",
    )
    cover = create_cover(
        before.book_id, config, review_id=review.id, approve=review.candidates[0].id
    )
    trial = typography_trial(
        before.book_id,
        config,
        TypographySettings(line_height=1.65, paragraph_gap=0.6),
        profiles=["compact"],
        max_documents=2,
    )
    assert trial.status == "complete"
    styled = apply_typography(
        before.book_id, config, trial_id=trial.id, approve=True, all_spine=True
    )
    assert styled.state == "VALIDATED" and styled.metadata_changes == metadata.metadata_changes
    assert (
        styled.cover_additions == cover.cover_additions
        and styled.repair_changes == repaired.repair_changes
    )
    assert styled.typography_changes and styled.inspection.spine[1:] == before.inspection.spine
    reading = reading_review(before.book_id, config)
    visual = visual_review(before.book_id, config, profiles=["compact"], max_documents=2)
    assert reading.artifact_sha256 == styled.artifact_sha256 and visual.status == "complete"
    current = config.root / "curated" / f"{before.book_id}.epub"
    current.chmod(0o600)
    current.write_bytes(b"injected interrupted activation")
    restored = recover(before.book_id, config, revision=styled.artifact_sha256, approve=True)
    assert restored.state == "VALIDATED"
    assert restored.metadata_changes == styled.metadata_changes
    assert restored.cover_additions == styled.cover_additions
    assert restored.typography_changes == styled.typography_changes
    assert restored.repair_changes == styled.repair_changes
    assert restored.recovery_changes
    reader = tmp_path / "Mounted reader"
    reader.mkdir()
    delivered = export_reader(before.book_id, reader, config)
    assert delivered["status"] == "exported"
    assert export_reader(before.book_id, reader, config)["status"] == "already_exported"
    assert len(list(reader.glob("*.epub"))) == 1
    assert sha256_file(next(reader.glob("*.epub"))) == styled.artifact_sha256
    result = publish(before.book_id, ["calibre"], config)
    assert result["calibre"]["status"] == "published", result
    record = rows(run, config.publish.calibre_library)[0]
    with Image.open(record["cover"]) as image:
        assert image.size == (800, 1200)
    book = result["calibre"]["receipt"]["calibre_id"]
    helper = Path(__file__).with_name("calibre_library.py")
    run(
        "calibredb",
        "add_custom_column",
        "reading_status",
        "Reading status",
        "text",
        "--with-library",
        config.publish.calibre_library,
    )
    run(
        "calibredb",
        "set_custom",
        "reading_status",
        book,
        "Read — annotated",
        "--with-library",
        config.publish.calibre_library,
    )
    rich = json.loads(
        run("calibre-debug", "-e", helper, "--", "enrich", config.publish.calibre_library, book)
    )
    run("calibredb", "backup_metadata", "--all", "--with-library", config.publish.calibre_library)
    baseline = inventory(config.publish.calibre_library)
    rclone_config = tmp_path / "rclone.conf"
    rclone_config.write_text("[disposable]\ntype = local\n")
    monkeypatch.setenv("RCLONE_CONFIG", str(rclone_config))
    config.tools.rclone = Path(rclone)
    config.publish.backup_remote = f"disposable:{tmp_path / 'remote-backups'}"
    backup = backup_library(config, calibre_closed=True, upload=True)
    assert backup["cloud_status"] == "uploaded"
    archive = tmp_path / "remote-backups" / Path(backup["archive"]).name
    assert verify_backup(archive)["files"] == baseline
    destination = restore_backup(archive, tmp_path / "Restored rich library")
    assert inventory(destination) == baseline
    assert (
        json.loads(run("calibre-debug", "-e", helper, "--", "inspect", destination, book)) == rich
    )
    assert len(rows(run, destination)) == 1
    assert sha256_file(config.root / "originals" / current.name) == before.book_id
    assert Store(config.root).resolve(before.book_id)["state"] == "PUBLISHED"


def test_real_browser_missing_cover_and_recovery_at_phone_and_desktop(
    workspace, tmp_path, browser_config
):
    from playwright.sync_api import sync_playwright

    from emend.pipeline import get_report
    from tests.test_cover_create import prepare

    config = browser_config
    before, review = prepare(tmp_path, config)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=str(config.tools.chromium) if config.tools.chromium else None
        )
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        page.goto(workspace.entry_url)
        page.goto(f"http://127.0.0.1:{workspace.server_port}/books/{before.book_id}")
        for width in (1280, 390):
            page.set_viewport_size({"width": width, "height": 1000})
            page.evaluate("document.fonts.ready")
            assert page.evaluate("document.documentElement.scrollWidth") == width
            page.screenshot(path=str(tmp_path / f"missing-cover-{width}.png"))
        page.locator("summary").filter(has_text="Book details and cover").click()
        form = page.locator("form").filter(
            has=page.get_by_role("button", name="Create reviewed missing cover", exact=True)
        )
        form.locator('input[name="candidate"]').check()
        form.get_by_role("button", name="Create reviewed missing cover", exact=True).click()
        added = get_report(Store(config.root), before.book_id)
        assert added.cover_additions
        page.locator("summary").filter(has_text="Technical details and recovery").click()
        page.get_by_role("link", name="Recover original or retained revision", exact=True).click()
        assert page.get_by_role("heading", name="Recover a book", exact=True).is_visible()
        for width in (1280, 390):
            page.set_viewport_size({"width": width, "height": 1000})
            assert page.evaluate("document.documentElement.scrollWidth") == width
            page.screenshot(path=str(tmp_path / f"recovery-{width}.png"))
        page.locator(f'input[name="revision"][value="{before.book_id}"]').check()
        page.get_by_label("I approve replacing the current artifact with this revision.").check()
        page.get_by_role(
            "button", name="Restore selected revision and revalidate", exact=True
        ).click()
        restored = get_report(Store(config.root), before.book_id)
        assert restored.state == "VALIDATED" and restored.artifact_sha256 == before.book_id
        assert restored.recovery_changes and not restored.cover_additions
        browser.close()
