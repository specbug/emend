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


class AccessibilityFinding(BaseModel):
    rule: str
    outcome: Literal["failed", "incomplete"]
    impact: str | None = None
    message: str
    document: str | None = None
    selectors: list[str] = Field(default_factory=list)


class AccessibilityAudit(BaseModel):
    tool_version: str | None = None
    report_sha256: str
    outcome: Literal["passed", "failed", "incomplete"]
    failed: int = 0
    incomplete: int = 0
    findings: list[AccessibilityFinding] = Field(default_factory=list)
    truncated: bool = False


class Gate(BaseModel):
    name: str
    status: Literal["passed", "failed", "unavailable", "skipped"]
    required: bool
    detail: str
    evidence: str | None = None
    accessibility: AccessibilityAudit | None = None


class MetadataChange(BaseModel):
    field: str
    before: list[str]
    after: list[str]
    evidence_urls: list[str]


class CoverChange(BaseModel):
    review_id: str
    candidate_id: str
    resource_path: str
    before_sha256: str
    after_sha256: str
    before_dimensions: list[int]
    after_dimensions: list[int]
    source: str
    source_url: str | None = None
    record_id: str | None = None
    allowed_smaller: bool = False
    allowed_aspect_change: bool = False


class RepairChange(BaseModel):
    plan_id: str
    action: str
    before_sha256: str
    after_sha256: str


class TypographyChange(BaseModel):
    trial_id: str
    css_sha256: str
    resource_path: str
    documents: list[str]
    all_spine: bool = False
    before_sha256: str
    after_sha256: str


class TextSizeChange(BaseModel):
    trial_id: str
    stylesheets: list[str]
    edits: int
    before_sha256: str
    after_sha256: str


class CoverAddition(BaseModel):
    review_id: str
    candidate_id: str
    image_path: str
    page_path: str
    image_sha256: str
    source: str
    source_url: str | None = None
    allowed_small: bool = False
    before_sha256: str
    after_sha256: str


class RecoveryChange(BaseModel):
    revision_sha256: str
    original: bool
    previous_report_path: str | None = None
    quarantine_path: str
    created_at: str


class Report(BaseModel):
    book_id: str
    source_name: str
    created_at: str
    engine_version: str
    policy_sha256: str
    state: Literal["FAILED", "REVIEW_REQUIRED", "VALIDATED"]
    inspection: Inspection
    gates: list[Gate]
    # Revalidation retains identity even when approval is revoked. Publication
    # always requires the state, policy, and required gates to pass as well.
    artifact_sha256: str | None = None
    revalidation: bool = False
    repair_changes: list[RepairChange] = Field(default_factory=list)
    typography_changes: list[TypographyChange] = Field(default_factory=list)
    text_size_changes: list[TextSizeChange] = Field(default_factory=list)
    cover_additions: list[CoverAddition] = Field(default_factory=list)
    recovery_changes: list[RecoveryChange] = Field(default_factory=list)
    changes: list[str] = Field(default_factory=list)
    metadata_changes: list[MetadataChange] = Field(default_factory=list)
    research_plan_id: str | None = None
    research_plan_sha256: str | None = None
    applied_proposals: list[str] = Field(default_factory=list)
    parent_artifact_sha256: str | None = None
    parent_report_path: str | None = None
    cover_changes: list[CoverChange] = Field(default_factory=list)
    cover_review_id: str | None = None
    approved_cover_id: str | None = None
