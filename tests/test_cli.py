import json

import yaml
from typer.testing import CliRunner

from emend.cli import app
from emend.config import load_config

runner = CliRunner()


def test_cli_workflow_with_explicit_config(epub, config, tmp_path):
    path = tmp_path / "config.yaml"
    result = runner.invoke(app, ["init", "--root", str(config.root), "--config", str(path)])
    assert result.exit_code == 0, result.output
    original = path.read_bytes()
    assert runner.invoke(app, ["init", "--config", str(path)]).exit_code == 2
    assert path.read_bytes() == original
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    assert runner.invoke(app, ["doctor", "-c", str(path)]).exit_code == 0
    inspection = runner.invoke(app, ["inspect", str(epub)])
    assert inspection.exit_code == 0
    book_id = json.loads(inspection.output)["source_sha256"]
    result = runner.invoke(app, ["ingest", str(epub), "-c", str(path)])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["report", book_id[:12], "--json", "-c", str(path)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["state"] == "VALIDATED"
    result = runner.invoke(app, ["list", "-c", str(path)])
    assert "VALIDATED" in result.output


def test_config_rejects_unknown_keys_and_resolves_paths(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("root: data\npublish:\n  calibre_library: library\n")
    config = load_config(path)
    assert config.root == tmp_path / "data"
    assert config.publish.calibre_library == tmp_path / "library"
    path.write_text("quality:\n  require_epubchek: false\n")
    result = runner.invoke(app, ["doctor", "-c", str(path)])
    assert result.exit_code == 2
    assert "require_epubchek" in result.output


def test_cli_blocked_ingest_returns_nonzero(epub, config, tmp_path):
    config.tools.epubcheck = tmp_path / "missing"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    assert runner.invoke(app, ["doctor", "-c", str(path)]).exit_code == 1
    result = runner.invoke(app, ["ingest", str(epub), "-c", str(path)])
    assert result.exit_code == 1
    assert "REVIEW_REQUIRED" in result.output


def test_backup_and_restore_cli(fake_publishers, tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(fake_publishers.model_dump(mode="json")))
    result = runner.invoke(app, ["backup", "--calibre-closed", "--to-drive", "-c", str(config)])
    assert result.exit_code == 0, result.output
    archive = json.loads(result.output)["archive"]
    assert runner.invoke(app, ["verify-backup", archive]).exit_code == 0
    restored = tmp_path / "restored"
    result = runner.invoke(app, ["restore", archive, "--to", str(restored)])
    assert result.exit_code == 0, result.output
    assert (restored / "metadata.db").is_file()
