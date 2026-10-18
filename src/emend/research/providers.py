import re
from datetime import datetime

from emend.research.http import EvidenceClient, ProviderError
from emend.research.isbn import normalize_isbn
from emend.research.models import CoverCandidate, Edition

LANGUAGES = {
    "eng": "en",
    "fre": "fr",
    "fra": "fr",
    "ger": "de",
    "deu": "de",
    "spa": "es",
    "ita": "it",
    "por": "pt",
    "hin": "hi",
    "jpn": "ja",
    "chi": "zh",
    "zho": "zh",
    "rus": "ru",
    "ara": "ar",
}


def strings(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        item.strip()
        for item in value
        if isinstance(item, str) and item.strip() and len(item) <= 2000
    ]


def date_value(value) -> list[str]:
    if not isinstance(value, str):
        return []
    value = value.strip()
    for pattern, output in [
        ("%Y-%m-%d", "%Y-%m-%d"),
        ("%B %d, %Y", "%Y-%m-%d"),
        ("%b %d, %Y", "%Y-%m-%d"),
        ("%d %B %Y", "%Y-%m-%d"),
        ("%Y-%m", "%Y-%m"),
        ("%B %Y", "%Y-%m"),
        ("%Y", "%Y"),
    ]:
        try:
            return [datetime.strptime(value, pattern).strftime(output)]
        except ValueError:
            continue
    return []


def openlibrary(isbn: str, client: EvidenceClient) -> list[Edition]:
    url = f"https://openlibrary.org/isbn/{isbn}.json"
    data = client.get(url)
    identifiers = strings(data.get("isbn_13")) + strings(data.get("isbn_10"))
    if isbn not in {normalize_isbn(value) for value in identifiers}:
        return []
    return [openlibrary_edition(data, url, client, isbn)]


def openlibrary_edition(
    data: dict, url: str, client: EvidenceClient, isbn: str | None = None
) -> Edition:
    if isbn is None:
        isbn = next(
            (
                value
                for item in strings(data.get("isbn_13")) + strings(data.get("isbn_10"))
                if (value := normalize_isbn(item))
            ),
            None,
        )
    key = data.get("key", "")
    title = data.get("title")
    if (
        not isinstance(key, str)
        or not re.fullmatch(r"/books/OL[0-9]+M", key)
        or not isinstance(title, str)
        or not title.strip()
    ):
        raise ProviderError("Open Library returned an invalid edition record.")
    urls = [url]
    creators = []
    authors = data.get("authors", [])
    if not isinstance(authors, list) or len(authors) > 20:
        raise ProviderError("Open Library returned invalid author references.")
    for author in authors:
        author_key = author.get("key") if isinstance(author, dict) else None
        if not isinstance(author_key, str) or not re.fullmatch(r"/authors/OL[0-9]+A", author_key):
            raise ProviderError("Open Library returned an invalid author reference.")
        author_url = "https://openlibrary.org" + author_key + ".json"
        name = client.get(author_url).get("name")
        if not isinstance(name, str) or not name.strip():
            raise ProviderError("Open Library author name is missing.")
        creators.append(name.strip())
        urls.append(author_url)
    languages = []
    for item in data.get("languages", []) if isinstance(data.get("languages", []), list) else []:
        if isinstance(item, dict):
            code = str(item.get("key", "")).removeprefix("/languages/")
            if code in LANGUAGES:
                languages.append(LANGUAGES[code])
    covers = (
        [
            CoverCandidate(
                url=f"https://covers.openlibrary.org/b/id/{cover}-L.jpg?default=false",
                source_url="https://openlibrary.org" + key,
            )
            for cover in data.get("covers", [])
            if type(cover) is int and cover > 0
        ][:5]
        if isinstance(data.get("covers", []), list)
        else []
    )
    return Edition(
        provider="openlibrary",
        record_id="openlibrary:" + key,
        source_url="https://openlibrary.org" + key,
        isbn=isbn,
        title=title.strip(),
        creators=creators,
        publisher=strings(data.get("publishers")),
        date=date_value(data.get("publish_date")),
        language=languages,
        covers=covers,
        evidence_urls=urls,
    )


def google_books(isbn: str, client: EvidenceClient) -> list[Edition]:
    url = f"https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}&maxResults=10"
    data = client.get(url)
    return google_editions(data, url, isbn)


def google_editions(data: dict, url: str, isbn: str | None = None) -> list[Edition]:
    items = data.get("items", [])
    if not isinstance(items, list):
        raise ProviderError("Google Books returned an invalid item list.")
    editions = []
    for item in items[:10]:
        if not isinstance(item, dict) or not isinstance(item.get("volumeInfo"), dict):
            continue
        info = item["volumeInfo"]
        identifiers = info.get("industryIdentifiers", [])
        found = (
            {
                value
                for item in identifiers
                if isinstance(item, dict)
                and isinstance(item.get("identifier"), str)
                and (value := normalize_isbn(item["identifier"]))
            }
            if isinstance(identifiers, list)
            else set()
        )
        if isbn is not None and isbn not in found:
            continue
        identifier, title = item.get("id"), info.get("title")
        if (
            not isinstance(identifier, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", identifier)
            or not isinstance(title, str)
            or not title.strip()
        ):
            continue
        language = info.get("language")
        editions.append(
            Edition(
                provider="google_books",
                record_id="google_books:" + identifier,
                source_url="https://books.google.com/books?id=" + identifier,
                isbn=isbn or next(iter(sorted(found)), None),
                title=title.strip(),
                creators=strings(info.get("authors")),
                publisher=strings([info.get("publisher")]),
                date=date_value(info.get("publishedDate")),
                language=[language]
                if isinstance(language, str)
                and re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]+)*", language)
                else [],
                evidence_urls=[url],
            )
        )
    return editions


PROVIDERS = {"openlibrary": openlibrary, "google_books": google_books}
