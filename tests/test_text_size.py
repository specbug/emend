"""Resetting publisher body-text sizes: discovery, compared preview, gated application."""

import json
import os
import zipfile
from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

from emend.cli import app
from emend.inspect import inspect_epub, sha256_file
from emend.pipeline import get_report, ingest
from emend.storage import Store
from emend.text_size import (
    TextSizeRejectedError,
    apply_text_size,
    discover,
    effective_scale,
    load_text_size_trial,
    sheet_preserved,
    text_size_trial,
)
from emend.visual import PROFILES, Sample, Shot
from tests.epub_factory import book_files, write_epub

SHRUNK = (
    b"/* publisher styles */\n"
    b"body { margin: 5%; font-size: 0.85em; }\n"
    b"p { line-height: 1.5; font-size: 90% !important }\n"
    b".note p, p.small { font-size: .8em }\n"
    b"@media print { p { font-size: 9pt } }\n"
    b"h1 { font-size: 2em; font-weight: normal }\n"
    b"img { max-width: 100%; height: auto; }\n"
)


@pytest.fixture
def fake_size_capture(monkeypatch):
    calls = []

    def capture(source, directory, selected, media, profiles, config, **options):
        calls.append((Path(source).name, options))
        samples = []
        for profile in profiles:
            width, height = PROFILES[profile]
            for i, name in enumerate(selected):
                image = directory / f"{profile}-{i}.png"
                Image.new("RGB", (16, 16), "#667788").save(image)
                samples.append(
                    Sample(
                        document=name,
                        profile=profile,
                        width=width,
                        height=height,
                        screenshots=[Shot(file=image.name, sha256=sha256_file(image), scroll_y=0)],
                    )
                )
        return "fixture-browser", samples

    monkeypatch.setattr("emend.text_size.render", capture)
    return calls


def files_with(css, version="3.0"):
    files = book_files(version)
    files["EPUB/style.css"] = css
    return files


def book(tmp_path, config, css=SHRUNK, version="3.0"):
    return ingest(write_epub(tmp_path / "book.epub", files_with(css, version)), config)


def findings(tmp_path, css, version="3.0", config=None):
    from emend.config import Config

    source = write_epub(tmp_path / "probe.epub", files_with(css, version))
    limits = (config or Config(root=tmp_path / "probe")).limits
    return discover(source, inspect_epub(source, limits))


def test_discovery_targets_only_plain_body_text_rules(tmp_path):
    edits, notes = findings(tmp_path, SHRUNK)
    assert [(e.selector, e.before, e.after, e.unit) for e in edits] == [
        ("body", "0.85em", "1em", "em"),
        ("p", "90%", "100%", "%"),
    ]
    assert effective_scale(edits) == pytest.approx(0.765)
    assert any("1 size rule for classes" in n and "stays" in n for n in notes)
    assert any("@media" in n for n in notes)


@pytest.mark.parametrize(
    "css, expected",
    [
        (b"body { font-size: 1em } p { font-size: 100% }", []),
        (b"p { font-size: small }", [("p", "small", "1em")]),
        (
            b"html { font-size: 62.5% } body { font-size: 1.6rem }",
            [("html", "62.5%", "100%"), ("body", "1.6rem", "1rem")],
        ),
        (b"body, p { font-size: .9em }", [("body, p", ".9em", "1em")]),
        (b"body { font-size: 12px } p { font-size: 11pt }", []),
        (b"div p { font-size: .8em } p.text { font-size: .8em }", []),
        (b"h1 { font-size: .5em }", []),
    ],
)
def test_discovery_cases(tmp_path, css, expected):
    edits, _ = findings(tmp_path, css)
    assert [(e.selector, e.before, e.after) for e in edits] == expected


@pytest.mark.parametrize(
    "css",
    [
        b"html { font-size: 62.5% } body { font-size: 1.6em } .text { font-size: 1.6rem }",
        b"html { font-size: 10px } body { font-size: 1.6rem }",
    ],
)
def test_root_size_chains_are_left_alone(tmp_path, css):
    edits, notes = findings(tmp_path, css)
    assert edits == [] and any("root size" in n for n in notes)


def test_non_utf8_stylesheets_are_not_offered(tmp_path):
    edits, notes = findings(tmp_path, "body { font-size: .8em } /* café */".encode("latin-1"))
    assert edits == [] and any("not UTF-8" in n for n in notes)


def test_sheet_check_rejects_any_unplanned_byte(tmp_path):
    edits, _ = findings(tmp_path, SHRUNK)
    planned = SHRUNK.decode()
    for edit in sorted(edits, key=lambda e: e.start, reverse=True):
        planned = planned[: edit.start] + edit.after + planned[edit.end :]
    assert sheet_preserved(SHRUNK, planned.encode(), edits)
    for tamper in (
        planned.replace("margin: 5%", "margin: 6%"),
        planned.replace("/* publisher styles */", "/* changed */"),
        planned.replace("h1 { font-size: 2em", "h1 { font-size: 1em"),
        planned + " ",
        planned.replace("100% !important", "100%"),
    ):
        assert not sheet_preserved(SHRUNK, tamper.encode(), edits)
    assert not sheet_preserved(SHRUNK, SHRUNK, edits)


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_compared_reset_changes_only_the_planned_values(
    tmp_path, config, fake_size_capture, version
):
    before = book(tmp_path, config, version=version)
    trial = text_size_trial(before.book_id, config, layout="scroll")
    assert trial.status == "complete" and len(trial.edits) == 2
    # One verified snapshot renders both sides; the comparison changes nothing.
    assert [name for name, _ in fake_size_capture] == ["current.epub", "candidate.epub"]
    assert get_report(Store(config.root), before.book_id) == before
    assert load_text_size_trial(Store(config.root), before.book_id, trial.id) == trial
    after = apply_text_size(before.book_id, config, trial_id=trial.id, approve=True)
    assert after.state == "VALIDATED" and after.text_size_changes[-1].edits == 2
    assert after.inspection.text_sha256 == before.inspection.text_sha256
    assert after.inspection.spine == before.inspection.spine
    assert after.inspection.metadata == before.inspection.metadata
    artifact = config.root / "curated" / f"{before.book_id}.epub"
    original = config.root / "originals" / f"{before.book_id}.epub"
    assert sha256_file(original) == before.book_id
    with zipfile.ZipFile(original) as old, zipfile.ZipFile(artifact) as new:
        assert old.namelist() == new.namelist()
        changed = {"EPUB/style.css", *(["EPUB/package.opf"] if version == "3.0" else [])}
        for name in old.namelist():
            if name not in changed:
                assert old.read(name) == new.read(name), name
        css = new.read("EPUB/style.css")
    assert css == SHRUNK.replace(b"0.85em;", b"1em;").replace(b"90% !important", b"100% !important")
    assert any("body: 0.85em → 1em" in c for c in after.changes)
    # Applying the same comparison again is idempotent.
    assert apply_text_size(before.book_id, config, trial_id=trial.id, approve=True) == after


def test_application_requires_approval_current_comparison_and_validated_book(
    tmp_path, config, fake_size_capture
):
    before = book(tmp_path, config)
    trial = text_size_trial(before.book_id, config, layout="scroll")
    with pytest.raises(ValueError, match="--approve"):
        apply_text_size(before.book_id, config, trial_id=trial.id, approve=False)
    row = Store(config.root).resolve(before.book_id)
    Store(config.root).transition(
        before.book_id, row["source_name"], "FAILED", "Injected", row["report_path"], None
    )
    with pytest.raises(ValueError):
        apply_text_size(before.book_id, config, trial_id=trial.id, approve=True)
    assert get_report(Store(config.root), before.book_id) == before


def test_clean_book_has_nothing_to_compare(tmp_path, config):
    before = ingest(write_epub(tmp_path / "clean.epub"), config)
    with pytest.raises(ValueError, match="already follows"):
        text_size_trial(before.book_id, config, layout="scroll")


def test_unplanned_change_rejects_the_candidate(tmp_path, config, fake_size_capture, monkeypatch):
    import emend.text_size as module

    before = book(tmp_path, config)
    trial = text_size_trial(before.book_id, config, layout="scroll")
    real = module.rewrite

    def tampered(source, target, inspection, edits):
        real(source, target, inspection, edits)
        with zipfile.ZipFile(target) as archive:
            infos = archive.infolist()
            data = {i.filename: archive.read(i) for i in infos}
        data["EPUB/style.css"] = data["EPUB/style.css"].replace(b"margin: 5%", b"margin: 9%")
        with zipfile.ZipFile(target, "w") as archive:
            for info in infos:
                archive.writestr(info, data[info.filename])

    monkeypatch.setattr(module, "rewrite", tampered)
    with pytest.raises(TextSizeRejectedError):
        apply_text_size(before.book_id, config, trial_id=trial.id, approve=True)
    assert get_report(Store(config.root), before.book_id) == before
    assert sha256_file(config.root / "curated" / f"{before.book_id}.epub") == before.book_id
    assert list((config.root / "reports" / before.book_id).glob("*/failure.txt"))


def test_later_edits_carry_text_size_history(tmp_path, config, fake_size_capture):
    from emend.revalidation import revalidate

    before = book(tmp_path, config)
    trial = text_size_trial(before.book_id, config, layout="scroll")
    apply_text_size(before.book_id, config, trial_id=trial.id, approve=True)
    assert revalidate(before.book_id, config).text_size_changes


def test_cli_compares_and_applies(tmp_path, config, fake_size_capture):
    import yaml

    before = book(tmp_path, config)
    settings = tmp_path / "config.yaml"
    settings.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["text-size-trial", before.book_id, "--layout", "scroll", "--json", "-c", str(settings)],
    )
    assert result.exit_code == 0, result.output
    trial = json.loads(result.output)
    result = runner.invoke(
        app, ["apply-text-size", before.book_id, "--trial", trial["id"], "-c", str(settings)]
    )
    assert result.exit_code == 2 and get_report(Store(config.root), before.book_id) == before
    result = runner.invoke(
        app,
        [
            "apply-text-size",
            before.book_id,
            "--trial",
            trial["id"],
            "--approve",
            "-c",
            str(settings),
        ],
    )
    assert result.exit_code == 0, result.output
    assert get_report(Store(config.root), before.book_id).text_size_changes


def test_workspace_detects_compares_applies_and_restores(
    workspace, tmp_path, config, fake_size_capture, fake_pages
):
    from tests.test_review_ui import fields, login, post, request

    before = book(tmp_path, config)
    cookie = login(workspace)
    page = f"/books/{before.book_id}?step=refine"
    body = request(workspace, page, headers={"Cookie": cookie})[2]
    assert "sets body text to 76% of the size you choose on your reader" in body
    assert "Compare at my reader’s size" in body and "ignores fonts embedded in a book" in body
    assert "book’s line height is ignored" in body
    assert get_report(Store(config.root), before.book_id) == before
    assert (
        post(workspace, cookie, fields(workspace, before, "text-size-trial", layout="scroll"))[0]
        == 303
    )
    body = request(workspace, page, headers={"Cookie": cookie})[2]
    assert "Use my reader’s size" in body and "/pages/text-size-trial/" in body
    assert "As published" in body and "Not applied yet" in body
    trial = Store(config.root).document(before.book_id, "text-size-trial")["id"]
    status, _, _ = post(
        workspace, cookie, fields(workspace, before, "apply-text-size", trial=trial)
    )
    assert status == 303
    adjusted = get_report(Store(config.root), before.book_id)
    assert adjusted.text_size_changes and adjusted.state == "VALIDATED"
    body = request(workspace, page, headers={"Cookie": cookie})[2]
    assert "Applied to the book" in body and "Return to the baseline" in body
    assert (
        "Body text now follows the size you choose" in body and "As published, it was 76%" in body
    )
    row = Store(config.root).resolve(before.book_id)
    status, _, _ = post(
        workspace,
        cookie,
        fields(
            workspace,
            adjusted,
            "restore-baseline",
            revision=before.artifact_sha256,
            expected_report=row["report_path"],
        ),
    )
    assert status == 303
    assert get_report(Store(config.root), before.book_id).artifact_sha256 == before.book_id


def test_workspace_says_when_body_text_already_follows_the_reader(workspace, tmp_path, config):
    from tests.test_review_ui import login, request

    before = ingest(write_epub(tmp_path / "clean.epub"), config)
    body = request(
        workspace, f"/books/{before.book_id}?step=refine", headers={"Cookie": login(workspace)}
    )[2]
    assert "Body text follows the size you choose on your reader." in body
    assert "Compare at my reader’s size" not in body


def test_official_epubcheck_accepts_reset_sizes(tmp_path, config, fake_size_capture):
    jar = os.environ.get("EMEND_TEST_EPUBCHECK_JAR")
    if not jar:
        pytest.skip("Set EMEND_TEST_EPUBCHECK_JAR for official validation")
    config.tools.epubcheck_jar = Path(jar)
    before = book(tmp_path, config)
    assert before.state == "VALIDATED"
    trial = text_size_trial(before.book_id, config, layout="scroll")
    after = apply_text_size(before.book_id, config, trial_id=trial.id, approve=True)
    assert after.state == "VALIDATED"
    assert next(g for g in after.gates if g.name == "epubcheck").status == "passed"
