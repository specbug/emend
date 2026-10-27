import errno
import json
import os

import pytest
import yaml
from typer.testing import CliRunner

from emend.cli import app
from emend.inspect import sha256_file
from emend.pipeline import get_report, ingest
from emend.reader_export import ReaderDeliveryError, export_reader
from emend.storage import Store
from tests.epub_factory import write_epub
from tests.test_review_ui import fields, login, post


def setup(tmp_path, config):
    book = ingest(write_epub(tmp_path / "book.epub"), config)
    directory = tmp_path / "reader"
    directory.mkdir()
    return book, directory


def test_real_no_overwrite_export_and_retry_preserve_approved_bytes(tmp_path, config):
    before, directory = setup(tmp_path, config)
    first = export_reader(before.book_id, directory, config)
    assert first["status"] == "exported"
    target = directory / first["receipt"]["filename"]
    assert sha256_file(target) == before.artifact_sha256
    assert before.artifact_sha256[:16] in target.name
    assert export_reader(before.book_id, directory, config)["status"] == "already_exported"
    assert len(list(directory.iterdir())) == 1
    assert get_report(Store(config.root), before.book_id) == before
    assert Store(config.root).resolve(before.book_id)["state"] == "PUBLISHED"


@pytest.mark.parametrize(
    "name",
    [
        "../book.epub",
        "book.txt",
        ".hidden.epub",
        "a\\b.epub",
        "a:b.epub",
        "a\u202eb.epub",
        "x" * 241 + ".epub",
    ],
)
def test_unsafe_or_unsupported_filenames_never_write(tmp_path, config, name):
    book, directory = setup(tmp_path, config)
    with pytest.raises(ValueError, match="filename"):
        export_reader(book.book_id, directory, config, filename=name)
    assert not list(directory.iterdir())


def test_existing_different_file_is_never_overwritten(tmp_path, config):
    book, directory = setup(tmp_path, config)
    target = directory / "mine.epub"
    target.write_bytes(b"personal existing data")
    with pytest.raises(ValueError, match="different file"):
        export_reader(book.book_id, directory, config, filename=target.name)
    assert target.read_bytes() == b"personal existing data"
    assert not list(directory.glob("*.part"))


def test_existing_symlink_is_not_followed(tmp_path, config):
    book, directory = setup(tmp_path, config)
    private = tmp_path / "private"
    private.write_text("private data")
    (directory / "link.epub").symlink_to(private)
    with pytest.raises(ValueError):
        export_reader(book.book_id, directory, config, filename="link.epub")
    assert private.read_text() == "private data"


@pytest.mark.parametrize("where", ["emend", "calibre", "missing"])
def test_forbidden_or_missing_destinations_are_rejected(tmp_path, config, where):
    book, directory = setup(tmp_path, config)
    if where == "emend":
        directory = config.root / "inbox"
    elif where == "calibre":
        config.publish.calibre_library = directory
    else:
        directory = tmp_path / "missing"
    with pytest.raises((ValueError, OSError)):
        export_reader(book.book_id, directory, config)


@pytest.mark.parametrize("fault", ["race_other", "race_same", "remount", "no_rename", "fsync"])
def test_destination_races_and_io_failures_cannot_clobber_files(
    tmp_path, config, monkeypatch, fault
):
    import emend.reader_export as module

    book, directory = setup(tmp_path, config)
    name = "book.epub"
    if fault.startswith("race"):
        real = module.atomic_noreplace

        def race(fd, source, destination):
            descriptor = os.open(
                destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=fd
            )
            with os.fdopen(descriptor, "wb") as out:
                out.write(
                    (config.root / "curated" / f"{book.book_id}.epub").read_bytes()
                    if fault == "race_same"
                    else b"new user file"
                )
            real(fd, source, destination)

        monkeypatch.setattr(module, "atomic_noreplace", race)
    elif fault == "remount":
        real = module.check_directory
        calls = 0

        def moved(path, fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                path.rename(tmp_path / "old-reader")
                path.mkdir()
            return real(path, fd)

        monkeypatch.setattr(module, "check_directory", moved)
    elif fault == "no_rename":
        monkeypatch.setattr(
            module,
            "atomic_noreplace",
            lambda *args: (_ for _ in ()).throw(OSError(errno.ENOTSUP, "unsupported rename")),
        )
    else:
        monkeypatch.setattr(
            module.os,
            "fsync",
            lambda *args: (_ for _ in ()).throw(OSError(errno.EIO, "disk failed")),
        )
    if fault == "race_same":
        assert (
            export_reader(book.book_id, directory, config, filename=name)["status"]
            == "already_exported"
        )
    else:
        with pytest.raises(ReaderDeliveryError):
            export_reader(book.book_id, directory, config, filename=name)
        assert Store(config.root).resolve(book.book_id)["state"] == "VALIDATED"
    if fault == "race_other":
        assert (directory / name).read_bytes() == b"new user file"
    assert not list(directory.glob("*.part"))


def test_database_failure_after_copy_reconciles_on_retry(tmp_path, config, monkeypatch):
    book, directory = setup(tmp_path, config)
    real = Store.save_receipt
    monkeypatch.setattr(
        Store, "save_receipt", lambda *a, **kw: (_ for _ in ()).throw(OSError("state write failed"))
    )
    with pytest.raises(ReaderDeliveryError):
        export_reader(book.book_id, directory, config)
    assert len(list(directory.glob("*.epub"))) == 1
    monkeypatch.setattr(Store, "save_receipt", real)
    result = export_reader(book.book_id, directory, config)
    assert result["status"] == "already_exported"
    assert sha256_file(directory / result["receipt"]["filename"]) == book.artifact_sha256


def test_publication_gate_and_expected_revision_are_enforced(tmp_path, config):
    book, directory = setup(tmp_path, config)
    with pytest.raises(ValueError, match="stale"):
        export_reader(book.book_id, directory, config, expected_artifact_sha256="0" * 64)
    config.quality.require_accessibility_audit = True
    with pytest.raises(ValueError, match="policy"):
        export_reader(book.book_id, directory, config)
    assert not list(directory.iterdir())


def test_unicode_filename_and_directory_fsync_limit_are_explicit(tmp_path, config, monkeypatch):
    import stat

    import emend.reader_export as module

    book, directory = setup(tmp_path, config)
    real = os.fsync

    def sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "directory sync unavailable")
        return real(fd)

    monkeypatch.setattr(module.os, "fsync", sync)
    result = export_reader(book.book_id, directory, config, filename="Café — 星.epub")
    assert not result["receipt"]["directory_fsync"]
    assert sha256_file(directory / "Café — 星.epub") == book.artifact_sha256


def test_cli_and_workspace_use_explicit_configured_reader_folder(workspace, tmp_path, config):
    book, directory = setup(tmp_path, config)
    settings = tmp_path / "config.yaml"
    settings.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    result = CliRunner().invoke(
        app, ["export-reader", book.book_id, "--to", str(directory), "-c", str(settings)]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "exported"
    workspace.app.reader_directory = directory
    cookie = login(workspace)
    status, _, _ = post(
        workspace,
        cookie,
        fields(
            workspace,
            book,
            "reader-export",
            filename="from-ui.epub",
            directory=str(tmp_path / "ignored"),
        ),
    )
    assert status == 303
    assert sha256_file(directory / "from-ui.epub") == book.artifact_sha256
    assert not (tmp_path / "ignored").exists()
