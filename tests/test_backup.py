import io
import json
import shutil
import sqlite3
import tarfile
from pathlib import Path

import pytest

from emend.backup import backup_library, inventory, restore_backup, upload_backup, verify_backup


def test_complete_rich_library_round_trip(fake_publishers, tmp_path):
    config = fake_publishers
    original_inventory = inventory(config.publish.calibre_library)
    receipt = backup_library(config, calibre_closed=True, upload=True)
    assert receipt["verified"]
    assert receipt["cloud_status"] == "uploaded"
    local = Path(receipt["archive"])
    cloud = tmp_path / "fake-drive" / "backups" / local.name
    assert cloud.read_bytes() == local.read_bytes()
    manifest = verify_backup(cloud)
    assert manifest["files"] == original_inventory
    restored = restore_backup(cloud, tmp_path / "restored-library")
    assert inventory(restored) == original_inventory
    db = sqlite3.connect(restored / "metadata.db")
    try:
        assert db.execute("SELECT value FROM custom_column_1 WHERE book=1").fetchone() == (
            "Read and annotated",
        )
        assert db.execute("SELECT title FROM books WHERE id=1").fetchone() == (
            "The Quiet Observatory",
        )
    finally:
        db.close()
    assert (restored / ".notes" / "notes.db").read_bytes() == b"test-note-database"


def test_backup_requires_closed_library_acknowledgement(fake_publishers):
    with pytest.raises(ValueError, match="Quit Calibre"):
        backup_library(fake_publishers, calibre_closed=False)


def test_backups_are_versioned_and_do_not_delete_old_snapshots(fake_publishers):
    first = backup_library(fake_publishers, True)
    second = backup_library(fake_publishers, True)
    assert first["archive"] != second["archive"]
    assert Path(first["archive"]).exists() and Path(second["archive"]).exists()


def test_wal_and_symlinks_block_backup(fake_publishers, tmp_path):
    library = fake_publishers.publish.calibre_library
    wal = library / "metadata.db-wal"
    wal.touch()
    with pytest.raises(ValueError, match="WAL/journal"):
        backup_library(fake_publishers, True)
    wal.unlink()
    (library / "external").symlink_to(tmp_path / "private")
    with pytest.raises(ValueError, match="symlink"):
        backup_library(fake_publishers, True)


def test_restore_never_overwrites_an_existing_library(fake_publishers):
    receipt = backup_library(fake_publishers, True)
    with pytest.raises(ValueError, match="must not exist"):
        restore_backup(Path(receipt["archive"]), fake_publishers.publish.calibre_library)


def test_changed_source_aborts_backup(fake_publishers, monkeypatch):
    from emend import backup

    original = backup.inventory
    calls = 0

    def changing(library):
        nonlocal calls
        calls += 1
        if calls == 2:
            (library / "new-file").write_text("Library changed")
        return original(library)

    monkeypatch.setattr(backup, "inventory", changing)
    with pytest.raises(ValueError, match="changed during backup"):
        backup_library(fake_publishers, True)
    assert not list((fake_publishers.root / "backups").iterdir())


def test_backup_upload_failure_retains_verified_local_snapshot(fake_publishers):
    config = fake_publishers
    tool = config.tools.rclone
    config.tools.rclone = config.root / "missing"
    receipt = backup_library(config, True, upload=True)
    assert receipt["cloud_status"] == "pending"
    archive = Path(receipt["archive"])
    assert verify_backup(archive)
    config.tools.rclone = tool
    assert upload_backup(archive, config)["cloud_status"] == "uploaded"


def test_corrupted_member_fails_manifest_verification(fake_publishers, tmp_path):
    receipt = backup_library(fake_publishers, True)
    bad = tmp_path / "tampered.tar.gz"
    with tarfile.open(receipt["archive"], "r:gz") as src, tarfile.open(bad, "w:gz") as dest:
        for member in src.getmembers():
            data = src.extractfile(member).read()
            if member.name.endswith("cover.jpg"):
                data = b"changed" + data
                member.size = len(data)
            dest.addfile(member, io.BytesIO(data))
    with pytest.raises(ValueError, match="checksum mismatch"):
        verify_backup(bad)


def test_unsafe_backup_is_rejected_before_restore(tmp_path):
    path = tmp_path / "unsafe.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        for name, data in [
            ("manifest.json", json.dumps({"version": 1, "files": {}}).encode()),
            ("../outside", b"unsafe"),
        ]:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
    with pytest.raises(ValueError, match="unsafe"):
        restore_backup(path, tmp_path / "restored")
    assert not (tmp_path / "outside").exists()


def test_non_calibre_database_is_rejected(fake_publishers):
    (fake_publishers.publish.calibre_library / "metadata.db").write_bytes(b"not sqlite")
    with pytest.raises(ValueError, match="Invalid backup"):
        backup_library(fake_publishers, True)


def test_real_rclone_backup_copy_and_restore(config, library, tmp_path, monkeypatch):
    tool = shutil.which("rclone")
    if tool is None:
        pytest.skip("rclone is not installed; the mocked transport tests still run.")
    # Exercise the real CLI against a local backend, without a Google account.
    rclone_config = tmp_path / "rclone.conf"
    rclone_config.write_text("[localtest]\ntype = local\n")
    monkeypatch.setenv("RCLONE_CONFIG", str(rclone_config))
    config.tools.rclone = Path(tool)
    config.publish.calibre_library = library
    destination = tmp_path / "rclone-destination"
    config.publish.backup_remote = f"localtest:{destination}"
    result = backup_library(config, True, upload=True)
    assert result["cloud_status"] == "uploaded", result
    uploaded = destination / Path(result["archive"]).name
    restored = restore_backup(uploaded, tmp_path / "rclone-restored")
    assert inventory(restored) == inventory(library)
