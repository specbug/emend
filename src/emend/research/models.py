from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MetadataField = Literal["title", "publisher", "date", "language"]


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    fetched_at: str
    sha256: str
    payload: dict[str, Any]


class CoverCandidate(BaseModel):
    url: str
    source_url: str
    note: str = "Edition-linked candidate; image quality and artwork have not been verified."


class Edition(BaseModel):
    provider: Literal["openlibrary", "google_books"]
    record_id: str
    source_url: str
    isbn: str
    title: str
    creators: list[str] = Field(default_factory=list)
    publisher: list[str] = Field(default_factory=list)
    date: list[str] = Field(default_factory=list)
    language: list[str] = Field(default_factory=list)
    covers: list[CoverCandidate] = Field(default_factory=list)
    evidence_urls: list[str] = Field(default_factory=list)


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    field: MetadataField
    before: list[str]
    after: list[str] = Field(min_length=1, max_length=1)
    decision: Literal["safe", "review"]
    reason: str
    record_ids: list[str]
    evidence_urls: list[str]


class ResearchPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    book_id: str
    artifact_sha256: str
    engine_version: str
    policy_sha256: str
    created_at: str
    isbn: str
    metadata: dict[str, list[str]]
    editions: list[Edition]
    documents: list[Evidence]
    proposals: list[Proposal]
    provider_errors: dict[str, str]
    notes: list[str] = Field(default_factory=list)
