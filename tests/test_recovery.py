import os
from pathlib import Path

import pytest

from emend.cover_create import create_cover
from emend.inspect import sha256_file
from emend.models import Gate
from emend.pipeline import get_report, ingest
from emend.publish import publish
from emend.recovery import recover, recovery_options
from emend.revalidation import revalidate
from emend.storage import Store
from tests.epub_factory import write_epub
from tests.test_cover_create import prepare


def artifact(config, book):
    return config.root / "curated" / f"{book}.epub"


def corrupt(path):
    path.chmod(0o600)
    path.write_bytes(b"unknown interrupted bytes")


def test_original_recovery_quarantines_unknown_bytes_and_revalidates(tmp_path, config):
    before, review = prepare(tmp_path, config)
    edited = create_cover(
        before.book_id, config, review_id=review.id, approve=review.candidates[0].id
    )
    current = artifact(config, before.book_id)
    corrupt(current)
    result = recover(before.book_id, config, revision="original", approve=True)
    assert result.state == "VALIDATED" and result.artifact_sha256 == before.book_id
    assert sha256_file(current) == before.book_id
    assert not result.cover_additions  # History describes selected revision, not discarded edits.
    change = result.recovery_changes[-1]
    assert (
        change.original
        and Path(change.quarantine_path).read_bytes() == b"unknown interrupted bytes"
    )
    assert change.previous_report_path and result.parent_report_path
    assert sha256_file(config.root / "originals" / current.name) == before.book_id
    assert edited.artifact_sha256 in {c["sha256"] for c in recovery_options(before.book_id, config)}


def test_known_approved_revision_restores_its_edit_history(tmp_path, config):
    before, review = prepare(tmp_path, config)
    edited = create_cover(
        before.book_id, config, review_id=review.id, approve=review.candidates[0].id
    )
    recover(before.book_id, config, revision="original", approve=True)
    result = recover(before.book_id, config, revision=edited.artifact_sha256, approve=True)
    assert result.state == "VALIDATED" and result.cover_additions == edited.cover_additions
    assert sha256_file(artifact(config, before.book_id)) == edited.artifact_sha256
    assert not result.recovery_changes[-1].original


def test_interrupted_initial_import_without_report_can_recover(tmp_path, config):
    before = ingest(write_epub(tmp_path / "book.epub"), config)
    store = Store(config.root)
    store.transition(before.book_id, "book.epub", "IMPORTED", "Injected interrupted import")
    artifact(config, before.book_id).unlink()
    result = recover(before.book_id, config, revision="original", approve=True)
    assert result.state == "VALIDATED"
    assert result.artifact_sha256 == before.book_id


@pytest.mark.parametrize(
    "fault", ["quarantine", "copy", "after_copy", "revalidate", "final_report"]
)
def test_interruption_revokes_approval_and_allows_retry(tmp_path, config, monkeypatch, fault):
    import emend.recovery as module
    import emend.revalidation as validation

    before = ingest(write_epub(tmp_path / "book.epub"), config)
    real_copy, real_replace = module.atomic_copy, module.os.replace
    real_revalidate, real_write = module.revalidate, validation.write_reports

    def killed(*args, **kwargs):
        raise SystemExit("injected process termination")

    if fault == "quarantine":
        monkeypatch.setattr(module.os, "replace", killed)
    elif fault == "copy":
        monkeypatch.setattr(module, "atomic_copy", killed)
    elif fault == "after_copy":

        def copy_then_die(*args):
            real_copy(*args)
            killed()

        monkeypatch.setattr(module, "atomic_copy", copy_then_die)
    elif fault == "revalidate":
        monkeypatch.setattr(module, "revalidate", killed)
    else:

        def fail_final(report, directory):
            if report.state == "VALIDATED":
                killed()
            return real_write(report, directory)

        monkeypatch.setattr(validation, "write_reports", fail_final)
    with pytest.raises(SystemExit):
        recover(before.book_id, config, revision="original", approve=True)
    assert Store(config.root).resolve(before.book_id)["state"] == "REVIEW_REQUIRED"
    assert get_report(Store(config.root), before.book_id).state == "REVIEW_REQUIRED"
    with pytest.raises(ValueError, match="required gates"):
        publish(before.book_id, ["calibre"], config)
    assert sha256_file(config.root / "originals" / f"{before.book_id}.epub") == before.book_id
    monkeypatch.setattr(module.os, "replace", real_replace)
    monkeypatch.setattr(module, "atomic_copy", real_copy)
    monkeypatch.setattr(module, "revalidate", real_revalidate)
    monkeypatch.setattr(validation, "write_reports", real_write)
    assert recover(before.book_id, config, revision="original", approve=True).state == "VALIDATED"


@pytest.mark.parametrize("fault", ["approval", "unknown", "original", "version", "stale"])
def test_refusals_do_not_replace_current_artifact(tmp_path, config, fault):
    before, review = prepare(tmp_path, config)
    edited = create_cover(
        before.book_id, config, review_id=review.id, approve=review.candidates[0].id
    )
    kw = {"revision": "original", "approve": True}
    if fault == "approval":
        kw["approve"] = False
    elif fault == "unknown":
        unknown = config.root / "versions" / before.book_id / ("0" * 64 + ".epub")
        unknown.write_bytes(b"unapproved")
        kw["revision"] = "0" * 64
    elif fault == "original":
        corrupt(config.root / "originals" / f"{before.book_id}.epub")
    elif fault == "version":
        corrupt(config.root / "versions" / before.book_id / f"{edited.artifact_sha256}.epub")
        kw["revision"] = edited.artifact_sha256
    else:
        kw["expected_report_path"] = "outdated"
    with pytest.raises(ValueError):
        recover(before.book_id, config, **kw)
    assert get_report(Store(config.root), before.book_id) == edited
    assert sha256_file(artifact(config, before.book_id)) == edited.artifact_sha256


def test_failed_validation_never_reapproves_recovered_bytes(tmp_path, config, monkeypatch):
    import emend.revalidation as module

    before = ingest(write_epub(tmp_path / "book.epub"), config)
    monkeypatch.setattr(
        module,
        "validate_epubcheck",
        lambda *a: Gate(
            name="epubcheck", required=True, status="failed", detail="Injected validator failure"
        ),
    )
    result = recover(before.book_id, config, revision="original", approve=True)
    assert result.state == "FAILED"
    with pytest.raises(ValueError, match="required gates"):
        publish(before.book_id, ["calibre"], config)
    assert sha256_file(artifact(config, before.book_id)) == before.book_id


def test_restored_pending_copy_can_be_revalidated_without_reset(tmp_path, config, monkeypatch):
    import emend.recovery as module

    before = ingest(write_epub(tmp_path / "book.epub"), config)

    def die(*args):
        raise SystemExit()

    monkeypatch.setattr(module, "revalidate", die)
    with pytest.raises(SystemExit):
        recover(before.book_id, config, revision="original", approve=True)
    assert revalidate(before.book_id, config).state == "VALIDATED"


def test_workspace_can_recover_unavailable_book_and_rejects_stale_page(workspace, tmp_path, config):
    from tests.test_review_ui import fields, login, post, request

    before = ingest(write_epub(tmp_path / "book.epub"), config)
    cookie = login(workspace)
    saved = Store(config.root).resolve(before.book_id)
    corrupt(artifact(config, before.book_id))
    status, _, body = request(workspace, "/books/" + before.book_id, headers={"Cookie": cookie})
    assert status == 200 and "Recover a book" in body
    data = fields(
        workspace, before, "recover", revision="original", approve="yes", expected_report="stale"
    )
    assert post(workspace, cookie, data)[0] == 400
    data["expected_report"] = saved["report_path"]
    assert post(workspace, cookie, data)[0] == 303
    assert get_report(Store(config.root), before.book_id).state == "VALIDATED"


def test_recovery_uses_current_real_epubcheck(tmp_path, config):
    jar = os.environ.get("EMEND_TEST_EPUBCHECK_JAR")
    if not jar:
        pytest.skip("Official EPUBCheck not configured")
    config.tools.epubcheck = None
    config.tools.epubcheck_jar = Path(jar)
    before = ingest(write_epub(tmp_path / "book.epub"), config)
    corrupt(artifact(config, before.book_id))
    assert recover(before.book_id, config, revision="original", approve=True).state == "VALIDATED"


def test_process_kill_during_recovery_leaves_retryable_revoked_approval(tmp_path, config):
    import signal
    import subprocess
    import sys
    import time

    import yaml

    before = ingest(write_epub(tmp_path / "book.epub"), config)
    original_tool = config.tools.epubcheck
    marker = tmp_path / "validator-started"
    slow = tmp_path / "slow-epubcheck"
    slow.write_text(
        f"#!{sys.executable}\nimport time\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\ntime.sleep(60)\n"
    )
    slow.chmod(0o755)
    config.tools.epubcheck = slow
    settings = tmp_path / "settings.yaml"
    settings.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    with (tmp_path / "process.log").open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "emend.cli",
                "recover",
                before.book_id,
                "--revision",
                "original",
                "--approve",
                "-c",
                str(settings),
            ],
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
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
    assert sha256_file(artifact(config, before.book_id)) == before.book_id
    config.tools.epubcheck = original_tool
    result = recover(before.book_id, config, revision="original", approve=True)
    assert result.state == "VALIDATED" and result.recovery_changes


def test_recovery_cli_reports_failed_gate_with_nonzero_exit(tmp_path, config, monkeypatch):
    import yaml
    from typer.testing import CliRunner

    import emend.revalidation as validation
    from emend.cli import app

    before = ingest(write_epub(tmp_path / "book.epub"), config)
    settings = tmp_path / "config.yaml"
    settings.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    monkeypatch.setattr(
        validation,
        "validate_epubcheck",
        lambda *a: Gate(
            name="epubcheck", required=True, status="failed", detail="Injected failure"
        ),
    )
    args = ["recover", before.book_id, "--revision", "original", "--approve", "-c", str(settings)]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1 and "FAILED" in result.output


def test_recovery_refuses_symlink_original_without_following_it(tmp_path, config):
    before = ingest(write_epub(tmp_path / "book.epub"), config)
    original = config.root / "originals" / f"{before.book_id}.epub"
    elsewhere = tmp_path / "elsewhere.epub"
    original.rename(elsewhere)
    original.symlink_to(elsewhere)
    with pytest.raises(ValueError, match="regular bounded"):
        recover(before.book_id, config, revision="original", approve=True)
    assert get_report(Store(config.root), before.book_id) == before


@pytest.mark.parametrize("at", [1, 2, 3])
def test_database_failures_cannot_leave_false_approval(tmp_path, config, monkeypatch, at):
    before = ingest(write_epub(tmp_path / "book.epub"), config)
    real = Store.transition
    calls = 0

    def fail(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == at:
            raise OSError("injected database failure")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Store, "transition", fail)
    with pytest.raises(OSError, match="database failure"):
        recover(before.book_id, config, revision="original", approve=True)
    assert sha256_file(artifact(config, before.book_id)) == before.book_id
    if at == 1:
        assert get_report(Store(config.root), before.book_id) == before
    else:
        assert get_report(Store(config.root), before.book_id).state == "REVIEW_REQUIRED"
    monkeypatch.setattr(Store, "transition", real)
    assert recover(before.book_id, config, revision="original", approve=True).state == "VALIDATED"
