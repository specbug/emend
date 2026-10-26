import json
from pathlib import Path

import pytest
import yaml
from PIL import Image
from typer.testing import CliRunner

from emend.cli import app
from emend.curation import apply_proposals
from emend.inspect import sha256_file
from emend.pipeline import get_report, ingest
from emend.storage import Store
from emend.typography import TypographySettings, load_trial, typography_trial
from emend.visual import PROFILES, Sample, Shot
from tests.epub_factory import book_files, write_epub
from tests.test_curation import setup_book


@pytest.fixture
def fake_capture(monkeypatch):
    calls = []

    def capture(source, directory, selected, media, profiles, config, **options):
        calls.append(options)
        samples = []
        for profile in profiles:
            width, height = PROFILES[profile]
            for i, name in enumerate(selected):
                image = directory / f"{profile}-{i}.png"
                Image.new("RGB", (16, 16), "#778877").save(image)
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

    monkeypatch.setattr("emend.typography.render", capture)
    return calls


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"line_height": 0.9},
        {"line_height": 2.5},
        {"line_height": float("nan")},
        {"line_height": float("inf")},
        {"paragraph_gap": -0.1},
        {"paragraph_gap": 2.1},
        {"paragraph_gap": "url(https://evil.invalid)"},
        {"line_height": True},
    ],
)
def test_only_explicit_bounded_numeric_settings_are_allowed(values):
    with pytest.raises(ValueError):
        TypographySettings(**values)


def test_trial_uses_identical_snapshot_and_explicit_css_without_mutating_books(
    tmp_path, config, fake_capture
):
    before = ingest(write_epub(tmp_path / "book.epub"), config)
    store = Store(config.root)
    row = store.resolve(before.book_id)
    values = TypographySettings(line_height=1.6, paragraph_gap=0)
    trial = typography_trial(before.book_id, config, values)
    assert trial.status == "complete" and trial.settings == values
    assert fake_capture[0].get("preview_css") is None
    assert fake_capture[1]["preview_css"] == values.css()
    assert "0em" in values.css()
    assert store.resolve(before.book_id) == row
    assert get_report(store, before.book_id) == before
    assert sha256_file(Path(row["artifact_path"])) == before.book_id
    assert load_trial(store, before.book_id, trial.id) == trial
    directory = Path(store.document(before.book_id, "typography-trial", trial.id)["path"]).parent
    html = (directory / "trial.html").read_text()
    assert "Current styling" in html and "Trial styling" in html
    assert "data:image/png;base64," in html and "not recommendations" in html
    assert not (config.root / "versions").exists()


@pytest.mark.parametrize("change", ["json", "css", "before", "after"])
def test_saved_trial_assets_are_verified(tmp_path, config, fake_capture, change):
    before = ingest(write_epub(tmp_path / "book.epub"), config)
    trial = typography_trial(before.book_id, config, TypographySettings(line_height=1.6))
    store = Store(config.root)
    path = Path(store.document(before.book_id, "typography-trial", trial.id)["path"])
    if change == "json":
        path.write_text(path.read_text() + "\n")
    elif change == "css":
        (path.parent / "trial.css").write_text("p {color:red}")
    else:
        sample = getattr(trial, change)[0]
        (path.parent / change / sample.screenshots[0].file).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="changed|hash mismatch"):
        load_trial(store, before.book_id, trial.id)


def test_changed_and_missing_samples_make_trial_incomplete(
    tmp_path, config, fake_capture, monkeypatch
):
    import emend.typography as typography

    before = ingest(write_epub(tmp_path / "book.epub"), config)
    capture = typography.render

    def incomplete(*args, **kwargs):
        version, samples = capture(*args, **kwargs)
        if kwargs.get("preview_css"):
            samples.pop()
            samples[0].error = "Capture budget exhausted."
            samples[0].screenshots = []
        return version, samples

    monkeypatch.setattr(typography, "render", incomplete)
    trial = typography_trial(before.book_id, config, TypographySettings(line_height=1.6))
    assert trial.status == "incomplete"
    assert load_trial(Store(config.root), before.book_id, trial.id).status == "incomplete"


@pytest.mark.parametrize("failure", ["browser", "snapshot", "version"])
def test_failed_trial_is_not_registered_and_books_stay_unchanged(
    tmp_path, config, fake_capture, monkeypatch, failure
):
    import emend.typography as typography

    before = ingest(write_epub(tmp_path / "book.epub"), config)
    capture = typography.render

    def fail(source, *args, **kwargs):
        if failure == "browser":
            raise ValueError("browser failed")
        version, samples = capture(source, *args, **kwargs)
        if kwargs.get("preview_css"):
            if failure == "snapshot":
                source.write_bytes(b"changed")
            else:
                version = "other-browser"
        return version, samples

    monkeypatch.setattr(typography, "render", fail)
    with pytest.raises(ValueError):
        typography_trial(before.book_id, config, TypographySettings(line_height=1.6))
    store = Store(config.root)
    assert get_report(store, before.book_id) == before
    assert not list((config.root / "typography" / before.book_id).iterdir())
    with pytest.raises(ValueError, match="No matching"):
        store.document(before.book_id, "typography-trial")


def test_invalid_profiles_and_damage_stop_before_capture(tmp_path, config, fake_capture):
    before = ingest(write_epub(tmp_path / "book.epub"), config)
    for profiles in [[], ["xteink"], ["compact", "compact"]]:
        with pytest.raises(ValueError, match="compact"):
            typography_trial(
                before.book_id, config, TypographySettings(line_height=1.6), profiles=profiles
            )
    files = book_files()
    del files["EPUB/observatory.png"]
    damaged = ingest(write_epub(tmp_path / "bad.epub", files), config)
    with pytest.raises(ValueError, match="structural"):
        typography_trial(damaged.book_id, config, TypographySettings(line_height=1.6))
    assert fake_capture == []


def test_cli_verifies_saved_trial_and_flags_stale_artifact(tmp_path, config, fake_capture):
    before, _ = setup_book(tmp_path, config)
    settings = tmp_path / "config.yaml"
    settings.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    runner = CliRunner()
    args = [before.book_id, "-c", str(settings)]
    assert runner.invoke(app, ["typography-trial", *args]).exit_code == 2
    result = runner.invoke(app, ["typography-trial", *args, "--line-height", "1.6", "--json"])
    assert result.exit_code == 0, result.output
    trial = json.loads(result.output)
    result = runner.invoke(app, ["typography-trials", *args, "--trial", trial["id"], "--json"])
    assert result.exit_code == 0 and json.loads(result.output)["current_artifact"]
    apply_proposals(before.book_id, config, safe=True)
    result = runner.invoke(app, ["typography-trials", *args, "--json"])
    assert result.exit_code == 1 and not json.loads(result.output)["current_artifact"]


@pytest.mark.parametrize("version", ["2.0", "3.0"])
@pytest.mark.parametrize("layout", ["scroll", "paginated"])
def test_real_browser_trial_changes_spacing_without_editing_epub(
    tmp_path, browser_config, version, layout
):
    config = browser_config
    files = book_files(version)
    files["EPUB/chapter.xhtml"] = files["EPUB/chapter.xhtml"].replace(
        b"</body>",
        b"<p>A long original paragraph for testing layout changes and the rhythm between lines.</p>"
        * 50
        + b"</body>",
    )
    before = ingest(write_epub(tmp_path / "book.epub", files), config)
    trial = typography_trial(
        before.book_id,
        config,
        TypographySettings(line_height=2.2, paragraph_gap=1.5),
        profiles=["compact"],
        max_documents=1,
        layout=layout,
    )
    assert trial.status == "complete", trial.model_dump()
    original, adjusted = trial.before[0], trial.after[0]
    assert original.screenshots[0].sha256 != adjusted.screenshots[0].sha256
    if layout == "scroll":
        assert adjusted.document_height > original.document_height
    else:
        assert adjusted.page_count > original.page_count
    assert get_report(Store(config.root), before.book_id) == before
    assert load_trial(Store(config.root), before.book_id, trial.id) == trial
