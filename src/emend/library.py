"""A local index of recorded quality state and existing review evidence."""

import hashlib
import html
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, Field

from emend import ENGINE_VERSION
from emend.config import Config
from emend.covers import CoverReview
from emend.models import Report
from emend.reports import report_styles
from emend.research.ai_review import SavedReview
from emend.research.discovery import Discovery
from emend.research.models import ResearchPlan
from emend.storage import Store, now
from emend.visual import VisualReview

MAX_RECORD_BYTES = 32 * 1024**2


class ReviewLink(BaseModel):
    label: str
    status: Literal["current", "earlier", "applied", "incomplete", "unavailable"]
    detail: str
    href: str | None = None


class LibraryBook(BaseModel):
    book_id: str
    title: str
    creators: list[str] = Field(default_factory=list)
    state: str
    updated_at: str
    artifact_sha256: str | None = None
    issues: list[str] = Field(default_factory=list)
    reviews: list[ReviewLink] = Field(default_factory=list)
    pending_proposals: int = 0
    next_step: str = ""


class LibrarySnapshot(BaseModel):
    created_at: str
    total_books: int
    offset: int = 0
    books: list[LibraryBook]


def local_file(root: Path, value: str | Path) -> Path:
    path = Path(value).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("Saved evidence is missing or outside the Emend data directory.")
    return path


def read_record(root: Path, row: dict, model, book_id: str):
    path = local_file(root, row["path"])
    with path.open("rb") as source:
        data = source.read(MAX_RECORD_BYTES + 1)
    if len(data) > MAX_RECORD_BYTES:
        raise ValueError("Saved evidence exceeds the overview size limit.")
    if row.get("sha256") and hashlib.sha256(data).hexdigest() != row["sha256"]:
        raise ValueError("Saved evidence changed after registration.")
    result = model.model_validate_json(data)
    if result.book_id != book_id or (row.get("id") and result.id != row["id"]):
        raise ValueError("Saved evidence identity mismatch.")
    return result


def link(root: Path, value: str | Path) -> str | None:
    try:
        path = local_file(root, value)
    except (ValueError, OSError):
        return None
    return quote(path.relative_to(root.resolve()).as_posix(), safe="/")


def freshness(record, digest: str | None, policy: str | None = None) -> str:
    if record.artifact_sha256 != digest:
        return "earlier"
    if getattr(record, "engine_version", ENGINE_VERSION) != ENGINE_VERSION:
        return "earlier"
    if policy and record.policy_sha256 != policy:
        return "earlier"
    return "current"


def summarize_book(row: dict, records: list[tuple[str, dict]], config: Config) -> LibraryBook:
    book = LibraryBook(
        book_id=row["id"],
        title=row["source_name"],
        state=row["state"],
        updated_at=row["updated_at"],
    )
    report = None
    try:
        if not row["report_path"]:
            raise ValueError("No quality report recorded; ingestion may have been interrupted.")
        report = read_record(config.root, {"path": row["report_path"]}, Report, row["id"])
        if report.state != row["state"]:
            raise ValueError("Recorded book status and quality report disagree.")
        book.title = "; ".join(report.inspection.metadata.get("title", [])) or book.title
        book.creators = report.inspection.metadata.get("creator", [])
        book.artifact_sha256 = report.artifact_sha256 if row["artifact_path"] else row["id"]
        quality_current = (
            report.policy_sha256 == config.policy_hash() and report.engine_version == ENGINE_VERSION
        )
        if not quality_current:
            book.issues.append("Quality policy or engine changed; revalidation is needed.")
        if row["state"] != "VALIDATED":
            book.issues.append("Required publication checks have not passed.")
        if row["artifact_path"]:
            local_file(config.root, row["artifact_path"])
        quality_href = link(config.root, Path(row["report_path"]).with_suffix(".html")) or link(
            config.root, row["report_path"]
        )
        if not quality_href:
            raise ValueError("Saved quality report is unavailable.")
        book.reviews.append(
            ReviewLink(
                label="Quality report",
                status="current" if quality_current else "earlier",
                detail="Recorded publication checks",
                href=quality_href,
            )
        )
    except (ValueError, OSError) as exc:
        book.issues.append(str(exc).split("\n", 1)[0])
        book.reviews.append(
            ReviewLink(
                label="Quality report",
                status="unavailable",
                detail="Inspect the stored report and source files before continuing.",
            )
        )

    models = {
        "research": ResearchPlan,
        "discovery": Discovery,
        "cover-review": CoverReview,
        "visual-review": VisualReview,
        "ai-review": SavedReview,
    }
    labels = {
        "research": "Metadata proposals",
        "discovery": "Edition candidates",
        "cover-review": "Cover comparison",
        "visual-review": "Visual samples",
        "ai-review": "Meta advice",
    }
    current_visual = False
    latest_plan = None
    latest_plan_digest = None
    for kind, record_row in records:
        label = labels[kind]
        try:
            record = read_record(config.root, record_row, models[kind], row["id"])
            policy = (
                config.metadata.fingerprint()
                if kind in {"research", "discovery"}
                else config.policy_hash()
                if kind == "cover-review"
                else None
            )
            status = freshness(record, book.artifact_sha256, policy)
            detail = (
                "For the recorded artifact"
                if status == "current"
                else "Artifact or policy has changed"
            )
            if kind == "research":
                latest_plan = record
                latest_plan_digest = record_row["sha256"]
                if report and report.research_plan_id == record.id:
                    status, detail = (
                        "applied",
                        "Selected edits were applied; research again for further changes",
                    )
                elif status == "current":
                    book.pending_proposals = len(record.proposals)
                    detail = f"{len(record.proposals)} proposals to inspect"
                    if record.provider_errors:
                        detail += f" · {len(record.provider_errors)} unavailable providers"
            elif kind == "discovery" and status == "current":
                detail = f"{len(record.candidates)} candidates; edition selection remains explicit"
            elif kind == "cover-review":
                if (
                    report
                    and report.cover_changes
                    and report.cover_changes[-1].review_id == record.id
                ):
                    status, detail = "applied", "Selected artwork is in the recorded cover history"
                elif status == "current":
                    detail = f"{len(record.candidates)} artwork candidates · {len(record.errors)} preparation errors"
            elif kind == "visual-review":
                current_visual = status == "current" and record.status == "complete"
                if status == "current" and record.status == "incomplete":
                    status = "incomplete"
                detail = f"{len(record.samples)} browser samples · {record.layout} · capture {record.status}"
            elif kind == "ai-review":
                if (
                    not latest_plan
                    or record.plan_id != latest_plan.id
                    or record.plan_sha256 != latest_plan_digest
                ):
                    raise ValueError("Saved advice no longer matches its research evidence.")
                if (
                    freshness(latest_plan, book.artifact_sha256, config.metadata.fingerprint())
                    != "current"
                ):
                    status = "earlier"
                detail = "Advisory only; inspect the linked plan and citations"
            path = Path(record_row["path"])
            href = (
                link(config.root, path.with_suffix(".html"))
                if kind in {"cover-review", "visual-review"}
                else link(config.root, path)
            )
            if href is None:
                book.issues.append(f"{label}: the saved report file is unavailable.")
                status, detail = (
                    "unavailable",
                    "The saved report file is missing or outside the data directory",
                )
            book.reviews.append(ReviewLink(label=label, status=status, detail=detail, href=href))
        except (ValueError, OSError) as exc:
            book.reviews.append(
                ReviewLink(label=label, status="unavailable", detail=str(exc).split("\n", 1)[0])
            )
            book.issues.append(f"{label}: saved evidence needs attention.")
    prefix = book.book_id[:12]
    if book.issues:
        book.next_step = f"emend report {prefix}"
    elif book.pending_proposals:
        book.next_step = f"emend proposals {prefix}"
    elif not current_visual:
        book.next_step = f"emend visual-review {prefix}"
    else:
        book.next_step = f"emend report {prefix}"
    return book


def library_snapshot(config: Config, limit: int = 200, offset: int = 0) -> LibrarySnapshot:
    if not 1 <= limit <= 1000:
        raise ValueError("Choose between 1 and 1000 books.")
    if offset < 0:
        raise ValueError("Offset must be nonnegative.")
    store = Store(config.root)
    with store.locked(), store.db() as db:
        total = db.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        rows = db.execute(
            "SELECT * FROM books ORDER BY updated_at DESC, id LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
        books = []
        for raw in rows:
            row = dict(raw)
            records = []
            plan = db.execute(
                "SELECT * FROM research_plans WHERE book_id=? ORDER BY created_at DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            if plan:
                records.append(("research", dict(plan)))
                advice = db.execute(
                    "SELECT * FROM ai_reviews WHERE book_id=? AND plan_id=? ORDER BY created_at DESC LIMIT 1",
                    (row["id"], plan["id"]),
                ).fetchone()
                if advice:
                    records.append(("ai-review", dict(advice)))
            for kind in ["discovery", "cover-review", "visual-review"]:
                document = db.execute(
                    "SELECT * FROM review_documents WHERE book_id=? AND kind=? ORDER BY created_at DESC LIMIT 1",
                    (row["id"], kind),
                ).fetchone()
                if document:
                    records.append((kind, dict(document)))
            books.append(summarize_book(row, records, config))
        return LibrarySnapshot(created_at=now(), total_books=total, offset=offset, books=books)


def atomic_text(path: Path, text: str):
    fd, name = tempfile.mkstemp(prefix=".overview-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(text)
        Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)


def write_library(snapshot: LibrarySnapshot, config: Config) -> Path:
    esc = html.escape
    cards = []
    for book in snapshot.books:
        reviews = []
        for review in book.reviews:
            label = (
                f'<a href="{esc(review.href)}">{esc(review.label)}</a>'
                if review.href
                else esc(review.label)
            )
            reviews.append(
                f'<li><div class="review-heading">{label}<span class="tag {review.status}">{esc(review.status.title())}</span></div><p class="review-detail">{esc(review.detail)}</p></li>'
            )
        issues = (
            f'<ul class="issues">{"".join(f"<li>{esc(i)}</li>" for i in book.issues)}</ul>'
            if book.issues
            else ""
        )
        cards.append(
            f"""<article><div class="book-heading"><h2>{esc(book.title)}</h2><span class="state">{esc(book.state.replace("_", " ").title())}</span></div><p>{esc("; ".join(book.creators) or "Author not recorded")}</p><code>{esc(book.book_id[:12])}</code>{issues}<ul class="reviews">{"".join(reviews)}</ul><p class="next">Continue review</p><code>{esc(book.next_step)}</code></article>"""
        )
    attention = sum(
        bool(
            b.issues
            or b.pending_proposals
            or any(r.status in {"incomplete", "unavailable"} for r in b.reviews)
        )
        for b in snapshot.books
    )
    if not cards:
        cards.append(
            "<section><h2>Your library starts here</h2><p>Import an EPUB to preserve the original and create its first quality report.</p><code>emend ingest /path/to/book.epub</code></section>"
            if not snapshot.total_books
            else "<section><h2>No books at this offset</h2><p>Start again with <code>emend library --offset 0</code>.</p></section>"
        )
    next_offset = snapshot.offset + len(snapshot.books)
    pagination = (
        f"<p>Continue to the next books: <code>emend library --offset {next_offset} --limit {len(snapshot.books)}</code></p>"
        if snapshot.books and next_offset < snapshot.total_books
        else ""
    )
    date = datetime.fromisoformat(snapshot.created_at).strftime("%d %b %Y, %H:%M UTC")
    shown = (
        f"Books {snapshot.offset + 1}–{next_offset} of {snapshot.total_books}"
        if snapshot.books
        else f"0 of {snapshot.total_books} books shown"
    )
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; font-src data:"><title>Emend library</title><style>{report_styles()}
.book-heading {{display:flex;align-items:baseline;justify-content:space-between;gap:1rem;flex-wrap:wrap}}
.book-heading h2 {{font-family:var(--serif);font-size:1.75rem;line-height:1.25;font-weight:normal;flex:1 1 20rem;margin:0;overflow-wrap:anywhere}}
.book-heading .state {{margin:0;white-space:nowrap}}
article {{border-bottom:1px solid var(--rule);padding:2.5rem 0}} article>p {{margin:.75rem 0}}
.reviews {{list-style:none;padding:0;display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,19rem),1fr));gap:1rem 2rem;margin-top:1.5rem}}
.reviews li {{padding:0;min-width:0}} .review-heading {{display:flex;justify-content:space-between;gap:1rem;align-items:baseline}}
.tag {{font-family:var(--mono);font-size:.7rem;color:var(--muted);white-space:nowrap}} .tag.unavailable,.tag.incomplete {{color:var(--signal)}}
li .review-detail {{font-size:.85rem;color:var(--muted);line-height:1.5;margin:.5rem 0 0}} .issues {{margin-top:1rem;color:var(--signal);font-size:.875rem}}
article .next {{margin:1.5rem 0 .25rem;font-size:.8rem;color:var(--muted)}} .summary {{font-family:var(--mono);font-size:.8rem;color:var(--muted)}}
</style></head><body><main><header><div class="brand">EMEND / LIBRARY</div><h1>Your library, in review.</h1><p>Find your books, inspect the evidence, and continue where you left off.</p><p class="summary">{shown} · {attention} with issues or proposals</p><p>Recorded state as of {esc(date)}. Run <code>emend library</code> to refresh.</p></header>{"".join(cards)}{pagination}<footer>Latest updated books are shown first. Current means the review references the recorded artifact and policy; it is not approval. Earlier evidence remains available for context. This overview does not revalidate EPUB/image bytes or publish anything. For another configuration, append <code>--config PATH</code> to commands.</footer></main></body></html>"""
    atomic_text(config.root / "library.json", snapshot.model_dump_json(indent=2))
    path = config.root / "library.html"
    atomic_text(path, page)
    return path
