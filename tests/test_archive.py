"""Edits keep entry records of books whose producers store no file attributes."""

import zipfile

import pytest

from emend.archive import copy_entry
from emend.curation import edit_metadata
from emend.image_conversion import discover as image_discover
from emend.image_conversion import preserved as image_preserved
from emend.image_conversion import rewrite as image_rewrite
from emend.inspect import inspect_epub
from emend.repairs import container_preserved, rewrite_container
from emend.text_size import discover as size_discover
from emend.text_size import preserved as size_preserved
from emend.text_size import rewrite as size_rewrite
from tests.epub_factory import book_files, write_epub
from tests.test_image_conversion import files_with as image_files
from tests.test_text_size import files_with as size_files


def attributes(path):
    with zipfile.ZipFile(path) as archive:
        return {info.filename: info.external_attr for info in archive.infolist()}


def test_copy_entry_keeps_zero_and_existing_attributes(tmp_path):
    source = write_epub(tmp_path / "zero.epub", external_attr=0)
    target = tmp_path / "copy.epub"
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(target, "w") as new:
        for info in old.infolist():
            copy_entry(new, info, old.read(info))
    assert set(attributes(target).values()) == {0}
    plain = write_epub(tmp_path / "plain.epub")
    target = tmp_path / "plain-copy.epub"
    with zipfile.ZipFile(plain) as old, zipfile.ZipFile(target, "w") as new:
        for info in old.infolist():
            copy_entry(new, info, old.read(info), "renamed/" + info.filename)
    assert list(attributes(target).values()) == list(attributes(plain).values())
    assert all(name.startswith("renamed/") for name in attributes(target))


@pytest.mark.parametrize("edit", ["image", "text-size", "container", "metadata"])
def test_edits_accept_books_without_file_attributes(tmp_path, config, edit):
    files = {
        "image": image_files(rich=True),
        "text-size": size_files(b"body { font-size: 0.85em; }\n"),
    }.get(edit, book_files())
    source = write_epub(tmp_path / "zero.epub", files, external_attr=0)
    target = tmp_path / "edited.epub"
    inspection = inspect_epub(source, config.limits)
    if edit == "image":
        plan = image_discover(source, inspection)
        image_rewrite(source, target, inspection, plan)
        assert image_preserved(source, target, inspection, plan)
    elif edit == "text-size":
        edits, _ = size_discover(source, inspection)
        size_rewrite(source, target, inspection, edits)
        assert size_preserved(source, target, inspection, edits)
    elif edit == "container":
        rewrite_container(source, target)
        assert container_preserved(source, target)
    else:
        edit_metadata(source, target, inspection.package_path, [])
    assert set(attributes(target).values()) == {0}
