"""Title/author discovery produces candidates, never an automatic edition match."""

import re
import uuid
from pathlib import Path
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field

from emend import ENGINE_VERSION
from emend.config import Config
from emend.inspect import inspect_epub, sha256_file
from emend.pipeline import get_report
from emend.research.http import EvidenceClient, ProviderError
from emend.research.isbn import normalize_isbn
from emend.research.models import Edition, Evidence
from emend.research.providers import google_editions, openlibrary_edition
from emend.storage import Store, now


class EditionCandidate(BaseModel):
    edition: Edition
    matches: list[str]
    differences: list[str]


class Discovery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    book_id: str
    artifact_sha256: str
    engine_version: str
    policy_sha256: str
    created_at: str
    title: str
    author: str
    metadata: dict[str, list[str]]
    candidates: list[EditionCandidate]
    documents: list[Evidence]
    provider_errors: dict[str, str]
    notes: list[str] = Field(
        default_factory=lambda: [
            "Search results are candidates, not confirmed edition matches. Ordering is not a confidence score.",
            "Review publisher, publication date, language, and cover before selecting one record.",
            "Selected discovery records never create safe proposals or replace an EPUB identifier.",
            "Search is bounded to a small first page; other editions may exist.",
        ]
    )


def current_source(store: Store, book_id: str, config: Config):
    row = store.resolve(book_id)
    report = get_report(store, row["id"])
    original = config.root / "originals" / f"{row['id']}.epub"
    if sha256_file(original) != row["id"]:
        raise ValueError("Stored original hash mismatch.")
    source = Path(row["artifact_path"]) if row["artifact_path"] else original
    expected = report.artifact_sha256 if row["artifact_path"] else row["id"]
    if report.book_id != row["id"] or sha256_file(source) != expected:
        raise ValueError("Current book/report hash mismatch.")
    return row, source, expected


def search_openlibrary(title: str, author: str, client: EvidenceClient) -> list[Edition]:
    url = "https://openlibrary.org/search.json?" + urlencode(
        {
            "title": title,
            "author": author,
            "limit": 5,
            "fields": "key,title,author_name,edition_key",
        }
    )
    data = client.get(url)
    docs = data.get("docs")
    if not isinstance(docs, list):
        raise ProviderError("Open Library returned an invalid search result.")
    keys = []
    for work in docs[:5]:
        if not isinstance(work, dict) or not isinstance(work.get("edition_key"), list):
            continue
        for key in work["edition_key"][:2]:
            if isinstance(key, str) and re.fullmatch(r"OL[0-9]+M", key) and key not in keys:
                keys.append(key)
    editions = []
    for key in keys:
        edition_url = f"https://openlibrary.org/books/{key}.json"
        record = client.get(edition_url)
        if record.get("key") != "/books/" + key:
            raise ProviderError("Open Library edition identity differs from the requested record.")
        edition = openlibrary_edition(record, edition_url, client)
        edition.evidence_urls = [url, *edition.evidence_urls]
        editions.append(edition)
    return editions


def search_google(title: str, author: str, client: EvidenceClient) -> list[Edition]:
    # Quote each value as a search phrase; never treat source text as query syntax.
    title, author = title.replace('"', " "), author.replace('"', " ")
    url = "https://www.googleapis.com/books/v1/volumes?" + urlencode(
        {"q": f'intitle:"{title}" inauthor:"{author}"', "maxResults": 10, "printType": "books"}
    )
    return google_editions(client.get(url), url)


def compare(edition: Edition, metadata: dict[str, list[str]]) -> EditionCandidate:
    def normalize(values):
        return [" ".join(v.casefold().split()) for v in values]

    matches, differences = [], []
    for field, values in [
        ("title", [edition.title]),
        ("creator", edition.creators),
        ("language", edition.language),
        ("publisher", edition.publisher),
        ("date", edition.date),
    ]:
        original = metadata.get(field, [])
        if original and values and normalize(original) == normalize(values):
            matches.append(field)
        elif original and values:
            differences.append(f"{field}: EPUB {'; '.join(original)} / record {'; '.join(values)}")
        elif not values:
            differences.append(f"{field}: absent from candidate record")
        else:
            differences.append(f"{field}: absent from EPUB")
    return EditionCandidate(edition=edition, matches=matches, differences=differences)


def discover(
    book_id: str,
    config: Config,
    *,
    title: str | None = None,
    author: str | None = None,
    offline: bool = False,
    refresh: bool = False,
) -> Discovery:
    store = Store(config.root)
    with store.locked():
        row, source, digest = current_source(store, book_id, config)
        inspection = inspect_epub(source, config.limits)
        if inspection.blocking:
            raise ValueError("Book inspection failed; resolve structural issues before discovery.")
        if any(normalize_isbn(value) for value in inspection.metadata.get("identifier", [])):
            raise ValueError(
                "This EPUB has an ISBN. Use emend research and --isbn for exact-ISBN research."
            )
        titles, authors = (
            inspection.metadata.get("title", []),
            inspection.metadata.get("creator", []),
        )
        title = title if title is not None else titles[0] if len(titles) == 1 else ""
        author = author if author is not None else authors[0] if len(authors) == 1 else ""
        if any(
            not value.strip() or len(value) > 300 or any(ord(c) < 32 for c in value)
            for value in [title, author]
        ):
            raise ValueError(
                "Supply one nonempty --title and --author (up to 300 characters each)."
            )
        client = EvidenceClient(
            config.root / "cache" / "metadata", config.metadata, offline=offline, refresh=refresh
        )
        candidates, errors = [], {}
        providers = {"openlibrary": search_openlibrary, "google_books": search_google}
        for provider in dict.fromkeys(config.metadata.sources):
            try:
                candidates += [
                    compare(e, inspection.metadata)
                    for e in providers[provider](title, author, client)
                ]
            except (ProviderError, OSError, TypeError, KeyError) as exc:
                errors[provider] = str(exc)
        result = Discovery(
            id=uuid.uuid4().hex,
            book_id=row["id"],
            artifact_sha256=digest,
            engine_version=ENGINE_VERSION,
            policy_sha256=config.metadata.fingerprint(),
            created_at=now(),
            title=title,
            author=author,
            metadata=inspection.metadata,
            candidates=candidates,
            documents=list(client.documents.values()),
            provider_errors=errors,
        )
        directory = config.root / "research" / row["id"] / "discoveries"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{result.id}.json"
        path.write_text(result.model_dump_json(indent=2))
        store.save_document(result.id, row["id"], "discovery", path, sha256_file(path))
        store.event(
            row["id"],
            "EDITIONS_DISCOVERED",
            f"Discovery {result.id}: {len(candidates)} candidates; no edition selected.",
        )
        return result


def load_discovery(store: Store, book_id: str, identifier: str | None = None) -> Discovery:
    row = store.document(book_id, "discovery", identifier)
    path = Path(row["path"])
    if sha256_file(path) != row["sha256"]:
        raise ValueError("Discovery changed after registration; run discover again.")
    result = Discovery.model_validate_json(path.read_text())
    if result.id != row["id"] or result.book_id != book_id:
        raise ValueError("Discovery identity mismatch.")
    return result


def discovery_text(result: Discovery) -> str:
    lines = [
        f"Edition discovery {result.id}",
        f"Query: {result.title} / {result.author}",
        "Candidates require explicit edition review.",
        "",
    ]
    for candidate in result.candidates:
        edition = candidate.edition
        lines += [
            f"{edition.record_id} · {edition.title}",
            f"  Authors: {'; '.join(edition.creators) or '(missing)'}",
            f"  ISBN: {edition.isbn or '(missing)'} · Publisher: {'; '.join(edition.publisher) or '(missing)'} · Date: {'; '.join(edition.date) or '(missing)'}",
            f"  Source: {edition.source_url}",
            f"  Matching fields: {', '.join(candidate.matches) or '(none)'}",
            *[f"  Review: {item}" for item in candidate.differences],
            "",
        ]
    lines += [
        f"UNAVAILABLE {provider}: {error}" for provider, error in result.provider_errors.items()
    ]
    lines += result.notes
    lines += [
        f"Select: emend research {result.book_id[:12]} --discovery {result.id} --edition RECORD_ID"
    ]
    return "\n".join(lines)
