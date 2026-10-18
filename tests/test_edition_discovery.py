import json
from pathlib import Path
from urllib.parse import urlencode

import pytest
import yaml
from typer.testing import CliRunner

from emend.cli import app
from emend.curation import apply_proposals
from emend.pipeline import ingest
from emend.research.discovery import discover, load_discovery
from emend.research.http import EvidenceClient, ProviderError
from emend.research.meta import request_body
from emend.research.service import research
from emend.storage import Store, now
from tests.test_research import ISBN, provider_payloads


@pytest.fixture
def search_responses(monkeypatch):
    payloads = provider_payloads()
    payloads["https://openlibrary.org/books/OL1M.json"] = payloads.pop(
        f"https://openlibrary.org/isbn/{ISBN}.json"
    )
    payloads[
        "https://openlibrary.org/search.json?"
        + urlencode(
            {
                "title": "The Quiet Observatory",
                "author": "Emend Test Corpus",
                "limit": 5,
                "fields": "key,title,author_name,edition_key",
            }
        )
    ] = {
        "docs": [
            {
                "key": "/works/OL1W",
                "title": "WORK DATA MUST NOT BECOME EDITION METADATA",
                "edition_key": ["OL1M"],
            }
        ]
    }
    google = payloads.pop(
        f"https://www.googleapis.com/books/v1/volumes?q=isbn:{ISBN}&maxResults=10"
    )
    payloads[
        "https://www.googleapis.com/books/v1/volumes?"
        + urlencode(
            {
                "q": 'intitle:"The Quiet Observatory" inauthor:"Emend Test Corpus"',
                "maxResults": 10,
                "printType": "books",
            }
        )
    ] = google
    calls = []

    def get(self, url):
        calls.append(url)
        if url not in payloads:
            raise ProviderError("Fixture source unavailable")
        value = payloads[url]
        if isinstance(value, Exception):
            raise value
        return self.remember(url, json.dumps(value).encode(), now())

    monkeypatch.setattr(EvidenceClient, "get", get)
    return payloads, calls


def test_discovery_uses_edition_records_and_explicit_selection(epub, config, search_responses):
    before = ingest(epub, config)
    result = discover(before.book_id, config)
    assert len(result.candidates) == 2 and not result.provider_errors
    assert result.candidates[0].edition.title == "The Quiet Observatory"
    assert "title" in result.candidates[0].matches
    assert any("publisher:" in v for v in result.candidates[0].differences)
    plan = research(
        before.book_id, config, discovery_id=result.id, edition_id="openlibrary:/books/OL1M"
    )
    assert plan.match_mode == "reviewed_edition" and plan.discovery_id == result.id
    assert plan.isbn == ISBN and all(p.decision == "review" for p in plan.proposals)
    assert len(plan.documents) >= 3
    with pytest.raises(ValueError, match="No proposals selected"):
        apply_proposals(before.book_id, config, safe=True)
    publisher = next(p for p in plan.proposals if p.field == "publisher")
    after = apply_proposals(before.book_id, config, approve=[publisher.id])
    assert after.inspection.metadata["identifier"] == before.inspection.metadata["identifier"]
    with pytest.raises(ValueError, match="stale"):
        research(
            before.book_id, config, discovery_id=result.id, edition_id="openlibrary:/books/OL1M"
        )
    context = json.loads(request_body(plan, config.ai_review)["messages"][1]["content"])
    assert context["match_mode"] == "reviewed_edition"


def test_candidates_without_isbn_still_can_be_selected(epub, config, search_responses):
    payloads, _ = search_responses
    payloads["https://openlibrary.org/books/OL1M.json"].pop("isbn_13")
    before = ingest(epub, config)
    result = discover(before.book_id, config)
    plan = research(before.book_id, config, edition_id="openlibrary:/books/OL1M")
    assert result.candidates[0].edition.isbn is None and plan.isbn is None
    assert all(p.decision == "review" for p in plan.proposals)


def test_failed_provider_does_not_erase_other_candidates(epub, config, search_responses):
    payloads, _ = search_responses
    for key in list(payloads):
        if "googleapis.com" in key:
            payloads[key] = ProviderError("HTTP 429")
    report = ingest(epub, config)
    result = discover(report.book_id, config)
    assert len(result.candidates) == 1 and "google_books" in result.provider_errors


def test_discovery_tampering_policy_changes_and_wrong_ids(epub, config, search_responses):
    report = ingest(epub, config)
    result = discover(report.book_id, config)
    with pytest.raises(ValueError, match="Choose one record"):
        research(report.book_id, config, edition_id="not-recorded")
    config.metadata.cache_days = 8
    with pytest.raises(ValueError, match="policy changed"):
        research(report.book_id, config, edition_id="openlibrary:/books/OL1M")
    config.metadata.cache_days = 7
    path = Path(Store(config.root).document(report.book_id, "discovery", result.id)["path"])
    path.write_text(path.read_text().replace("Observatory Press", "Altered Press"))
    with pytest.raises(ValueError, match="changed after registration"):
        load_discovery(Store(config.root), report.book_id, result.id)


def test_isbn_books_do_not_use_title_discovery(tmp_path, config, search_responses):
    from tests.test_curation import setup_book

    report, _ = setup_book(tmp_path, config)
    with pytest.raises(ValueError, match="has an ISBN"):
        discover(report.book_id, config)


def test_search_query_overrides_never_edit_metadata(epub, config, search_responses):
    report = ingest(epub, config)
    result = discover(report.book_id, config, title="A different title", author="Other Author")
    assert not result.candidates and len(result.provider_errors) == 2
    assert result.metadata["title"] == ["The Quiet Observatory"]
    assert result.title == "A different title"
    with pytest.raises(ValueError, match="nonempty"):
        discover(report.book_id, config, title="")


def test_cli_discovery_and_selected_plan(epub, config, tmp_path, search_responses):
    report = ingest(epub, config)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    args = [report.book_id[:12], "--config", str(path)]
    runner = CliRunner()
    result = runner.invoke(app, ["discover", *args])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["editions", *args, "--json"])
    assert result.exit_code == 0, result.output
    discovery = json.loads(result.output)
    result = runner.invoke(
        app,
        ["research", *args, "--discovery", discovery["id"], "--edition", "openlibrary:/books/OL1M"],
    )
    assert result.exit_code == 0, result.output
    assert "reviewed_edition" in result.output
