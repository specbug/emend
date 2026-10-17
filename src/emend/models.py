from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["error", "warning", "info"]


class Finding(BaseModel):
    code: str
    severity: Severity
    message: str
    location: str | None = None


class Inspection(BaseModel):
    source_sha256: str
    metadata: dict[str, list[str]] = Field(default_factory=dict)
    epub_version: str | None = None
    package_path: str | None = None
    spine: list[str] = Field(default_factory=list)
    resource_hashes: dict[str, str] = Field(default_factory=dict)
    text_sha256: str | None = None
    spine_text_sha256: str | None = None
    text_characters: int = 0
    findings: list[Finding] = Field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return any(item.severity == "error" for item in self.findings)


class Gate(BaseModel):
    name: str
    status: Literal["passed", "failed", "unavailable", "skipped"]
    required: bool
    detail: str
    evidence: str | None = None


class MetadataChange(BaseModel):
    field: str
    before: list[str]
    after: list[str]
    evidence_urls: list[str]


class Report(BaseModel):
    book_id: str
    source_name: str
    created_at: str
    engine_version: str
    policy_sha256: str
    state: Literal["FAILED", "REVIEW_REQUIRED", "VALIDATED"]
    inspection: Inspection
    gates: list[Gate]
    artifact_sha256: str | None = None
    changes: list[str] = Field(default_factory=list)
    metadata_changes: list[MetadataChange] = Field(default_factory=list)
    research_plan_id: str | None = None
    research_plan_sha256: str | None = None
    applied_proposals: list[str] = Field(default_factory=list)
    parent_artifact_sha256: str | None = None
    parent_report_path: str | None = None
