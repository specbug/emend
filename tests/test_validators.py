import json
import os
import sys
from pathlib import Path

import pytest

from emend.pipeline import ingest
from emend.validation import validate_ace
from tests.epub_factory import book_files, write_epub


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [("earl:passed", "passed"), ("earl:failed", "failed"), ("earl:cantTell", "failed")],
)
def test_ace_outcomes_are_used_even_when_process_exits_zero(
    epub, config, tmp_path, outcome, expected
):
    tool = tmp_path / "ace"
    contents = {"assertions": [{"earl:result": {"earl:outcome": outcome}}]}
    tool.write_text(f"""#!{sys.executable}
import sys,json
from pathlib import Path
out=Path(sys.argv[sys.argv.index('--outdir')+1])
out.mkdir()
(out/'report.json').write_text(json.dumps({contents!r}))
""")
    tool.chmod(0o755)
    config.tools.ace = tool
    config.quality.require_accessibility_audit = True
    output = tmp_path / "audit"
    output.mkdir()
    gate = validate_ace(epub, output, config)
    assert gate.status == expected
    assert gate.required


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_official_epubcheck_end_to_end(tmp_path, config, version):
    jar = os.environ.get("EMEND_TEST_EPUBCHECK_JAR")
    if not jar:
        pytest.skip("Set EMEND_TEST_EPUBCHECK_JAR to run the official validator integration.")
    config.tools.epubcheck_jar = Path(jar)
    path = write_epub(tmp_path / "real-validator.epub", version=version)
    report = ingest(path, config)
    assert report.state == "VALIDATED", report.model_dump_json(indent=2)
    gate = next(g for g in report.gates if g.name == "epubcheck")
    assert gate.status == "passed"
    assert not json.loads(Path(gate.evidence).read_text())["messages"]
    # A standards defect beyond the built-in checks must still block publication.
    files = book_files(version)
    files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
        b'unique-identifier="book-id"', b'unique-identifier="book-id" invalid-attribute="true"'
    )
    bad = write_epub(tmp_path / "standards-defect.epub", files)
    rejected = ingest(bad, config)
    assert rejected.state == "FAILED"
    assert next(g.status for g in rejected.gates if g.name == "structure") == "passed"
    assert next(g.status for g in rejected.gates if g.name == "epubcheck") == "failed"
