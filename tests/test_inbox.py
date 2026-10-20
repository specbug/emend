import json
import os
import select
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import emend.inbox as inbox_module
from emend.cli import app
from emend.curation import apply_proposals
from emend.inbox import FileChanged, Inbox
from emend.inspect import sha256_file
from emend.pipeline import get_report, ingest, ingest_new
from emend.revalidation import revalidate
from emend.storage import OperationBusyError, Store
from tests.epub_factory import book_files, write_epub
from tests.test_curation import setup_book


@pytest.fixture
def clock(monkeypatch):
    value = [0.0]
    monkeypatch.setattr(inbox_module, "monotonic", lambda: value[0])
    monkeypatch.setattr(
        inbox_module, "sleep", lambda seconds: value.__setitem__(0, value[0] + seconds)
    )
    return value


def put(processor, name="a book.epub", files=None):
    return write_epub(processor.directory / name, files)


def test_waits_for_stable_observation_and_preserves_source_name_and_bytes(config):
    processor = Inbox(config)
    source = put(processor)
    original = source.read_bytes()
    assert processor.scan(now=0)[0].status == "waiting"
    assert processor.scan(now=1) == []
    assert not Store(config.root).books()
    (event,) = processor.scan(now=2)
    assert event.status == "imported" and event.state == "VALIDATED"
    row = Store(config.root).resolve(event.book_id)
    assert row["source_name"] == "a book.epub"
    assert source.read_bytes() == original
    assert Path(row["artifact_path"]).read_bytes() == original
    assert processor.scan(now=10) == []
    assert not list((config.root / ".staging").iterdir())


def test_growing_file_resets_stability_without_repeating_wait_messages(config):
    processor = Inbox(config)
    source = put(processor)
    assert processor.scan(now=0)[0].status == "waiting"
    source.write_bytes(source.read_bytes() + b"more bytes")
    assert processor.scan(now=2) == []
    assert not Store(config.root).books()
    assert processor.scan(now=3) == []
    assert processor.scan(now=4)[0].status == "imported"


def test_duplicate_names_and_restarts_do_not_rerun_quality_checks(config, monkeypatch):
    processor = Inbox(config)
    first = put(processor, "first.epub")
    (processor.directory / "second.EPUB").write_bytes(first.read_bytes())
    processor.scan(now=0)
    result = processor.scan(now=2)
    assert [event.status for event in result] == ["imported", "known"]
    (row,) = Store(config.root).books()
    before = row.copy()

    def unexpected(*args, **kwargs):
        pytest.fail("Duplicate input must not be revalidated automatically")

    monkeypatch.setattr("emend.pipeline.validate_epubcheck", unexpected)
    restarted = Inbox(config)
    restarted.scan(now=0)
    assert all(event.status == "known" for event in restarted.scan(now=2))
    assert Store(config.root).books() == [before]


def test_duplicate_original_keeps_curated_metadata_report_and_history(tmp_path, config):
    before, _ = setup_book(tmp_path, config)
    after = apply_proposals(before.book_id, config, safe=True)
    row = Store(config.root).resolve(before.book_id)
    artifact = Path(row["artifact_path"]).read_bytes()
    processor = Inbox(config)
    original = config.root / "originals" / f"{before.book_id}.epub"
    (processor.directory / "duplicate.epub").write_bytes(original.read_bytes())
    processor.scan(now=0)
    assert processor.scan(now=2)[0].status == "known"
    assert Store(config.root).resolve(before.book_id) == row
    assert get_report(Store(config.root), before.book_id) == after
    assert Path(row["artifact_path"]).read_bytes() == artifact


def test_book_curated_while_inbox_prepares_snapshot_is_not_reset(tmp_path, config, monkeypatch):
    before, _ = setup_book(tmp_path, config)
    processor = Inbox(config)
    (processor.directory / "copy.epub").write_bytes(
        (config.root / "originals" / f"{before.book_id}.epub").read_bytes()
    )
    actual = inbox_module.ingest_new
    captured = []

    def concurrent_edit(source, settings):
        captured.append(apply_proposals(before.book_id, settings, safe=True))
        return actual(source, settings)

    monkeypatch.setattr(inbox_module, "ingest_new", concurrent_edit)
    processor.scan(now=0)
    assert processor.scan(now=2)[0].status == "known"
    assert get_report(Store(config.root), before.book_id) == captured[0]


@pytest.mark.parametrize("state", ["FAILED", "REVIEW_REQUIRED"])
def test_known_failed_or_pending_reports_are_left_for_explicit_revalidation(config, state):
    if state == "REVIEW_REQUIRED":
        config.tools.epubcheck = config.root / "missing"
    processor = Inbox(config)
    source = put(processor)
    if state == "FAILED":
        source.write_bytes(b"broken zip")
    processor.scan(now=0)
    (event,) = processor.scan(now=2)
    assert event.status == "blocked" and event.state == state
    row = Store(config.root).resolve(event.book_id)
    assert processor.scan(now=4) == []
    restarted = Inbox(config)
    restarted.scan(now=0)
    assert restarted.scan(now=2)[0].status == "known"
    assert Store(config.root).resolve(event.book_id) == row


def test_revalidation_pending_curated_copy_is_preserved(tmp_path, config):
    before, _ = setup_book(tmp_path, config)
    after = apply_proposals(before.book_id, config, safe=True)
    config.tools.epubcheck = tmp_path / "missing"
    pending = revalidate(before.book_id, config)
    processor = Inbox(config)
    (processor.directory / "original.epub").write_bytes(
        (config.root / "originals" / f"{before.book_id}.epub").read_bytes()
    )
    processor.scan(now=0)
    assert processor.scan(now=2)[0].status == "known"
    assert get_report(Store(config.root), before.book_id) == pending
    assert (
        sha256_file(Path(Store(config.root).resolve(before.book_id)["artifact_path"]))
        == after.artifact_sha256
    )


def test_partial_copies_hidden_files_links_and_subdirectories_are_ignored(config):
    processor = Inbox(config)
    for name in ("copy.epub.part", ".hidden.epub", "unrelated.txt"):
        (processor.directory / name).write_bytes(b"unfinished")
    (processor.directory / "directory.epub").mkdir()
    (processor.directory / "link.epub").symlink_to(processor.directory / "unrelated.txt")
    os.mkfifo(processor.directory / "pipe.epub")
    assert processor.scan(now=0) == [] and processor.scan(now=2) == []
    assert not Store(config.root).books()


def test_renamed_partial_copy_is_seen_after_it_settles(config):
    processor = Inbox(config)
    source = put(processor, "book.epub.part")
    assert processor.scan(now=0) == []
    source.rename(source.with_suffix(""))
    assert processor.scan(now=5)[0].status == "waiting"
    assert processor.scan(now=7)[0].status == "imported"


def test_disappearing_inputs_are_forgotten_and_can_return(config):
    processor = Inbox(config)
    source = put(processor)
    data = source.read_bytes()
    processor.scan(now=0)
    source.unlink()
    assert processor.scan(now=2) == [] and not processor.observations
    source.write_bytes(data)
    assert processor.scan(now=3)[0].status == "waiting"
    assert processor.scan(now=5)[0].status == "imported"


def test_replaced_input_with_preserved_mtime_is_a_new_observation(config):
    processor = Inbox(config)
    source = put(processor)
    processor.scan(now=0)
    first = processor.scan(now=2)[0]
    metadata = source.stat()
    files = book_files()
    files["EPUB/chapter.xhtml"] = files["EPUB/chapter.xhtml"].replace(b"Mira", b"Lina")
    replacement = write_epub(source.with_suffix(".replacement"), files)
    os.utime(replacement, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    replacement.replace(source)
    assert processor.scan(now=3)[0].status == "waiting"
    second = processor.scan(now=5)[0]
    assert second.status == "imported" and second.book_id != first.book_id
    assert len(Store(config.root).books()) == 2


def test_busy_operation_retries_without_repeat_events_or_data_changes(config):
    processor = Inbox(config)
    put(processor)
    processor.scan(now=0)
    with Store(config.root).locked():
        assert processor.scan(now=2)[0].status == "busy"
        assert processor.scan(now=3) == []
    assert not list((config.root / ".staging").iterdir())
    assert processor.scan(now=4)[0].status == "imported"


def test_changed_copy_is_deferred_before_ingestion(config, monkeypatch):
    processor = Inbox(config)
    put(processor)
    processor.scan(now=0)
    with monkeypatch.context() as patch:
        patch.setattr(
            inbox_module, "stable_copy", lambda *args: (_ for _ in ()).throw(FileChanged("changed"))
        )
        assert processor.scan(now=2) == []  # already waiting; no repeated message
    assert not Store(config.root).books()
    assert processor.scan(now=3) == []
    assert processor.scan(now=4)[0].status == "imported"


def test_symlink_replacement_between_stat_and_copy_never_reads_target(config, monkeypatch):
    processor = Inbox(config)
    put(processor)
    target = processor.directory / "private.txt"
    target.write_text("Private data must not become an EPUB original")
    processor.scan(now=0)
    original = inbox_module.stable_copy

    def swap(path, snapshot, expected, limit):
        path.unlink()
        path.symlink_to(target)
        return original(path, snapshot, expected, limit)

    monkeypatch.setattr(inbox_module, "stable_copy", swap)
    assert processor.scan(now=2)[0].status == "error"
    assert not Store(config.root).books() and not list((config.root / "originals").iterdir())


def test_source_changed_during_actual_read_is_deferred(config, monkeypatch):
    processor = Inbox(config)
    source = put(processor)
    processor.scan(now=0)
    fdopen = os.fdopen

    class ChangingReader:
        def __init__(self, fd, mode):
            self.stream = fdopen(fd, mode)
            self.changed = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            data = self.stream.read(size)
            if not self.changed:
                self.changed = True
                with source.open("ab") as out:
                    out.write(b"new trailing bytes")
            return data

    with monkeypatch.context() as patch:
        patch.setattr(inbox_module.os, "fdopen", ChangingReader)
        assert processor.scan(now=2) == []
    assert not Store(config.root).books()
    processor.scan(now=3)
    assert processor.scan(now=5)[0].status == "imported"


def test_oversized_file_and_bad_input_do_not_prevent_other_imports(config):
    processor = Inbox(config)
    put(processor, "good.epub")
    (processor.directory / "broken.epub").write_bytes(b"bad zip")
    with (processor.directory / "large.epub").open("wb") as out:
        out.truncate(2 * 1024**2)
    config.limits.max_archive_mb = 1
    processor.scan(now=0)
    assert {event.status for event in processor.scan(now=2)} == {"error", "blocked", "imported"}
    assert len(Store(config.root).books()) == 2


def test_ingestion_io_error_is_reported_instead_of_treated_as_an_unstable_input(
    config, monkeypatch
):
    processor = Inbox(config)
    put(processor)
    processor.scan(now=0)

    def missing(*args):
        raise FileNotFoundError("Missing stored evidence")

    monkeypatch.setattr(inbox_module, "ingest_new", missing)
    (event,) = processor.scan(now=2)
    assert event.status == "error" and "Missing stored evidence" in event.detail
    assert processor.scan(now=4) == []


def test_directory_bound_is_checked_before_any_import(config, monkeypatch):
    processor = Inbox(config)
    for i in range(4):
        put(processor, f"{i}.epub")
    monkeypatch.setattr(inbox_module, "MAX_ENTRIES", 3)
    with pytest.raises(ValueError, match="entry limit"):
        processor.scan(now=0)
    assert not Store(config.root).books()


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), 301])
def test_invalid_stability_window_is_rejected(config, value):
    with pytest.raises(ValueError, match="Stable interval"):
        Inbox(config, stable_seconds=value)


def test_managed_or_calibre_directories_are_not_used_as_input(tmp_path, config):
    Store(config.root)
    for directory in (config.root, config.root / "curated", config.root / "originals"):
        with pytest.raises(ValueError, match="managed data"):
            Inbox(config, directory)
    config.publish.calibre_library = tmp_path / "Calibre Library"
    config.publish.calibre_library.mkdir()
    with pytest.raises(ValueError, match="Calibre library"):
        Inbox(config, config.publish.calibre_library)
    with pytest.raises(ValueError, match="does not exist"):
        Inbox(config, tmp_path / "missing")


def test_only_new_ingestion_is_atomic_and_leaves_known_reports_unchanged(epub, config):
    first = ingest_new(epub, config)
    row = Store(config.root).resolve(first.book_id)
    with Store(config.root).locked(), pytest.raises(OperationBusyError):
        ingest_new(epub, config)
    assert ingest_new(epub, config).book_id == first.book_id
    assert Store(config.root).resolve(first.book_id) == row
    assert (
        ingest(epub, config).book_id == first.book_id
    )  # explicit ingestion retains its existing semantics


def test_orphaned_curated_artifact_is_not_overwritten(epub, config):
    Store(config.root)
    artifact = config.root / "curated" / f"{sha256_file(epub)}.epub"
    artifact.write_bytes(b"retained data with missing database record")
    assert ingest_new(epub, config).book_id == sha256_file(epub)
    assert artifact.read_bytes() == b"retained data with missing database record"


def test_incomplete_known_attempt_is_not_restarted_implicitly(epub, config):
    store = Store(config.root)
    digest = sha256_file(epub)
    store.transition(digest, epub.name, "ANALYZED", "Interrupted attempt without a report.")
    row = store.resolve(digest)
    assert ingest_new(epub, config).book_id == digest
    assert store.resolve(digest) == row


def test_copies_of_curated_revisions_resolve_to_the_original_book_id(tmp_path, config):
    before, _ = setup_book(tmp_path, config)
    after = apply_proposals(before.book_id, config, safe=True)
    processor = Inbox(config)
    artifact = Path(Store(config.root).resolve(before.book_id)["artifact_path"])
    (processor.directory / "edited-copy.epub").write_bytes(artifact.read_bytes())
    processor.scan(now=0)
    (event,) = processor.scan(now=2)
    assert event.status == "known" and event.book_id == before.book_id
    assert event.source_sha256 == after.artifact_sha256 != before.book_id
    assert len(Store(config.root).books()) == 1
    assert get_report(Store(config.root), before.book_id) == after


def test_batch_reports_pending_files_when_they_keep_changing(config, clock, monkeypatch):
    processor = Inbox(config)
    source = put(processor)

    def change(seconds):
        clock[0] += seconds
        source.write_bytes(source.read_bytes() + b"more")

    monkeypatch.setattr(inbox_module, "sleep", change)
    assert processor.batch()[0].status == "waiting"
    assert not Store(config.root).books()


def test_batch_rechecks_are_quiet_for_unchanged_completed_files(config, clock):
    processor = Inbox(config)
    put(processor)
    assert processor.batch()[0].status == "imported"
    assert processor.batch() == []


def test_cli_batch_json_exit_codes_and_continuous_event_stream(
    config, tmp_path, clock, monkeypatch
):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    processor = Inbox(config)
    put(processor)
    runner = CliRunner()
    args = ["inbox", "--json", "-c", str(path)]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["events"][0]["status"] == "imported"
    (processor.directory / "bad.epub").write_bytes(b"bad zip")
    result = runner.invoke(app, args)
    assert result.exit_code == 1 and any(
        e["status"] == "blocked" for e in json.loads(result.output)["events"]
    )

    def stop(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr("emend.cli.sleep", stop)
    result = runner.invoke(app, [*args, "--watch"])
    assert result.exit_code == 0
    assert all(json.loads(line)["status"] == "waiting" for line in result.output.splitlines())


def test_official_epubcheck_inbox_ingestion(config):
    jar = os.environ.get("EMEND_TEST_EPUBCHECK_JAR")
    if not jar:
        pytest.skip("Set EMEND_TEST_EPUBCHECK_JAR for official EPUBCheck integration")
    config.tools.epubcheck_jar = Path(jar)
    processor = Inbox(config)
    put(processor)
    processor.scan(now=0)
    (event,) = processor.scan(now=2)
    assert event.status == "imported"
    report = get_report(Store(config.root), event.book_id)
    assert next(g.status for g in report.gates if g.name == "epubcheck") == "passed"


def test_real_foreground_watch_imports_then_stops_cleanly(config, tmp_path):
    settings = tmp_path / "config.yaml"
    settings.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    processor = Inbox(config)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "emend.cli",
            "inbox",
            "--watch",
            "--json",
            "--stable-seconds",
            "0.1",
            "--interval",
            "0.1",
            "-c",
            str(settings),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        source = put(processor)
        events = []
        buffered = b""
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and process.poll() is None:
            ready, _, _ = select.select([process.stdout], [], [], 0.2)
            if ready:
                buffered += os.read(process.stdout.fileno(), 65536)
                while b"\n" in buffered:
                    line, buffered = buffered.split(b"\n", 1)
                    if line:
                        events.append(json.loads(line))
                if events and events[-1]["status"] == "imported":
                    break
        assert events and events[-1]["status"] == "imported", events
        process.send_signal(signal.SIGINT)
        process.communicate(timeout=5)
        assert process.returncode == 0
        assert source.exists()
        row = Store(config.root).resolve(events[-1]["book_id"])
        assert row["state"] == "VALIDATED"
        # Restarting the command is harmless, including for a renamed copy.
        source.rename(source.with_name("renamed.epub"))
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "emend.cli",
                "inbox",
                "--json",
                "--stable-seconds",
                "0.1",
                "-c",
                str(settings),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["events"][0]["status"] == "known"
        assert Store(config.root).resolve(events[-1]["book_id"]) == row
    finally:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
