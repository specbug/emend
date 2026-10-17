import hashlib
import json
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest

from emend.config import MetadataPolicy
from emend.research.http import EvidenceClient, ProviderError, SameProviderRedirect
from emend.research.isbn import choose_isbn, normalize_isbn
from emend.research.providers import google_books, openlibrary
from emend.research.service import reconcile
from emend.storage import now

ISBN = "9780140328721"
OL = f"https://openlibrary.org/isbn/{ISBN}.json"
AUTHOR = "https://openlibrary.org/authors/OL1A.json"
GOOGLE = f"https://www.googleapis.com/books/v1/volumes?q=isbn:{ISBN}&maxResults=10"


def provider_payloads():
    # Synthetic edition evidence for our original test corpus, not a claim about this ISBN.
    return {
        OL: {
            "key": "/books/OL1M",
            "isbn_13": [ISBN],
            "title": "The Quiet Observatory",
            "authors": [{"key": "/authors/OL1A"}],
            "publishers": ["Observatory Press"],
            "publish_date": "March 4, 2020",
            "languages": [{"key": "/languages/eng"}],
            "covers": [123, -1],
        },
        AUTHOR: {"name": "Emend Test Corpus"},
        GOOGLE: {
            "items": [
                {
                    "id": "fixture1",
                    "volumeInfo": {
                        "title": "The Quiet Observatory",
                        "authors": ["Emend Test Corpus"],
                        "publisher": "Observatory Press",
                        "publishedDate": "2020-03-04",
                        "language": "en",
                        "industryIdentifiers": [{"type": "ISBN_13", "identifier": ISBN}],
                    },
                }
            ]
        },
    }


def evidence_client(tmp_path, payloads=None):
    client = EvidenceClient(tmp_path / "cache", MetadataPolicy(), offline=True)
    for url, payload in (provider_payloads() if payloads is None else payloads).items():
        client.remember(url, json.dumps(payload).encode(), now())
    return client


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0-14-032872-6", ISBN),
        ("urn:isbn:9780140328721", ISBN),
        ("ISBN-13: 978-0-14-032872-1", ISBN),
        ("9780140328722", None),
        ("urn:uuid:1234", None),
        ("080442957X", "9780804429573"),
        ("000000000X", None),
    ],
)
def test_isbn_checksums(value, expected):
    assert normalize_isbn(value) == expected


def test_isbn_requires_existing_unambiguous_edition():
    assert choose_isbn([ISBN, "0140328726"]) == ISBN
    with pytest.raises(ValueError, match="one valid ISBN"):
        choose_isbn([ISBN, "9780804429573"])
    with pytest.raises(ValueError, match="already present"):
        choose_isbn([ISBN], "9780804429573")


def test_providers_filter_edition_and_keep_cover_provenance(tmp_path):
    client = evidence_client(tmp_path)
    ol, google = openlibrary(ISBN, client)[0], google_books(ISBN, client)[0]
    assert ol.date == google.date == ["2020-03-04"]
    assert ol.creators == google.creators == ["Emend Test Corpus"]
    assert ol.language == ["en"]
    assert ol.evidence_urls == [OL, AUTHOR]
    assert len(ol.covers) == 1 and ol.covers[0].source_url.endswith("/books/OL1M")
    assert not openlibrary(
        "9780804429573",
        evidence_client(
            tmp_path, {"https://openlibrary.org/isbn/9780804429573.json": provider_payloads()[OL]}
        ),
    )


def test_unsafe_author_url_is_never_fetched(tmp_path):
    data = provider_payloads()
    data[OL]["authors"] = [{"key": "https://attacker.invalid/key"}]
    with pytest.raises(ProviderError, match="author reference"):
        openlibrary(ISBN, evidence_client(tmp_path, data))


def test_reconciliation_requires_corroboration_and_preserves_existing(tmp_path):
    client = evidence_client(tmp_path)
    editions = openlibrary(ISBN, client) + google_books(ISBN, client)
    metadata = {
        "title": ["The Quiet Observatory"],
        "creator": ["Emend Test Corpus"],
        "language": ["en"],
    }
    proposals = reconcile(metadata, editions, {})
    assert {p.field for p in proposals if p.decision == "safe"} == {"publisher", "date"}
    assert all(p.decision == "review" for p in reconcile(metadata, editions[:1], {}))
    assert all(
        p.decision == "review" for p in reconcile(metadata, editions, {"openlibrary": "timeout"})
    )
    metadata["publisher"] = ["Keep this publisher"]
    assert (
        next(p for p in reconcile(metadata, editions, {}) if p.field == "publisher").decision
        == "review"
    )
    for field, value in [("creators", []), ("title", "Wrong edition"), ("language", ["fr"])]:
        changed = [editions[0].model_copy(update={field: value}), editions[1]]
        assert all(p.decision == "review" for p in reconcile(metadata, changed, {}))


def test_conflicting_or_multivalued_provider_records_are_not_safe(tmp_path):
    client = evidence_client(tmp_path)
    editions = openlibrary(ISBN, client) + google_books(ISBN, client)
    metadata = {"title": ["The Quiet Observatory"], "creator": ["Emend Test Corpus"]}
    for publishers in [["Other press"], ["Observatory Press", "Other press"]]:
        extra = editions[1].model_copy(
            update={"record_id": "google_books:second", "publisher": publishers}
        )
        assert all(
            p.decision == "review"
            for p in reconcile(metadata, editions + [extra], {})
            if p.field == "publisher"
        )


def cache_entry(tmp_path, body=b'{"key": "value"}', stamp=None):
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    path = cache / (hashlib.sha256(OL.encode()).hexdigest() + ".json")
    path.write_text(
        json.dumps(
            {
                "url": OL,
                "body": body.decode(),
                "sha256": hashlib.sha256(body).hexdigest(),
                "fetched_at": stamp or now(),
            }
        )
    )
    return path


def test_offline_cache_checks_age_integrity_and_shape(tmp_path):
    path = cache_entry(tmp_path)
    assert EvidenceClient(path.parent, MetadataPolicy(), offline=True).get(OL) == {"key": "value"}
    cache_entry(tmp_path, stamp=(datetime.now(UTC) - timedelta(days=8)).isoformat())
    with pytest.raises(ProviderError, match="fresh cached"):
        EvidenceClient(path.parent, MetadataPolicy(), offline=True).get(OL)
    data = json.loads(path.read_text())
    data["body"] = "{}"
    path.write_text(json.dumps(data))
    with pytest.raises(ProviderError, match="integrity"):
        EvidenceClient(path.parent, MetadataPolicy(), offline=True).get(OL)
    cache_entry(tmp_path, body=b"[]")
    with pytest.raises(ProviderError, match="must be an object"):
        EvidenceClient(path.parent, MetadataPolicy(), offline=True).get(OL)


@pytest.mark.parametrize(
    "url",
    [
        "http://openlibrary.org/book",
        "https://127.0.0.1/",
        "https://openlibrary.org@attacker.invalid/",
        "https://openlibrary.org:8080/",
    ],
)
def test_request_destinations_are_restricted(tmp_path, url):
    with pytest.raises(ProviderError, match="HTTPS destinations"):
        EvidenceClient(tmp_path, MetadataPolicy()).get(url)


def test_cross_provider_redirect_is_rejected():
    with pytest.raises(ProviderError, match="Cross-provider"):
        SameProviderRedirect().redirect_request(
            urllib.request.Request(OL), None, 302, "Found", {}, GOOGLE
        )


def test_network_response_is_cached_and_refreshable(tmp_path, monkeypatch):
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def geturl(self):
            return OL

        def read(self, limit):
            return b'{"network":true}'

    class Opener:
        def open(self, request, timeout):
            calls.append((request.full_url, timeout))
            return Response()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: Opener())
    client = EvidenceClient(tmp_path, MetadataPolicy())
    assert client.get(OL) == {"network": True}
    assert len(calls) == 1 and client.documents[OL].sha256
    assert EvidenceClient(tmp_path, MetadataPolicy(), offline=True).get(OL) == {"network": True}
    EvidenceClient(tmp_path, MetadataPolicy(), refresh=True).get(OL)
    assert len(calls) == 2


def test_oversized_response_rejected(tmp_path, monkeypatch):
    from emend.research import http

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def geturl(self):
            return OL

        def read(self, limit):
            return b" " * limit

    class Opener:
        def open(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: Opener())
    monkeypatch.setattr(http, "MAX_RESPONSE", 100)
    with pytest.raises(ProviderError, match="size limit"):
        EvidenceClient(tmp_path, MetadataPolicy()).get(OL)
