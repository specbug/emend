import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from emend.cli import app
from emend.cover_apply import apply_cover
from emend.covers import cover_review
from emend.curation import apply_proposals
from emend.inspect import sha256_file
from emend.library import library_snapshot, link, write_library
from emend.pipeline import ingest
from emend.research.discovery import discover
from emend.research.service import research
from emend.storage import Store
from emend.visual import VisualReview
from tests.epub_factory import book_files, write_epub
from tests.test_covers import candidate, cover_book


def test_empty_library_and_cli(tmp_path, config):
    snapshot = library_snapshot(config)
    assert snapshot.total_books == 0 and snapshot.books == []
    path = write_library(snapshot, config)
    assert "Your library starts here" in path.read_text()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    result = CliRunner().invoke(app, ["library", "--config", str(config_path), "--json"])
    assert result.exit_code == 0 and json.loads(result.output)["total_books"] == 0
    result = CliRunner().invoke(app, ["library", "--config", str(config_path)])
    assert result.exit_code == 0 and "library.html" in result.output


def test_overview_preserves_books_and_links_reports(tmp_path, config):
    report = ingest(write_epub(tmp_path / "book.epub"), config)
    store = Store(config.root)
    before = store.books()
    with store.db() as db:
        events = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    snapshot = library_snapshot(config)
    book = snapshot.books[0]
    assert book.title == "The Quiet Observatory" and book.state == "VALIDATED"
    assert book.creators == ["Emend Test Corpus"] and not book.issues
    assert book.next_step == f"emend visual-review {report.book_id[:12]}"
    assert book.reviews[0].status == "current" and book.reviews[0].href.endswith("report.html")
    assert (config.root / book.reviews[0].href).is_file()
    path = write_library(snapshot, config)
    assert "<script" not in path.read_text()
    assert store.books() == before
    assert sha256_file(Path(before[0]["artifact_path"])) == report.book_id
    Path(before[0]["report_path"]).with_suffix(".html").unlink()
    assert library_snapshot(config).books[0].reviews[0].href.endswith("report.json")
    with store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events


def test_proposals_and_cover_reviews_track_application(tmp_path, config, search_responses):
    report = cover_book(tmp_path, config)
    discovery = discover(report.book_id, config)
    plan = research(
        report.book_id, config, discovery_id=discovery.id, edition_id="openlibrary:/books/OL1M"
    )
    cover = cover_review(report.book_id, config, image=candidate(tmp_path))
    before = library_snapshot(config).books[0]
    assert before.pending_proposals == len(plan.proposals) > 0
    assert "proposals" in before.next_step
    assert next(r for r in before.reviews if r.label == "Cover comparison").status == "current"
    publisher = next(p for p in plan.proposals if p.field == "publisher")
    apply_proposals(report.book_id, config, approve=[publisher.id])
    after = library_snapshot(config).books[0]
    assert after.pending_proposals == 0
    assert next(r for r in after.reviews if r.label == "Metadata proposals").status == "applied"
    assert next(r for r in after.reviews if r.label == "Cover comparison").status == "earlier"
    cover = cover_review(report.book_id, config, image=candidate(tmp_path))
    apply_cover(report.book_id, config, review_id=cover.id, approve=cover.candidates[0].id)
    assert (
        next(
            r for r in library_snapshot(config).books[0].reviews if r.label == "Cover comparison"
        ).status
        == "applied"
    )


def test_corrupt_evidence_and_missing_reports_do_not_hide_other_books(
    tmp_path, config, search_responses
):
    report = cover_book(tmp_path, config)
    discovery = discover(report.book_id, config)
    store = Store(config.root)
    record = store.document(report.book_id, "discovery", discovery.id)
    Path(record["path"]).write_text("tampered")
    files = book_files()
    files.pop("EPUB/observatory.png")
    failed = ingest(write_epub(tmp_path / "broken.epub", files), config)
    Path(store.resolve(failed.book_id)["report_path"]).unlink()
    snapshot = library_snapshot(config)
    assert len(snapshot.books) == 2 and all(b.issues for b in snapshot.books)
    good_book = next(b for b in snapshot.books if b.book_id == report.book_id)
    assert any(
        r.status == "unavailable" and "changed after registration" in r.detail
        for r in good_book.reviews
    )
    assert write_library(snapshot, config).read_text().count("<article>") == 2


def test_policy_changes_are_visible_and_do_not_count_old_proposals(
    tmp_path, config, search_responses
):
    report = cover_book(tmp_path, config)
    discovery = discover(report.book_id, config)
    research(
        report.book_id, config, discovery_id=discovery.id, edition_id="openlibrary:/books/OL1M"
    )
    config.metadata.cache_days += 1
    config.quality.require_accessibility_audit = True
    book = library_snapshot(config).books[0]
    assert book.issues and book.pending_proposals == 0
    assert all(r.status == "earlier" for r in book.reviews)


def test_links_and_html_escape_untrusted_names(tmp_path, config):
    files = book_files()
    files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
        b"The Quiet Observatory", b"&lt;script&gt;alert(1)&lt;/script&gt;"
    )
    ingest(write_epub(tmp_path / "bad-name.epub", files), config)
    output = write_library(library_snapshot(config), config).read_text()
    assert "<script>" not in output and "&lt;script&gt;" in output
    external = tmp_path / "private.txt"
    external.write_text("private")
    assert link(config.root, external) is None
    symlink = config.root / "outside"
    symlink.symlink_to(external)
    assert link(config.root, symlink) is None
    local = config.root / "report #1.html"
    local.write_text("report")
    assert link(config.root, local) == "report%20%231.html"


def test_overview_limits_and_missing_artifact(tmp_path, config):
    first = ingest(write_epub(tmp_path / "first.epub", version="2.0"), config)
    second = ingest(write_epub(tmp_path / "second.epub"), config)
    snapshot = library_snapshot(config, limit=1)
    assert snapshot.total_books == 2 and snapshot.books[0].book_id == second.book_id
    assert library_snapshot(config, limit=1, offset=1).books[0].book_id == first.book_id
    with pytest.raises(ValueError, match="nonnegative"):
        library_snapshot(config, offset=-1)
    beyond = library_snapshot(config, offset=2)
    assert "No books at this offset" in write_library(beyond, config).read_text()
    for limit in [0, 1001]:
        with pytest.raises(ValueError, match="between"):
            library_snapshot(config, limit)
    Path(Store(config.root).resolve(first.book_id)["artifact_path"]).unlink()
    broken = next(b for b in library_snapshot(config).books if b.book_id == first.book_id)
    assert broken.issues and broken.reviews[0].status == "unavailable"


def test_incomplete_visual_review_is_not_presented_as_complete(tmp_path, config):
    report = ingest(write_epub(tmp_path / "book.epub"), config)
    review = VisualReview(
        id="test",
        book_id=report.book_id,
        artifact_sha256=report.book_id,
        engine_version=report.engine_version,
        created_at=report.created_at,
        browser_version="fixture",
        title="Book",
        total_spine_documents=2,
        selected_documents=["EPUB/chapter.xhtml"],
        profiles=["compact"],
        status="incomplete",
        samples=[],
    )
    path = config.root / "visual-test.json"
    path.write_text(review.model_dump_json())
    path.with_suffix(".html").write_text("fixture")
    Store(config.root).save_document(
        review.id, report.book_id, "visual-review", path, sha256_file(path)
    )
    book = library_snapshot(config).books[0]
    assert next(r for r in book.reviews if r.label == "Visual samples").status == "incomplete"
    assert "visual-review" in book.next_step


def test_meta_advice_requires_intact_current_plan(tmp_path, config, search_responses, monkeypatch):
    from emend.research import ai_review
    from emend.research.meta import ReviewAnswer
    from tests.test_meta_review import answer_for

    report = cover_book(tmp_path, config)
    discovery = discover(report.book_id, config)
    plan = research(
        report.book_id, config, discovery_id=discovery.id, edition_id="openlibrary:/books/OL1M"
    )
    monkeypatch.setattr(
        ai_review,
        "call_meta",
        lambda *args: (ReviewAnswer.model_validate(answer_for(plan)), {}, "a" * 64),
    )
    ai_review.review_plan(report.book_id, config)
    book = library_snapshot(config).books[0]
    assert next(r for r in book.reviews if r.label == "Meta advice").status == "current"
    config.metadata.cache_days += 1
    book = library_snapshot(config).books[0]
    assert next(r for r in book.reviews if r.label == "Meta advice").status == "earlier"
    path = Path(Store(config.root).plan(report.book_id, plan.id)["path"])
    path.write_text("tampered")
    book = library_snapshot(config).books[0]
    assert next(r for r in book.reviews if r.label == "Meta advice").status == "unavailable"
