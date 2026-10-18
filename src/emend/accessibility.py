"""Read Ace's EARL evidence without treating aggregate outcomes as extra defects."""

import hashlib
import json
from pathlib import Path

from emend.models import AccessibilityAudit, AccessibilityFinding

MAX_REPORT_BYTES = 32 * 1024**2
MAX_ASSERTIONS = 100_000
MAX_FINDINGS = 500
PASS = {"pass", "passed", "earl:passed", "inapplicable", "earl:inapplicable"}
FAIL = {"fail", "failed", "earl:failed"}


def read_ace_report(path: Path) -> AccessibilityAudit:
    with path.open("rb") as source:
        data = source.read(MAX_REPORT_BYTES + 1)
    if len(data) > MAX_REPORT_BYTES:
        raise ValueError("Ace report exceeds the 32 MiB evidence limit")
    try:
        report = json.loads(data)
    except (RecursionError, UnicodeError) as exc:
        raise ValueError("Ace report is not usable JSON") from exc
    if not isinstance(report, dict) or not isinstance(report.get("assertions"), list):
        raise ValueError("Missing Ace assertions")
    if not report["assertions"]:
        raise ValueError("Ace report contains no audited assertions")
    audit = AccessibilityAudit(report_sha256=hashlib.sha256(data).hexdigest(), outcome="passed")
    visited = 0

    def bounded(value, limit=2000):
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("Invalid text in Ace assertion")
        if len(value) > limit:
            audit.truncated = True
            return value[:limit] + "… [truncated]"
        return value

    def walk(node, document=None, depth=0):
        nonlocal visited
        visited += 1
        if depth > 32 or visited > MAX_ASSERTIONS:
            raise ValueError("Ace assertion tree exceeds the processing limit")
        if not isinstance(node, dict):
            raise ValueError("Invalid Ace assertion")
        result = node.get("earl:result")
        if not isinstance(result, dict) or not isinstance(result.get("earl:outcome"), str):
            raise ValueError("Ace assertion is missing a usable outcome")
        outcome = result["earl:outcome"]
        status = "passed" if outcome in PASS else "failed" if outcome in FAIL else "incomplete"
        subject = node.get("earl:testSubject", {})
        if not isinstance(subject, dict):
            raise ValueError("Invalid Ace test subject")
        document = bounded(subject.get("url")) or document
        children = node.get("assertions", [])
        if not isinstance(children, list):
            raise ValueError("Invalid nested Ace assertions")
        child_statuses = [walk(child, document, depth + 1) for child in children]
        # Report/document summaries repeat child failures. Retain a contradictory
        # summary as evidence, but never count matching ancestors as new defects.
        if status != "passed" and status not in child_statuses:
            if status == "failed":
                audit.failed += 1
            else:
                audit.incomplete += 1
            test = node.get("earl:test", {})
            pointer = result.get("earl:pointer", {})
            if not isinstance(test, dict) or not isinstance(pointer, dict):
                raise ValueError("Invalid Ace test or pointer")
            selectors = pointer.get("css", [])
            if not isinstance(selectors, list) or any(not isinstance(s, str) for s in selectors):
                raise ValueError("Invalid Ace CSS selectors")
            if len(audit.findings) < MAX_FINDINGS:
                audit.truncated |= len(selectors) > 20
                audit.findings.append(
                    AccessibilityFinding(
                        rule=bounded(test.get("dct:title"), 200) or "audit-outcome",
                        outcome=status,
                        impact=bounded(test.get("earl:impact"), 100),
                        message=bounded(result.get("dct:description"))
                        or bounded(test.get("dct:description"))
                        or f"Ace reported {bounded(outcome, 100)}; inspect the raw evidence.",
                        document=document,
                        selectors=[bounded(s) for s in selectors[:20]],
                    )
                )
            else:
                audit.truncated = True
        return (
            "failed"
            if "failed" in [status, *child_statuses]
            else "incomplete"
            if "incomplete" in [status, *child_statuses]
            else "passed"
        )

    # Some older adapters omit the top-level result; child evidence is still mandatory.
    root = {**report, "earl:result": report.get("earl:result", {"earl:outcome": "pass"})}
    audit.outcome = walk(root)
    software = report.get("earl:assertedBy", {})
    if isinstance(software, dict):
        release = software.get("doap:release", {})
        if isinstance(release, dict):
            audit.tool_version = bounded(release.get("doap:revision"), 100)
    return audit
