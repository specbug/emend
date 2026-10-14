import zipfile

import pytest

from emend.config import Limits
from emend.inspect import inspect_epub, local_target
from tests.epub_factory import book_files, write_epub


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_valid_books_inventory_spine_notes_and_images(tmp_path, version):
    epub = write_epub(tmp_path / "book.epub", version=version)
    result = inspect_epub(epub)
    assert not result.blocking, result.findings
    assert result.epub_version == version
    assert result.spine == ["EPUB/chapter.xhtml", "EPUB/notes.xhtml"]
    assert result.text_characters == (381 if version == "3.0" else 350)
    assert "EPUB/observatory.png" in result.resource_hashes
    assert result.text_sha256 and result.spine_text_sha256


@pytest.mark.parametrize(
    ("path", "old", "new", "code"),
    [
        ("mimetype", b"application/epub+zip", b"wrong", "zip.mimetype"),
        (
            "EPUB/chapter.xhtml",
            b"notes.xhtml#note1",
            b"notes.xhtml#missing",
            "navigation.missing_fragment",
        ),
        ("EPUB/chapter.xhtml", b"notes.xhtml#note1", b"absent.xhtml", "navigation.missing_target"),
        ("EPUB/chapter.xhtml", b'id="ref1"', b'id="start"', "navigation.duplicate_id"),
        ("EPUB/chapter.xhtml", b"notes.xhtml#note1", b"../../outside.xhtml", "navigation.unsafe"),
        (
            "EPUB/chapter.xhtml",
            b"notes.xhtml#note1",
            b"%2e%2e/%2e%2e/outside.xhtml",
            "navigation.unsafe",
        ),
        ("EPUB/chapter.xhtml", b"notes.xhtml#note1", b"javascript:alert(1)", "navigation.unsafe"),
        (
            "EPUB/chapter.xhtml",
            b'src="observatory.png"',
            b'src="https://example.org/a.png"',
            "resource.remote",
        ),
        ("EPUB/chapter.xhtml", b"<body>", b'<body onload="alert(1)">', "content.scripted"),
        ("EPUB/package.opf", b'idref="chapter"', b'idref="missing"', "spine.missing_item"),
        ("EPUB/package.opf", b'properties="nav"', b'properties=""', "navigation.missing"),
        ("EPUB/package.opf", b"<dc:language>en</dc:language>", b"", "metadata.missing"),
        (
            "EPUB/package.opf",
            b'unique-identifier="book-id"',
            b'unique-identifier="absent"',
            "metadata.unique_identifier",
        ),
        (
            "EPUB/package.opf",
            b'<itemref idref="notes"/>',
            b'<itemref idref="chapter"/>',
            "spine.duplicate",
        ),
        ("EPUB/nav.xhtml", b'epub:type="toc"', b'epub:type="landmarks"', "navigation.empty"),
        ("EPUB/observatory.png", None, b"not an image", "image.invalid"),
        ("EPUB/chapter.xhtml", b"</html>", b"", "xml.invalid"),
        ("EPUB/style.css", None, b"@import 'missing.css';", "navigation.missing_target"),
        (
            "EPUB/style.css",
            None,
            b"p { background: url('missing.png'); }",
            "navigation.missing_target",
        ),
    ],
)
def test_defects_block(tmp_path, path, old, new, code):
    files = book_files()
    files[path] = new if old is None else files[path].replace(old, new)
    result = inspect_epub(write_epub(tmp_path / "broken.epub", files))
    assert result.blocking
    assert code in {finding.code for finding in result.findings}


@pytest.mark.parametrize(
    "missing", ["META-INF/container.xml", "EPUB/package.opf", "EPUB/chapter.xhtml"]
)
def test_missing_required_resources_block(tmp_path, missing):
    files = book_files()
    del files[missing]
    assert inspect_epub(write_epub(tmp_path / "book.epub", files)).blocking


@pytest.mark.parametrize(
    "name", ["../outside", "/absolute", "EPUB/../../outside", "EPUB\\evil", "EPUB/./chapter"]
)
def test_unsafe_zip_paths_are_rejected(tmp_path, name):
    files = book_files()
    files[name] = b"unsafe"
    result = inspect_epub(write_epub(tmp_path / "book.epub", files))
    assert any(f.code == "zip.path" for f in result.findings)
    assert not (tmp_path / "outside").exists()


def test_duplicate_zip_members_block(tmp_path):
    path = write_epub(tmp_path / "book.epub")
    with pytest.warns(UserWarning), zipfile.ZipFile(path, "a") as archive:
        archive.writestr("mimetype", b"application/epub+zip")
    assert inspect_epub(path).findings[0].code == "zip.duplicate"


def test_invalid_zip_and_size_limits(tmp_path, epub):
    broken = tmp_path / "bad.epub"
    broken.write_bytes(b"not a zip")
    assert inspect_epub(broken).findings[0].code == "zip.invalid"
    assert inspect_epub(epub, Limits(max_members=1)).findings[0].code == "zip.limit"


def test_xml_entities_are_not_resolved(tmp_path):
    files = book_files()
    secret = tmp_path / "sensitive.txt"
    secret.write_text("must-not-be-read")
    files["EPUB/chapter.xhtml"] = (
        f'''<!DOCTYPE html [<!ENTITY private SYSTEM "{secret.as_uri()}">]><html xmlns="http://www.w3.org/1999/xhtml"><body>&private;</body></html>'''.encode()
    )
    result = inspect_epub(write_epub(tmp_path / "book.epub", files))
    assert result.blocking
    assert "must-not-be-read" not in result.model_dump_json()


def test_typography_observations_preserve_source(tmp_path):
    files = book_files()
    files["EPUB/style.css"] += b" p { color: black; font-size: 12px; }"
    path = write_epub(tmp_path / "book.epub", files)
    original = path.read_bytes()
    result = inspect_epub(path)
    assert not result.blocking
    assert {"typography.forced_color", "typography.fixed_size"} <= {f.code for f in result.findings}
    assert path.read_bytes() == original


def test_non_spine_text_and_reading_order_affect_fingerprints(tmp_path):
    files = book_files()
    base = inspect_epub(write_epub(tmp_path / "base.epub", files))
    files["EPUB/nav.xhtml"] = files["EPUB/nav.xhtml"].replace(
        b"At the Observatory</a>", b"Opening</a>"
    )
    nav = inspect_epub(write_epub(tmp_path / "nav.epub", files))
    assert nav.text_sha256 != base.text_sha256
    assert nav.spine_text_sha256 == base.spine_text_sha256
    files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
        b'<itemref idref="chapter"/><itemref idref="notes"/>',
        b'<itemref idref="notes"/><itemref idref="chapter"/>',
    )
    reordered = inspect_epub(write_epub(tmp_path / "reordered.epub", files))
    assert reordered.spine_text_sha256 != nav.spine_text_sha256


def test_url_resolution_handles_unicode_and_spaces():
    assert local_target("EPUB/chapter.xhtml", "a%20book.xhtml#n%C3%A9") == (
        "EPUB/a book.xhtml",
        "né",
    )
    assert local_target("EPUB/chapter.xhtml", "https://example.org/book") is None
