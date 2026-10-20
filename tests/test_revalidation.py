import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import emend.revalidation as validation
from emend.cli import app
from emend.cover_apply import apply_cover
from emend.covers import cover_review
from emend.curation import apply_proposals
from emend.library import library_snapshot
from emend.models import Gate
from emend.pipeline import get_report, ingest
from emend.publish import publish
from emend.research.discovery import current_source
from emend.research.service import research
from emend.revalidation import revalidate
from emend.storage import Store
from emend.visual import load_visual_review, visual_review
from tests.epub_factory import book_files, write_epub
from tests.test_accessibility import accessible_book, ace_report, fake_ace
from tests.test_covers import candidate
from tests.test_curation import setup_book


def curated(tmp_path, config, version="3.0"):
    before, _ = setup_book(tmp_path, config, version, extra=b'<meta name="cover" content="image"/>')
    apply_proposals(before.book_id, config, safe=True)
    review = cover_review(before.book_id, config, image=candidate(tmp_path))
    return apply_cover(before.book_id, config, review_id=review.id, approve=review.candidates[0].id)


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_revalidation_retains_metadata_cover_history_and_exact_bytes(tmp_path, config, version):
    before = curated(tmp_path, config, version)
    store = Store(config.root)
    row = store.resolve(before.book_id)
    artifact = Path(row["artifact_path"])
    original = config.root / "originals" / artifact.name
    old_report = Path(row["report_path"]).read_bytes()
    old_bytes, original_bytes = artifact.read_bytes(), original.read_bytes()
    after = revalidate(before.book_id[:12], config)
    assert after.state == "VALIDATED" and after.revalidation
    assert after.artifact_sha256 == before.artifact_sha256 != before.book_id
    for field in (
        "metadata_changes",
        "cover_changes",
        "changes",
        "research_plan_id",
        "research_plan_sha256",
        "applied_proposals",
        "cover_review_id",
        "approved_cover_id",
    ):
        assert getattr(after, field) == getattr(before, field)
    assert after.inspection == before.inspection
    assert after.parent_report_path == row["report_path"]
    assert after.parent_artifact_sha256 == before.artifact_sha256
    assert artifact.read_bytes() == old_bytes and original.read_bytes() == original_bytes
    assert Path(row["report_path"]).read_bytes() == old_report
    assert get_report(store, before.book_id) == after
    assert revalidate(before.book_id, config).artifact_sha256 == before.artifact_sha256


def test_missing_validator_revokes_approval_and_retries_same_curated_book(
    tmp_path, fake_publishers
):
    config = fake_publishers
    before = curated(tmp_path, config)
    tool = config.tools.epubcheck
    config.tools.epubcheck = tmp_path / "missing"
    after = revalidate(before.book_id, config)
    assert after.state == "REVIEW_REQUIRED" and after.artifact_sha256 == before.artifact_sha256
    row, source, digest = current_source(Store(config.root), before.book_id, config)
    assert source == Path(row["artifact_path"]) and digest == before.artifact_sha256
    assert library_snapshot(config).books[0].artifact_sha256 == before.artifact_sha256
    with pytest.raises(ValueError, match="required gates"):
        publish(before.book_id, ["calibre"], config)
    with pytest.raises(ValueError, match="validated book"):
        apply_proposals(before.book_id, config, safe=True)
    # Advisory research continues to inspect the retained edited bytes, not the original.
    plan = research(before.book_id, config, offline=True)
    assert plan.artifact_sha256 == before.artifact_sha256
    config.tools.epubcheck = tool
    recovered = revalidate(before.book_id, config)
    assert recovered.state == "VALIDATED" and recovered.artifact_sha256 == before.artifact_sha256
    assert recovered.cover_changes == before.cover_changes


@pytest.mark.parametrize("required", [True, False])
def test_new_accessibility_policy_and_findings_keep_edits(tmp_path, config, required):
    before = curated(tmp_path, config)
    fake_ace(config, tmp_path, ace_report())
    config.quality.require_accessibility_audit = required
    result = revalidate(before.book_id, config)
    assert result.state == ("FAILED" if required else "VALIDATED")
    gate = next(g for g in result.gates if g.name == "accessibility")
    assert gate.accessibility.failed == 1
    assert result.artifact_sha256 == before.artifact_sha256
    assert result.inspection.metadata == before.inspection.metadata
    assert result.policy_sha256 == config.policy_hash()


def test_old_engine_can_be_revalidated_without_duplicate_publication(tmp_path, fake_publishers):
    config = fake_publishers
    before = curated(tmp_path, config)
    assert publish(before.book_id, ["calibre", "drive"], config)["calibre"]["status"] == "published"
    store = Store(config.root)
    path = Path(store.resolve(before.book_id)["report_path"])
    data = json.loads(path.read_text())
    data["engine_version"] = "old-engine"
    path.write_text(json.dumps(data))
    config.limits.tool_timeout_seconds += 1
    with pytest.raises(ValueError, match="emend revalidate"):
        publish(before.book_id, ["calibre"], config)
    after = revalidate(before.book_id, config)
    assert after.state == "VALIDATED" and after.artifact_sha256 == before.artifact_sha256
    assert (
        publish(before.book_id, ["calibre", "drive"], config)["calibre"]["status"]
        == "already_published"
    )
    assert (
        len(json.loads((config.publish.calibre_library / "fake-published.json").read_text())) == 1
    )


def test_failed_initial_ingestion_can_pass_with_original_identity(epub, config, tmp_path):
    tool = config.tools.epubcheck
    config.tools.epubcheck = tmp_path / "missing"
    before = ingest(epub, config)
    assert before.state == "REVIEW_REQUIRED"
    assert revalidate(before.book_id, config).state == "REVIEW_REQUIRED"
    config.tools.epubcheck = tool
    after = revalidate(before.book_id, config)
    assert after.state == "VALIDATED" and after.artifact_sha256 == before.book_id
    assert (
        Path(Store(config.root).resolve(before.book_id)["artifact_path"]).read_bytes()
        == epub.read_bytes()
    )


def test_structurally_broken_original_skips_external_tools(tmp_path, config, monkeypatch):
    files = book_files()
    del files["EPUB/observatory.png"]
    before = ingest(write_epub(tmp_path / "broken.epub", files), config)

    def unexpected(*args):
        pytest.fail("External validation must not run after structural failure")

    monkeypatch.setattr(validation, "validate_epubcheck", unexpected)
    after = revalidate(before.book_id, config)
    assert after.state == "FAILED"
    assert next(g.status for g in after.gates if g.name == "epubcheck") == "skipped"


@pytest.mark.parametrize("which", ["originals", "curated"])
def test_tampered_or_missing_current_input_is_never_adopted(tmp_path, config, which):
    before = curated(tmp_path, config)
    path = config.root / which / f"{before.book_id}.epub"
    path.chmod(0o644)
    path.write_bytes(b"unapproved bytes")
    with pytest.raises(ValueError, match="hash mismatch"):
        revalidate(before.book_id, config)
    assert get_report(Store(config.root), before.book_id) == before
    path.unlink()
    with pytest.raises(OSError):
        revalidate(before.book_id, config)


def test_validator_only_receives_snapshot_and_mutation_blocks_approval(
    tmp_path, config, monkeypatch
):
    before = curated(tmp_path, config)
    artifact = Path(Store(config.root).resolve(before.book_id)["artifact_path"])
    saved = artifact.read_bytes()

    def corrupt(epub, directory, settings):
        assert epub != artifact and ".staging" in epub.parts
        assert Store(config.root).resolve(before.book_id)["state"] == "REVIEW_REQUIRED"
        epub.write_bytes(b"changed by validator")
        return Gate(name="epubcheck", required=True, status="passed", detail="Bad validator")

    monkeypatch.setattr(validation, "validate_epubcheck", corrupt)
    after = revalidate(before.book_id, config)
    assert after.state == "FAILED"
    assert next(g.status for g in after.gates if g.name == "preservation") == "failed"
    assert artifact.read_bytes() == saved


@pytest.mark.parametrize("point", ["inspection", "final_report", "final_database"])
def test_interrupted_writes_leave_retryable_blocked_state(tmp_path, config, monkeypatch, point):
    before = curated(tmp_path, config)
    with monkeypatch.context() as patch:

        def fail(*args, **kwargs):
            raise OSError("injected failure")

        if point == "inspection":
            patch.setattr(validation, "inspect_epub", fail)
        elif point == "final_report":
            real_write = validation.write_reports

            def write(report, directory):
                if directory.name != "pending":
                    fail()
                return real_write(report, directory)

            patch.setattr(validation, "write_reports", write)
        else:
            real_transition = Store.transition

            def transition(self, book_id, source, state, *args, **kwargs):
                if state == "VALIDATED":
                    fail()
                return real_transition(self, book_id, source, state, *args, **kwargs)

            patch.setattr(Store, "transition", transition)
        with pytest.raises(OSError, match="injected failure"):
            revalidate(before.book_id, config)
    pending = get_report(Store(config.root), before.book_id)
    assert pending.state == "REVIEW_REQUIRED" and pending.artifact_sha256 == before.artifact_sha256
    assert pending.cover_changes == before.cover_changes
    assert revalidate(before.book_id, config).state == "VALIDATED"


def test_process_termination_keeps_approval_revoked_and_releases_lock(tmp_path, config):
    before = curated(tmp_path, config)
    original_tool = config.tools.epubcheck
    marker = tmp_path / "validator-started"
    slow = tmp_path / "slow-epubcheck"
    slow.write_text(
        f"#!{sys.executable}\nimport time\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\ntime.sleep(60)\n"
    )
    slow.chmod(0o755)
    config.tools.epubcheck = slow
    settings = tmp_path / "config.yaml"
    settings.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    with (tmp_path / "process.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "emend.cli", "revalidate", before.book_id, "-c", str(settings)],
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 15
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            assert marker.exists(), (tmp_path / "process.log").read_text()
            assert get_report(Store(config.root), before.book_id).state == "REVIEW_REQUIRED"
        finally:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
    assert get_report(Store(config.root), before.book_id).artifact_sha256 == before.artifact_sha256
    config.tools.epubcheck = original_tool
    assert revalidate(before.book_id, config).state == "VALIDATED"


def test_cli_returns_json_and_nonzero_for_blocked_revalidation(epub, config, tmp_path):
    before = ingest(epub, config)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    args = ["revalidate", before.book_id[:12], "--json", "-c", str(path)]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["artifact_sha256"] == before.book_id
    config.tools.epubcheck = tmp_path / "missing"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1
    assert json.loads(result.output)["state"] == "REVIEW_REQUIRED"


def test_visual_evidence_remains_current_after_failed_audit(epub, browser_config, tmp_path):
    config = browser_config
    before = ingest(epub, config)
    visual = visual_review(before.book_id, config, profiles=["compact"], max_documents=1)
    config.tools.epubcheck = tmp_path / "missing"
    after = revalidate(before.book_id, config)
    assert after.state == "REVIEW_REQUIRED"
    assert load_visual_review(Store(config.root), before.book_id) == visual
    book = library_snapshot(config).books[0]
    assert next(r.status for r in book.reviews if r.label == "Visual samples") == "current"


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_revalidation_of_curated_epub_with_official_checker(tmp_path, config, version):
    jar = os.environ.get("EMEND_TEST_EPUBCHECK_JAR")
    if not jar:
        pytest.skip("Set EMEND_TEST_EPUBCHECK_JAR for official EPUBCheck integration")
    before = curated(tmp_path, config, version)
    config.tools.epubcheck_jar = Path(jar)
    after = revalidate(before.book_id, config)
    assert after.state == "VALIDATED", after.model_dump_json()
    assert after.artifact_sha256 == before.artifact_sha256


@pytest.mark.parametrize("clean", [True, False])
def test_revalidate_with_official_ace(tmp_path, config, clean):
    tool = os.environ.get("EMEND_TEST_ACE")
    if not tool:
        pytest.skip("Set EMEND_TEST_ACE for official DAISY Ace integration")
    files = accessible_book()
    if not clean:
        files["EPUB/chapter.xhtml"] = files["EPUB/chapter.xhtml"].replace(
            b' alt="A simple green illustration of the observatory"', b""
        )
    before = ingest(write_epub(tmp_path / "ace.epub", files), config)
    config.tools.ace = Path(tool)
    config.quality.require_accessibility_audit = True
    result = revalidate(before.book_id, config)
    assert result.state == ("VALIDATED" if clean else "FAILED"), [
        g.model_dump() for g in result.gates if g.required and g.status != "passed"
    ]
    audit = next(g.accessibility for g in result.gates if g.name == "accessibility")
    assert audit is not None and audit.tool_version == "1.4.6"
    assert any(f.rule == "image-alt" for f in audit.findings) == (not clean)
    assert result.artifact_sha256 == before.artifact_sha256


def test_smaller_archive_limit_does_not_overwrite_approved_record(tmp_path, config):
    files = book_files()
    files["EPUB/large.bin"] = os.urandom(2 * 1024**2)
    before = ingest(write_epub(tmp_path / "large.epub", files), config)
    assert before.state == "VALIDATED"
    config.limits.max_archive_mb = 1
    with pytest.raises(ValueError, match="archive size limit"):
        revalidate(before.book_id, config)
    assert get_report(Store(config.root), before.book_id) == before
    assert not list((config.root / ".staging").iterdir())


def test_revalidation_respects_the_shared_operation_lock(epub, config):
    before = ingest(epub, config)
    with Store(config.root).locked(), pytest.raises(ValueError, match="Another Emend operation"):
        revalidate(before.book_id, config)
    assert get_report(Store(config.root), before.book_id) == before
