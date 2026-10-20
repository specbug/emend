import csv
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from emend.backup import backup_library, inventory, restore_backup, verify_backup
from emend.cover_apply import apply_cover
from emend.covers import cover_review
from emend.curation import apply_proposals
from emend.inspect import sha256_file
from emend.publish import publish, publish_calibre
from tests.test_covers import candidate, cover_book
from tests.test_curation import setup_book


@pytest.fixture
def real_calibre(tmp_path, config, monkeypatch):
    folder = os.environ.get("EMEND_TEST_CALIBRE_BIN")
    if not folder:
        pytest.skip("Set EMEND_TEST_CALIBRE_BIN to run real Calibre CLI integration")
    folder = Path(folder)
    for name in ("calibredb", "calibre-debug", "ebook-meta", "ebook-convert"):
        assert (folder / name).is_file(), f"Missing real Calibre tool: {name}"
    monkeypatch.setenv("CALIBRE_CONFIG_DIRECTORY", str(tmp_path / "calibre-config"))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    config.tools.calibre_bin = folder
    config.publish.calibre_library = tmp_path / "Calibre Library"
    config.publish.calibre_library.mkdir()

    def run(tool, *args):
        result = subprocess.run(
            [str(folder / tool), *map(str, args)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert result.returncode == 0, result.stderr or result.stdout
        return result.stdout

    # The real CLI initializes the database; no handcrafted schema.
    assert (
        json.loads(
            run(
                "calibredb",
                "list",
                "--with-library",
                config.publish.calibre_library,
                "--for-machine",
            )
        )
        == []
    )
    return config, run


def rows(run, library):
    return json.loads(
        run(
            "calibredb",
            "list",
            "--with-library",
            library,
            "--for-machine",
            "--fields",
            "id,title,authors,identifiers,formats,cover,publisher,pubdate,languages",
        )
    )


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_real_calibre_import_revisions_and_retry(tmp_path, real_calibre, version):
    config, run = real_calibre
    before, _ = setup_book(tmp_path, config, version, extra=b'<meta name="cover" content="image"/>')
    library = config.publish.calibre_library
    first = publish(before.book_id, ["calibre"], config)
    assert first["calibre"]["status"] == "published", first
    assert publish(before.book_id, ["calibre"], config)["calibre"]["status"] == "already_published"
    apply_proposals(before.book_id, config, safe=True)
    review = cover_review(before.book_id, config, image=candidate(tmp_path))
    after = apply_cover(
        before.book_id, config, review_id=review.id, approve=review.candidates[0].id
    )
    result = publish(after.book_id, ["calibre"], config)
    assert result["calibre"]["status"] == "published", result
    records = rows(run, library)
    assert len(records) == 2
    assert {r["identifiers"]["emend"] for r in records} == {
        before.artifact_sha256,
        after.artifact_sha256,
    }
    for row in records:
        epub = next(Path(p) for p in row["formats"] if p.endswith(".epub"))
        assert sha256_file(epub) == row["identifiers"]["emend"]
        assert Path(row["cover"]).is_file()
        with Image.open(row["cover"]) as image:
            assert image.size == (
                (160, 240) if row["identifiers"]["emend"] == before.artifact_sha256 else (320, 480)
            )
        assert row["title"] == "The Quiet Observatory" and row["authors"] == "Emend Test Corpus"
    # Reconcile an import that succeeded before a receipt could be saved.
    epub = config.root / "curated" / f"{after.book_id}.epub"
    assert (
        publish_calibre(epub, after.artifact_sha256, config)["calibre_id"]
        == result["calibre"]["receipt"]["calibre_id"]
    )
    assert len(rows(run, library)) == 2
    # Detect a destination format changed by a plugin/user before claiming delivery.
    imported = next(r for r in records if r["identifiers"]["emend"] == after.artifact_sha256)
    assert imported["publisher"] == "Observatory Press"
    assert imported["pubdate"].startswith("2020-03-04")
    assert imported["languages"] == ["eng"]
    stored = next(Path(p) for p in imported["formats"] if p.endswith(".epub"))
    stored.write_bytes(stored.read_bytes() + b"changed after import")
    with pytest.raises(ValueError, match="differs from the approved artifact"):
        publish_calibre(epub, after.artifact_sha256, config)


def test_real_rich_library_backup_and_restore(tmp_path, real_calibre, monkeypatch):
    config, run = real_calibre
    before = cover_book(tmp_path, config)
    result = publish(before.book_id, ["calibre"], config)
    assert result["calibre"]["status"] == "published", result
    book = result["calibre"]["receipt"]["calibre_id"]
    library = config.publish.calibre_library
    run(
        "calibredb",
        "add_custom_column",
        "reading_status",
        "Reading status",
        "text",
        "--with-library",
        library,
    )
    run(
        "calibredb",
        "set_custom",
        "reading_status",
        book,
        "Read — annotated",
        "--with-library",
        library,
    )
    run(
        "calibredb",
        "set_metadata",
        book,
        "--with-library",
        library,
        "--field",
        "comments:<p>Rich <strong>HTML</strong> description — café.</p>",
        "--field",
        "tags:astronomy,original corpus",
        "--field",
        "series:Observatory Papers",
        "--field",
        "series_index:2.5",
        "--field",
        "rating:4",
    )
    # Create another actual ebook format, rather than a placeholder byte string.
    extra = tmp_path / "observatory.txt"
    run("ebook-convert", config.root / "curated" / f"{before.book_id}.epub", extra)
    assert "Mira" in extra.read_text(encoding="utf-8-sig")
    run("calibredb", "add_format", book, extra, "--with-library", library)
    helper = Path(__file__).with_name("calibre_library.py")
    expected = json.loads(run("calibre-debug", "-e", helper, "--", "enrich", library, book))
    assert expected["custom_status"] == "Read — annotated"
    assert expected["comments"] == "<p>Rich <strong>HTML</strong> description — café.</p>"
    assert expected["tags"] == ["astronomy", "original corpus"]
    assert expected["series"] == "Observatory Papers" and expected["series_index"] == 2.5
    assert expected["rating"] == 8  # Calibre stores a four-star rating on a ten-point scale.
    assert expected["formats"] == ["EPUB", "TXT"]
    assert expected["resources"] and expected["annotations"] and expected["extra_files"]
    run("calibredb", "backup_metadata", "--all", "--with-library", library)
    baseline = inventory(library)
    assert ".calnotes/notes.db" in baseline
    assert any(name.endswith("metadata.opf") for name in baseline)
    transport = shutil.which("rclone")
    if transport:
        rclone_config = tmp_path / "rclone.conf"
        rclone_config.write_text("[localtest]\ntype = local\n")
        monkeypatch.setenv("RCLONE_CONFIG", str(rclone_config))
        config.tools.rclone = Path(transport)
        config.publish.backup_remote = f"localtest:{tmp_path / 'transferred-backups'}"
    receipt = backup_library(config, calibre_closed=True, upload=bool(transport))
    archive = Path(receipt["archive"])
    if transport:
        assert receipt["cloud_status"] == "uploaded", receipt
        archive = tmp_path / "transferred-backups" / archive.name
    assert verify_backup(archive)["files"] == baseline
    restored = restore_backup(archive, tmp_path / "Restored Calibre Library")
    # Compare bytes, metadata, permissions and timestamps BEFORE reopening; Calibre
    # can legitimately write preferences/cache state when it opens a library.
    assert inventory(restored) == baseline
    actual = json.loads(run("calibre-debug", "-e", helper, "--", "inspect", restored, book))
    assert actual == expected
    assert len(rows(run, restored)) == 1
    check = run("calibredb", "check_library", "--csv", "--with-library", restored)
    # Calibre prints progress before CSV output; actual issues have three columns.
    assert not [row for row in csv.reader(check.splitlines()) if len(row) == 3], check
