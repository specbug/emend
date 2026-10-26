import json
from importlib.resources import files as package_files
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from emend.cli import app
from emend.pipeline import get_report, ingest
from emend.reading_review import load_reading_review, reading_review
from emend.storage import Store
from tests.epub_factory import book_files, write_epub


def marked_book(*, backlink=True, semantic=True, reference_id=True):
    files = book_files()
    files["EPUB/chapter.xhtml"] = files["EPUB/chapter.xhtml"].replace(
        b'id="ref1"', b'id="ref1" role="doc-noteref"' if reference_id else b'role="doc-noteref"'
    )
    files["EPUB/notes.xhtml"] = files["EPUB/notes.xhtml"].replace(
        b'id="note1"', b'id="note1" role="doc-footnote"' if semantic else b'id="note1"'
    )
    if not backlink:
        files["EPUB/notes.xhtml"] = files["EPUB/notes.xhtml"].replace(
            b'<a href="chapter.xhtml#ref1">Return</a>', b"No backlink"
        )
    files["EPUB/type.ttf"] = package_files("emend").joinpath("assets", "DMSans.ttf").read_bytes()
    files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
        b"</manifest>", b'<item id="type" href="type.ttf" media-type="font/ttf"/></manifest>'
    )
    files["EPUB/style.css"] += (
        b'@font-face{font-family:Sample;src:url("type.ttf")} p{font-family:Sample,serif}'
    )
    return files


def test_all_book_note_font_evidence_preserves_bytes_and_state(tmp_path, config):
    before = ingest(write_epub(tmp_path / "book.epub", marked_book()), config)
    store = Store(config.root)
    row = store.resolve(before.book_id)
    result = reading_review(before.book_id, config)
    assert result.complete and len(result.notes) == 1 and len(result.fonts) == 1
    assert result.notes[0].reciprocal_backlink and result.notes[0].semantic_note
    assert result.fonts[0].declared_in == ["EPUB/style.css"]
    assert result.fonts[0].sha256 == before.inspection.resource_hashes["EPUB/type.ttf"]
    assert store.resolve(before.book_id) == row and get_report(store, before.book_id) == before
    assert load_reading_review(store, before.book_id, result.id) == result
    assert not [f for f in result.findings if f.code.startswith("note.")]


@pytest.mark.parametrize(
    "backlink,semantic,reference_id,code",
    [
        (False, True, True, "note.backlink"),
        (True, False, True, "note.semantics"),
        (False, True, False, "note.backlink"),
    ],
)
def test_missing_note_evidence_remains_actionable(
    tmp_path, config, backlink, semantic, reference_id, code
):
    files = marked_book(backlink=backlink, semantic=semantic, reference_id=reference_id)
    if not reference_id:
        files["EPUB/notes.xhtml"] = files["EPUB/notes.xhtml"].replace(
            b"chapter.xhtml#ref1", b"chapter.xhtml"
        )
    before = ingest(write_epub(tmp_path / "notes.epub", files), config)
    result = reading_review(before.book_id, config)
    assert result.complete and any(f.code == code for f in result.findings)


def test_unmarked_links_are_not_silently_classified_as_notes(tmp_path, config):
    before = ingest(write_epub(tmp_path / "plain.epub"), config)
    result = reading_review(before.book_id, config)
    assert result.notes == []
    path = Path(Store(config.root).document(before.book_id, "reading-review", result.id)["path"])
    assert "does not establish that the book has no notes" in path.with_suffix(".html").read_text()


def test_obfuscated_font_and_local_font_sources_are_distinguished(tmp_path, config):
    files = marked_book()
    files["META-INF/encryption.xml"] = (
        b"""<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><EncryptedData xmlns="http://www.w3.org/2001/04/xmlenc#"><EncryptionMethod Algorithm="http://www.idpf.org/2008/embedding"/><CipherData><CipherReference URI="EPUB/type.ttf"/></CipherData></EncryptedData></encryption>"""
    )
    files["EPUB/style.css"] += b'@font-face{font-family:Fallback;src:local("Some Local Font")}'
    before = ingest(write_epub(tmp_path / "font.epub", files), config)
    result = reading_review(before.book_id, config)
    assert result.fonts[0].obfuscated
    assert {"font.obfuscated", "font.local"} <= {f.code for f in result.findings}


def test_nested_font_faces_and_inline_style_sources_are_recorded(tmp_path, config):
    files = marked_book()
    files["EPUB/style.css"] += (
        b"@media screen{@supports (font-size:1em){@font-face{font-family:Nested;src:url(type.ttf)}}}"
    )
    files["EPUB/chapter.xhtml"] = files["EPUB/chapter.xhtml"].replace(
        b"</head>", b"<style>@font-face{font-family:Inline;src:url(type.ttf)}</style></head>"
    )
    before = ingest(write_epub(tmp_path / "nested.epub", files), config)
    result = reading_review(before.book_id, config)
    assert result.fonts[0].declared_in == ["EPUB/chapter.xhtml", "EPUB/style.css"]


@pytest.mark.parametrize("kind", ["documents", "fonts", "references"])
def test_bounded_review_is_incomplete_not_a_pass(tmp_path, config, monkeypatch, kind):
    constant = {"documents": "MAX_DOCUMENTS", "fonts": "MAX_FONTS", "references": "MAX_REFERENCES"}[
        kind
    ]
    monkeypatch.setattr("emend.reading_review." + constant, 0)
    before = ingest(write_epub(tmp_path / "bounded.epub", marked_book()), config)
    result = reading_review(before.book_id, config)
    assert not result.complete and any(f.code == "review.limit" for f in result.findings)


def test_language_direction_and_markup_are_escaped(tmp_path, config):
    files = marked_book()
    files["EPUB/chapter.xhtml"] = files["EPUB/chapter.xhtml"].replace(
        b'xml:lang="en"', b'dir="rtl"'
    )
    files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
        b"The Quiet Observatory", b"&lt;script&gt;not executable&lt;/script&gt;"
    )
    files["EPUB/style.css"] += b"body{writing-mode:vertical-rl}"
    before = ingest(write_epub(tmp_path / "direction.epub", files), config)
    result = reading_review(before.book_id, config)
    assert {"document.direction", "document.language", "layout.direction_css"} <= {
        f.code for f in result.findings
    }
    path = Path(Store(config.root).document(before.book_id, "reading-review", result.id)["path"])
    html = path.with_suffix(".html").read_text()
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_tampered_review_is_rejected(tmp_path, config):
    before = ingest(write_epub(tmp_path / "book.epub"), config)
    result = reading_review(before.book_id, config)
    store = Store(config.root)
    path = Path(store.document(before.book_id, "reading-review", result.id)["path"])
    path.write_text(path.read_text() + "\n")
    with pytest.raises(ValueError, match="changed"):
        load_reading_review(store, before.book_id, result.id)


def test_cli_json_and_incomplete_exit_status(tmp_path, config, monkeypatch):
    before = ingest(write_epub(tmp_path / "book.epub"), config)
    settings = tmp_path / "config.yaml"
    settings.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    runner = CliRunner()
    args = ["reading-review", before.book_id, "--json", "-c", str(settings)]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["current_artifact"]
    result = runner.invoke(app, [*args, "--show"])
    assert result.exit_code == 0
    monkeypatch.setattr("emend.reading_review.MAX_DOCUMENTS", 0)
    assert runner.invoke(app, args).exit_code == 1


def test_css_comments_do_not_become_direction_declarations(tmp_path, config):
    files = book_files()
    files["EPUB/style.css"] += b"/* direction: rtl; writing-mode: vertical-rl */"
    before = ingest(write_epub(tmp_path / "comment.epub", files), config)
    assert not any(
        f.code == "layout.direction_css" for f in reading_review(before.book_id, config).findings
    )
