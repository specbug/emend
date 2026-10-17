"""Keep AI advice separate from research evidence and transformation approval."""

import hashlib
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from emend import ENGINE_VERSION
from emend.config import Config
from emend.inspect import sha256_file
from emend.pipeline import get_report
from emend.research.meta import (
    ENDPOINT,
    PROMPT_VERSION,
    MetaError,
    ReviewAnswer,
    call_meta,
    encode_request,
    request_body,
    validate_answer,
)
from emend.research.models import ResearchPlan
from emend.research.service import load_plan
from emend.storage import Store, now


class SavedReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    book_id: str
    plan_id: str
    plan_sha256: str
    artifact_sha256: str
    created_at: str
    endpoint: str
    model: str
    prompt_version: str
    request_key: str
    request: dict[str, Any]
    response_sha256: str
    usage: dict[str, int]
    answer: ReviewAnswer


def cached_review(row: dict, plan: ResearchPlan, plan_digest: str) -> SavedReview:
    path = Path(row["path"])
    if sha256_file(path) != row["sha256"]:
        raise ValueError(
            "Saved AI review changed after registration. Run ai-review --refresh to replace it."
        )
    review = SavedReview.model_validate_json(path.read_text())
    key = hashlib.sha256(plan_digest.encode() + encode_request(review.request)).hexdigest()
    if (
        review.book_id != plan.book_id
        or review.plan_id != plan.id
        or review.plan_sha256 != plan_digest
        or review.artifact_sha256 != plan.artifact_sha256
        or key != review.request_key
        or key != row["request_key"]
    ):
        raise ValueError("Saved AI review identity mismatch.")
    validate_answer(review.answer, plan)
    return review


def show_review(book_id: str, config: Config, plan_id: str | None = None) -> SavedReview:
    store = Store(config.root)
    with store.locked():
        book_id = store.resolve(book_id)["id"]
        plan = load_plan(store, book_id, plan_id)
        row = store.ai_review(book_id, plan.id)
        if row is None:
            raise ValueError(
                "No saved AI review for this research plan. Run emend ai-review first."
            )
        return cached_review(row, plan, store.plan(book_id, plan.id)["sha256"])


def review_plan(
    book_id: str, config: Config, *, plan_id: str | None = None, refresh: bool = False
) -> SavedReview:
    store = Store(config.root)
    with store.locked():
        row = store.resolve(book_id)
        book_id = row["id"]
        plan = load_plan(store, book_id, plan_id)
        if (
            plan.engine_version != ENGINE_VERSION
            or plan.policy_sha256 != config.metadata.fingerprint()
        ):
            raise ValueError(
                "Research policy or engine changed; run research again before AI review."
            )
        report = get_report(store, book_id)
        original = config.root / "originals" / f"{book_id}.epub"
        if sha256_file(original) != book_id:
            raise ValueError("Stored original hash mismatch.")
        source = Path(row["artifact_path"]) if row["artifact_path"] else original
        expected = report.artifact_sha256 if row["artifact_path"] else book_id
        if expected != plan.artifact_sha256 or sha256_file(source) != expected:
            raise ValueError(
                "Research plan is stale or artifact changed; research the current book again."
            )
        plan_digest = store.plan(book_id, plan.id)["sha256"]
        body = request_body(plan, config.ai_review)
        request_key = hashlib.sha256(plan_digest.encode() + encode_request(body)).hexdigest()
        cached = store.ai_review(book_id, plan.id, request_key)
        if cached and not refresh:
            return cached_review(cached, plan, plan_digest)
        try:
            answer, usage, response_digest = call_meta(body, plan, config.ai_review)
        except MetaError as exc:
            store.event(book_id, "AI_REVIEW_FAILED", str(exc))
            raise
        # API advice cannot change the underlying plan, book state, or quality gates.
        if sha256_file(source) != expected or sha256_file(original) != book_id:
            raise ValueError("Book changed during AI review; advice discarded.")
        load_plan(store, book_id, plan.id)
        review = SavedReview(
            id=uuid.uuid4().hex,
            book_id=book_id,
            plan_id=plan.id,
            plan_sha256=plan_digest,
            artifact_sha256=plan.artifact_sha256,
            created_at=now(),
            endpoint=ENDPOINT,
            model=config.ai_review.model,
            prompt_version=PROMPT_VERSION,
            request_key=request_key,
            request=body,
            response_sha256=response_digest,
            usage=usage,
            answer=answer,
        )
        directory = config.root / "research" / book_id / "ai-reviews"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{review.id}.json"
        with path.open("x") as out:
            out.write(review.model_dump_json(indent=2))
        store.save_ai_review(book_id, plan.id, request_key, path, sha256_file(path))
        store.event(
            book_id,
            "AI_REVIEWED",
            f"Meta review {review.id} for plan {plan.id}; advice only, no approvals changed.",
        )
        return review


def review_text(review: SavedReview) -> str:
    lines = [
        f"Meta review {review.id} · {review.model}",
        f"Research plan {review.plan_id}",
        f"Artifact {review.artifact_sha256}",
        "Advice only. No metadata changed or approved.",
        "",
        review.answer.summary,
        "",
    ]
    for item in review.answer.recommendations:
        lines += [
            f"{item.proposal_id}  [{item.verdict}]",
            f"  {item.rationale}",
            f"  Evidence records: {', '.join(item.record_ids) or '(none)'}",
        ]
    lines += [
        "",
        "Compare these recommendations with emend proposals and its source evidence.",
        f"To select a proposal explicitly: emend apply {review.book_id[:12]} --plan {review.plan_id} --approve PROPOSAL_ID",
        "Saved advice describes this exact revision; applying a change requires current evidence and quality gates.",
    ]
    return "\n".join(lines)
