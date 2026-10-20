import hashlib
import json
import os
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from lxml import html

from emend import accessibility
from emend.accessibility import read_ace_report
from emend.library import library_snapshot
from emend.models import Gate
from emend.pipeline import ingest
from emend.reports import report_text
from emend.storage import Store
from emend.validation import validate_ace
from tests.epub_factory import book_files, write_epub


def assertion(outcome="fail", **fields):
    return {"earl:result": {"earl:outcome": outcome}, **fields}


def ace_report():
    # Ace 1.4.6's published ReportBuilder/AssertionBuilder schema.
    leaf = assertion()
    leaf["earl:test"] = {"dct:title": "image-alt", "earl:impact": "critical"}
    leaf["earl:result"].update(
        {"dct:description": "Image needs alternative text.", "earl:pointer": {"css": ["img"]}}
    )
    document = assertion(assertions=[leaf], **{"earl:testSubject": {"url": "chapter.xhtml"}})
    return assertion(
        assertions=[document],
        **{"earl:assertedBy": {"doap:release": {"doap:revision": "1.4.6"}}},
    )


def parse(tmp_path, report):
    path = tmp_path / "ace.json"
    path.write_text(json.dumps(report))
    return read_ace_report(path)


def fake_ace(config, tmp_path, contents, exit_code=0):
    tool = tmp_path / "fake-ace"
    tool.write_text(f"""#!{sys.executable}
import sys,json
from pathlib import Path
out=Path(sys.argv[sys.argv.index('--outdir')+1])
out.mkdir()
(out/'report.json').write_text(json.dumps({contents!r}))
sys.exit({exit_code})
""")
    tool.chmod(0o755)
    config.tools.ace = tool


def test_leaf_findings_have_provenance_without_counting_ancestors(tmp_path):
    result = parse(tmp_path, ace_report())
    assert result.outcome == "failed" and result.failed == 1 and result.incomplete == 0
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.rule == "image-alt" and finding.document == "chapter.xhtml"
    assert finding.selectors == ["img"] and finding.impact == "critical"
    assert result.tool_version == "1.4.6"
    assert result.report_sha256 == hashlib.sha256((tmp_path / "ace.json").read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "outcome", ["pass", "passed", "earl:passed", "inapplicable", "earl:inapplicable"]
)
def test_passing_document_with_no_violations_is_not_an_empty_audit(tmp_path, outcome):
    result = parse(tmp_path, assertion("pass", assertions=[assertion(outcome, assertions=[])]))
    assert result.outcome == "passed" and result.findings == []


@pytest.mark.parametrize("outcome", ["cantTell", "earl:cantTell", "untested", "unknown", ""])
def test_unresolved_evidence_is_never_a_pass(tmp_path, outcome):
    result = parse(tmp_path, assertion("pass", assertions=[assertion(outcome)]))
    assert result.outcome == "incomplete" and result.incomplete == 1


@pytest.mark.parametrize("outcome", ["fail", "failed", "earl:failed"])
def test_failure_under_passing_summary_is_not_hidden(tmp_path, outcome):
    result = parse(tmp_path, assertion("pass", assertions=[assertion(outcome)]))
    assert result.outcome == "failed" and result.failed == 1


def test_failed_summary_with_passing_children_is_retained(tmp_path):
    result = parse(tmp_path, assertion(assertions=[assertion("pass")]))
    assert result.failed == 1 and result.findings[0].rule == "audit-outcome"


@pytest.mark.parametrize(
    "report",
    [
        [],
        {},
        {"assertions": []},
        {"assertions": [None]},
        {"assertions": [{}]},
        {"assertions": [assertion([])]},
        {"assertions": [assertion("pass", assertions={})]},
        {"assertions": [assertion("pass", **{"earl:testSubject": []})]},
    ],
)
def test_malformed_reports_do_not_pass(tmp_path, report):
    with pytest.raises(ValueError):
        parse(tmp_path, report)


def test_unrelated_outcome_in_book_metadata_does_not_change_audit(tmp_path):
    report = assertion("pass", assertions=[assertion("pass")], data={"earl:outcome": "fail"})
    assert parse(tmp_path, report).outcome == "passed"


def test_bounded_findings_keep_complete_counts(tmp_path, monkeypatch):
    report = ace_report()
    leaf = report["assertions"][0]["assertions"][0]
    report["assertions"][0]["assertions"] = [deepcopy(leaf) for _ in range(4)]
    monkeypatch.setattr(accessibility, "MAX_FINDINGS", 2)
    result = parse(tmp_path, report)
    assert result.failed == 4 and len(result.findings) == 2 and result.truncated


def test_long_details_and_selectors_are_explicitly_shortened(tmp_path):
    report = ace_report()
    leaf = report["assertions"][0]["assertions"][0]
    leaf["earl:result"]["dct:description"] = "x" * 3000
    leaf["earl:result"]["earl:pointer"]["css"] = ["img"] * 21
    result = parse(tmp_path, report)
    assert result.truncated and result.findings[0].message.endswith("[truncated]")
    assert len(result.findings[0].selectors) == 20


def test_report_and_assertion_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(accessibility, "MAX_REPORT_BYTES", 10)
    with pytest.raises(ValueError, match="evidence limit"):
        parse(tmp_path, ace_report())
    monkeypatch.setattr(accessibility, "MAX_REPORT_BYTES", 32 * 1024**2)
    monkeypatch.setattr(accessibility, "MAX_ASSERTIONS", 2)
    with pytest.raises(ValueError, match="processing limit"):
        parse(tmp_path, ace_report())
    monkeypatch.setattr(accessibility, "MAX_ASSERTIONS", 100_000)
    node = assertion("pass")
    for _ in range(35):
        node = assertion("pass", assertions=[node])
    with pytest.raises(ValueError, match="processing limit"):
        parse(tmp_path, node)


def test_nonzero_exit_cannot_pass_even_with_passing_json(epub, config, tmp_path):
    fake_ace(config, tmp_path, assertion("pass", assertions=[assertion("pass")]), 1)
    gate = validate_ace(epub, tmp_path, config)
    assert gate.status == "failed" and gate.accessibility.outcome == "passed"
    assert "Exit 1" in gate.detail


def test_stale_output_cannot_be_reused(epub, config, tmp_path):
    fake_ace(config, tmp_path, assertion("pass", assertions=[assertion("pass")]))
    assert validate_ace(epub, tmp_path, config).status == "passed"
    gate = validate_ace(epub, tmp_path, config)
    assert gate.status == "failed" and "already exists" in gate.detail


@pytest.mark.parametrize("required", [True, False])
def test_findings_in_reports_and_library_preserve_gate_policy(epub, config, tmp_path, required):
    data = ace_report()
    leaf = data["assertions"][0]["assertions"][0]
    leaf["earl:result"]["dct:description"] = '<script>alert("bad")</script>'
    fake_ace(config, tmp_path, data)
    config.quality.require_accessibility_audit = required
    original = epub.read_bytes()
    report = ingest(epub, config)
    assert report.state == ("FAILED" if required else "VALIDATED")
    assert epub.read_bytes() == original
    assert (config.root / "originals" / f"{report.book_id}.epub").read_bytes() == original
    text = report_text(report)
    assert "image-alt" in text and "chapter.xhtml" in text and "critical" in text
    path = Path(Store(config.root).resolve(report.book_id)["report_path"])
    saved = json.loads(path.read_text())
    audit = next(g["accessibility"] for g in saved["gates"] if g["name"] == "accessibility")
    assert audit["failed"] == 1 and audit["tool_version"] == "1.4.6"
    page = html.fromstring(path.with_suffix(".html").read_text())
    assert not page.xpath("//script")
    assert '<script>alert("bad")</script>' in page.text_content()
    assert "Manual review still needed" in page.text_content()
    assert any(
        "Accessibility audit needs attention" in s for s in library_snapshot(config).books[0].issues
    )


def test_older_gate_remains_readable():
    gate = Gate.model_validate(
        {"name": "accessibility", "status": "passed", "required": True, "detail": "Old summary"}
    )
    assert gate.accessibility is None


def accessible_book():
    files = book_files()
    metadata = b"""<meta property="schema:accessMode">textual</meta>
<meta property="schema:accessMode">visual</meta>
<meta property="schema:accessModeSufficient">textual</meta>
<meta property="schema:accessibilityFeature">alternativeText</meta>
<meta property="schema:accessibilityFeature">structuralNavigation</meta>
<meta property="schema:accessibilityHazard">none</meta>
<meta property="schema:accessibilitySummary">Original test story with headings, navigation and described illustrations.</meta>"""
    files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
        b"</metadata>", metadata + b"</metadata>"
    )
    files["EPUB/nav.xhtml"] = files["EPUB/nav.xhtml"].replace(
        b'<nav epub:type="toc"', b'<nav role="doc-toc" epub:type="toc"'
    )
    for name, data in files.items():
        if name.endswith(".xhtml"):
            files[name] = (
                data.replace(b'xml:lang="en"', b'xml:lang="en" lang="en"')
                .replace(b"<body>", b"<body><main>")
                .replace(b"</body>", b"</main></body>")
            )
    return files


def test_official_ace_accepts_clean_automated_audit(tmp_path, config):
    tool = os.environ.get("EMEND_TEST_ACE")
    if not tool:
        pytest.skip("Set EMEND_TEST_ACE to run the official DAISY Ace CLI integration.")
    config.tools.ace = Path(tool)
    config.quality.require_accessibility_audit = True
    if jar := os.environ.get("EMEND_TEST_EPUBCHECK_JAR"):
        config.tools.epubcheck_jar = Path(jar)
    source = write_epub(tmp_path / "accessible.epub", accessible_book())
    report = ingest(source, config)
    failures = []
    for gate in report.gates:
        if gate.required and gate.status != "passed":
            detail = f"{gate.name}: {gate.detail}"
            if gate.evidence and (log := Path(gate.evidence)).suffix == ".log":
                detail += "\n" + log.read_text(errors="replace")[-2500:]
            failures.append(detail)
    assert report.state == "VALIDATED", "\n".join(failures)
    gate = next(g for g in report.gates if g.name == "accessibility")
    assert gate.accessibility.outcome == "passed" and gate.accessibility.findings == []
    assert (config.root / "curated" / f"{report.book_id}.epub").read_bytes() == source.read_bytes()


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_official_ace_detects_and_locates_missing_alt(tmp_path, config, version):
    tool = os.environ.get("EMEND_TEST_ACE")
    if not tool:
        pytest.skip("Set EMEND_TEST_ACE to run the official DAISY Ace CLI integration.")
    config.tools.ace = Path(tool)
    config.quality.require_accessibility_audit = True
    files = book_files(version)
    files["EPUB/chapter.xhtml"] = files["EPUB/chapter.xhtml"].replace(
        b' alt="A simple green illustration of the observatory"', b""
    )
    source = write_epub(tmp_path / "missing-alt.epub", files)
    original = source.read_bytes()
    report = ingest(source, config)
    gate = next(g for g in report.gates if g.name == "accessibility")
    assert gate.accessibility is not None, gate.detail
    assert gate.status == "failed" and report.state == "FAILED"
    assert gate.accessibility.tool_version == "1.4.6"
    findings = [f for f in gate.accessibility.findings if f.rule == "image-alt"]
    assert len(findings) == 1
    assert findings[0].document.endswith("chapter.xhtml") and findings[0].selectors
    assert source.read_bytes() == original
    assert gate.accessibility.failed == len(gate.accessibility.findings)
    fixed = ingest(write_epub(tmp_path / "has-alt.epub", version=version), config)
    fixed_audit = next(g.accessibility for g in fixed.gates if g.name == "accessibility")
    assert fixed_audit is not None
    assert not any(f.rule == "image-alt" for f in fixed_audit.findings)
    assert fixed_audit.failed < gate.accessibility.failed
