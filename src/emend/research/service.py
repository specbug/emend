import hashlib
import json
import unicodedata
import uuid
from pathlib import Path

from emend import ENGINE_VERSION
from emend.config import Config
from emend.inspect import inspect_epub, sha256_file
from emend.pipeline import get_report
from emend.research.http import EvidenceClient, ProviderError
from emend.research.isbn import choose_isbn
from emend.research.models import Edition, Proposal, ResearchPlan
from emend.research.providers import PROVIDERS
from emend.storage import Store, now


def normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def identity_matches(edition: Edition, metadata: dict[str, list[str]]) -> bool:
    titles, creators = metadata.get("title", []), metadata.get("creator", [])
    if len(titles) != 1 or normalized(titles[0]) != normalized(edition.title):
        return False
    if (
        not creators
        or not edition.creators
        or [normalized(x) for x in creators] != [normalized(x) for x in edition.creators]
    ):
        return False
    languages = metadata.get("language", [])
    return not (
        languages
        and edition.language
        and [normalized(x) for x in languages] != [normalized(x) for x in edition.language]
    )


def reconcile(
    metadata: dict[str, list[str]], editions: list[Edition], errors: dict[str, str]
) -> list[Proposal]:
    proposals = []
    all_match = bool(editions) and all(identity_matches(edition, metadata) for edition in editions)
    for field in ["title", "publisher", "date", "language"]:
        before = metadata.get(field, [])
        groups: dict[str, list[tuple[Edition, str]]] = {}
        for edition in editions:
            values = [edition.title] if field == "title" else getattr(edition, field)
            if len(values) == 1 and values[0].strip():
                groups.setdefault(normalized(values[0]), []).append((edition, values[0]))
        for key, group in sorted(groups.items()):
            if [normalized(x) for x in before] == [key]:
                continue
            providers = {edition.provider for edition, _ in group}
            safe = (
                not any(before)
                and field in {"publisher", "date"}
                and len(providers) >= 2
                and len(groups) == 1
                and all(len(getattr(e, field)) == 1 for e in editions)
                and all_match
                and not errors
            )
            if safe:
                reason = "Missing field corroborated by both providers, with matching ISBN, title, and ordered authors. No supplied language conflicts."
            elif len(groups) > 1:
                reason = "Providers or edition records disagree; choose a value explicitly after reviewing the sources."
            elif not all_match:
                reason = "An exact-ISBN result has incomplete or conflicting title/author/language evidence; review edition identity."
            elif any(before):
                reason = (
                    "Existing metadata is preserved unless you explicitly approve a replacement."
                )
            else:
                reason = "Insufficient corroboration, a failed provider, or a field that always requires review."
            record_ids = sorted({edition.record_id for edition, _ in group})
            after = [group[0][1]]
            identifier = hashlib.sha256(
                json.dumps([field, before, after, record_ids], ensure_ascii=False).encode()
            ).hexdigest()[:16]
            proposals.append(
                Proposal(
                    id=identifier,
                    field=field,
                    before=before,
                    after=after,
                    decision="safe" if safe else "review",
                    reason=reason,
                    record_ids=record_ids,
                    evidence_urls=sorted(
                        {url for edition, _ in group for url in edition.evidence_urls}
                    ),
                )
            )
    return proposals


def research(
    book_id: str,
    config: Config,
    *,
    isbn: str | None = None,
    offline=False,
    refresh=False,
    client: EvidenceClient | None = None,
) -> ResearchPlan:
    store = Store(config.root)
    with store.locked():
        row = store.resolve(book_id)
        book_id = row["id"]
        report = get_report(store, book_id)
        original = config.root / "originals" / f"{book_id}.epub"
        if sha256_file(original) != book_id:
            raise ValueError("Stored original hash mismatch.")
        source = Path(row["artifact_path"]) if row["artifact_path"] else original
        expected = report.artifact_sha256 if row["artifact_path"] else book_id
        if sha256_file(source) != expected:
            raise ValueError("Research input hash mismatch; revalidate the book first.")
        inspection = inspect_epub(source, config.limits)
        selected = choose_isbn(inspection.metadata.get("identifier", []), isbn)
        unknown = set(config.metadata.sources) - PROVIDERS.keys()
        if unknown or not config.metadata.sources:
            raise ValueError("Metadata sources must include openlibrary and/or google_books.")
        client = client or EvidenceClient(
            config.root / "cache" / "metadata", config.metadata, offline=offline, refresh=refresh
        )
        editions, errors = [], {}
        for provider in dict.fromkeys(config.metadata.sources):
            try:
                editions.extend(PROVIDERS[provider](selected, client))
            except (ProviderError, OSError, TypeError, KeyError) as exc:
                errors[provider] = str(exc)
        notes = [
            "Cover URLs are edition-linked suggestions only. No cover is downloaded or replaced.",
            "No AI service was called; uncertain cases require explicit review.",
        ]
        if not editions:
            notes.append(
                "No exact-ISBN edition evidence was retrieved. No changes can be proposed."
            )
        plan = ResearchPlan(
            id=uuid.uuid4().hex,
            book_id=book_id,
            artifact_sha256=expected,
            engine_version=ENGINE_VERSION,
            policy_sha256=config.metadata.fingerprint(),
            created_at=now(),
            isbn=selected,
            metadata=inspection.metadata,
            editions=editions,
            documents=list(client.documents.values()),
            proposals=reconcile(inspection.metadata, editions, errors),
            provider_errors=errors,
            notes=notes,
        )
        directory = config.root / "research" / book_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{plan.id}.json"
        path.write_text(plan.model_dump_json(indent=2))
        store.save_plan(plan.id, book_id, path, sha256_file(path))
        store.event(
            book_id,
            "RESEARCHED",
            f"Plan {plan.id}: {len(editions)} edition records; {len(errors)} provider errors.",
        )
        return plan


def load_plan(store: Store, book_id: str, identifier: str | None = None) -> ResearchPlan:
    row = store.plan(book_id, identifier)
    path = Path(row["path"])
    if sha256_file(path) != row["sha256"]:
        raise ValueError(
            "Research plan changed after it was recorded. Run research again; edited plans cannot be applied."
        )
    plan = ResearchPlan.model_validate_json(path.read_text())
    if plan.book_id != book_id or plan.id != row["id"]:
        raise ValueError("Research plan identity mismatch.")
    return plan


def plan_text(plan: ResearchPlan) -> str:
    lines = [
        f"Research plan {plan.id}",
        f"Book {plan.book_id} · ISBN {plan.isbn}",
        f"{len(plan.editions)} exact-ISBN records · {len(plan.proposals)} proposals",
        "",
    ]
    for proposal in plan.proposals:
        lines.extend(
            [
                f"{proposal.id}  {proposal.field}  [{proposal.decision}]",
                f"  {proposal.before or '(missing)'} → {proposal.after}",
                f"  {proposal.reason}",
                *[f"  Evidence: {url}" for url in proposal.evidence_urls],
            ]
        )
    for provider, error in plan.provider_errors.items():
        lines.append(f"UNAVAILABLE {provider}: {error}")
    for edition in plan.editions:
        for cover in edition.covers:
            lines.append(f"Cover candidate (unverified): {cover.url} — {cover.source_url}")
    lines.extend(plan.notes)
    return "\n".join(lines)
