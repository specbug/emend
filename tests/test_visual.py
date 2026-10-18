import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from emend.cli import app
from emend.cover_apply import apply_cover
from emend.covers import cover_review
from emend.inspect import sha256_file
from emend.pipeline import get_report, ingest
from emend.storage import Store
from emend.visual import load_visual_review, resource_name, select_documents, visual_review
from tests.epub_factory import book_files, write_epub
from tests.test_covers import candidate, cover_book


def test_representative_selection_is_bounded_and_includes_ending():
    spine = [f"chapter-{i}.xhtml" for i in range(100)]
    assert select_documents(spine, ["nav.xhtml"], 5) == [
        spine[0],
        spine[50],
        spine[99],
        "nav.xhtml",
        spine[12],
    ]
    assert select_documents(["a", "b"], ["b"], 9) == ["a", "b"]
    with pytest.raises(ValueError):
        select_documents(spine, [], 10)
    with pytest.raises(ValueError):
        select_documents([], [], 5)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.invalid/a.xhtml",
        "file:///etc/passwd",
        "http://epub.emend.invalid/a",
        "https://epub.emend.invalid:443/a",
        "https://epub.emend.invalid/a?b=c",
        "https://epub.emend.invalid/%2e%2e/etc/passwd",
        "https://epub.emend.invalid/a%5cb",
        "https://epub.emend.invalid//etc/passwd",
        "https://user@epub.emend.invalid/a",
    ],
)
def test_resource_routes_reject_external_and_unsafe_paths(url):
    assert resource_name(url) is None


def test_resource_routes_preserve_encoded_filenames():
    assert resource_name("https://epub.emend.invalid/EPUB/a%20b.xhtml#note") == "EPUB/a b.xhtml"


def test_invalid_options_and_structural_errors_fail_before_browser(tmp_path, config):
    before = ingest(write_epub(tmp_path / "book.epub"), config)
    for profiles in [[], ["xteink"], ["compact", "compact"]]:
        with pytest.raises(ValueError, match="compact"):
            visual_review(before.book_id, config, profiles=profiles)
    files = book_files()
    files.pop("EPUB/observatory.png")
    broken = ingest(write_epub(tmp_path / "broken.epub", files), config)
    with pytest.raises(ValueError, match="structural"):
        visual_review(broken.book_id, config)


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_real_browser_samples_preserve_artifact_and_approval(tmp_path, browser_config, version):
    config = browser_config
    before = ingest(write_epub(tmp_path / "book.epub", version=version), config)
    state = Store(config.root).resolve(before.book_id)
    result = visual_review(before.book_id, config)
    assert result.status == "complete", result.model_dump()
    assert len(result.samples) == (6 if version == "3.0" else 4)
    assert all(s.screenshots and not s.error and not s.observations for s in result.samples)
    assert Store(config.root).resolve(before.book_id) == state
    assert get_report(Store(config.root), before.book_id) == before
    assert sha256_file(Path(state["artifact_path"])) == before.book_id
    assert load_visual_review(Store(config.root), before.book_id, result.id) == result
    directory = Path(
        Store(config.root).document(before.book_id, "visual-review", result.id)["path"]
    ).parent
    assert "data:image/png;base64," in (directory / "review.html").read_text()
    assert not list((config.root / ".staging").glob("visual-*"))


def test_real_browser_flags_overflow_small_text_and_samples_long_documents(
    tmp_path, browser_config
):
    files = book_files()
    files["EPUB/style.css"] += b" body{width:1500px} p{font-size:8px} body{min-height:3200px}"
    before = ingest(write_epub(tmp_path / "wide.epub", files), browser_config)
    review = visual_review(before.book_id, browser_config, profiles=["compact"], max_documents=1)
    sample = review.samples[0]
    assert review.status == "complete"
    assert any("Horizontal overflow" in n for n in sample.observations)
    assert any("below 10 CSS px" in n for n in sample.observations)
    assert len(sample.screenshots) == 3
    assert sample.screenshots[0].scroll_y == 0 < sample.screenshots[-1].scroll_y


def test_real_browser_offline_routing_blocks_external_resources(tmp_path, browser_config):
    # Exercise the rendering boundary directly: the normal inspector rejects such embeds earlier.
    from emend.visual import render

    files = book_files()
    files["EPUB/chapter.xhtml"] = files["EPUB/chapter.xhtml"].replace(
        b"</body>",
        b'<img src="https://example.invalid/tracker.png" alt="blocked"/><script>document.body.innerHTML="executed"</script></body>',
    )
    path = write_epub(tmp_path / "untrusted.epub", files)
    output = tmp_path / "screens"
    output.mkdir()
    _, samples = render(
        path,
        output,
        ["EPUB/chapter.xhtml"],
        {
            "EPUB/chapter.xhtml": "application/xhtml+xml",
            "EPUB/style.css": "text/css",
            "EPUB/observatory.png": "image/png",
        },
        ["compact"],
        browser_config,
    )
    assert not samples[0].error
    assert any("Images failed to load: 1" in n for n in samples[0].observations)
    assert samples[0].screenshots


def test_missing_browser_and_render_failure_do_not_register_success(tmp_path, browser_config):
    before = ingest(write_epub(tmp_path / "book.epub"), browser_config)
    browser_config.tools.chromium = tmp_path / "absent"
    with pytest.raises(ValueError, match="could not launch"):
        visual_review(before.book_id, browser_config)
    with pytest.raises(ValueError, match="No matching"):
        Store(browser_config.root).document(before.book_id, "visual-review")
    assert not list((browser_config.root / "visual" / before.book_id).iterdir())


def test_browser_budget_failure_is_recorded_as_incomplete(tmp_path, browser_config, monkeypatch):
    import emend.visual as visual

    before = ingest(write_epub(tmp_path / "book.epub"), browser_config)
    monkeypatch.setattr(visual, "MAX_SCREENSHOT_BYTES", 1)
    result = visual_review(before.book_id, browser_config, profiles=["compact"], max_documents=1)
    assert result.status == "incomplete" and result.samples[0].error
    assert not result.samples[0].screenshots
    assert get_report(Store(browser_config.root), before.book_id) == before


def test_saved_visual_tampering_and_staleness_are_visible(tmp_path, browser_config):
    config = browser_config
    before = cover_book(tmp_path, config)
    result = visual_review(before.book_id, config, profiles=["compact"], max_documents=1)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    runner = CliRunner()
    args = ["visuals", before.book_id, "--config", str(config_path), "--json"]
    current = runner.invoke(app, args)
    assert current.exit_code == 0 and json.loads(current.output)["current_artifact"]
    cover = cover_review(before.book_id, config, image=candidate(tmp_path))
    apply_cover(before.book_id, config, review_id=cover.id, approve=cover.candidates[0].id)
    stale = runner.invoke(app, args)
    assert stale.exit_code == 1 and not json.loads(stale.output)["current_artifact"]
    path = Path(Store(config.root).document(before.book_id, "visual-review", result.id)["path"])
    shot = path.parent / result.samples[0].screenshots[0].file
    original = shot.read_bytes()
    shot.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="screenshot hash"):
        load_visual_review(Store(config.root), before.book_id)
    shot.write_bytes(original)
    path.write_text(path.read_text().replace('"complete"', '"incomplete"'))
    with pytest.raises(ValueError, match="changed after registration"):
        load_visual_review(Store(config.root), before.book_id)


def test_visual_cli_capture_and_incomplete_exit_status(tmp_path, browser_config, monkeypatch):
    import emend.visual as visual

    report = ingest(write_epub(tmp_path / "cli.epub"), browser_config)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(browser_config.model_dump(mode="json")))
    args = [
        "visual-review",
        report.book_id[:12],
        "--config",
        str(path),
        "--profile",
        "compact",
        "--max-documents",
        "1",
    ]
    captured = CliRunner().invoke(app, args)
    assert captured.exit_code == 0 and "Sampling complete" in captured.output
    monkeypatch.setattr(visual, "MAX_SCREENSHOT_BYTES", 1)
    incomplete = CliRunner().invoke(app, args)
    assert incomplete.exit_code == 1 and "Sampling incomplete" in incomplete.output


def test_automatic_navigation_cannot_mislabel_a_screenshot(tmp_path, browser_config):
    from emend.visual import render

    files = book_files()
    files["EPUB/chapter.xhtml"] = files["EPUB/chapter.xhtml"].replace(
        b"</head>", b'<meta http-equiv="refresh" content="0;url=notes.xhtml"/></head>'
    )
    source = write_epub(tmp_path / "refresh.epub", files)
    directory = tmp_path / "shots"
    directory.mkdir()
    _, samples = render(
        source,
        directory,
        ["EPUB/chapter.xhtml"],
        {
            "EPUB/chapter.xhtml": "application/xhtml+xml",
            "EPUB/notes.xhtml": "application/xhtml+xml",
            "EPUB/style.css": "text/css",
            "EPUB/observatory.png": "image/png",
        },
        ["compact"],
        browser_config,
    )
    assert samples[0].error
