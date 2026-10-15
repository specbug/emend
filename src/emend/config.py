import hashlib
import json
import os
import shutil
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Quality(StrictModel):
    require_epubcheck: bool = True
    require_accessibility_audit: bool = False


class Limits(StrictModel):
    max_archive_mb: int = Field(default=256, ge=1, le=2048)
    max_uncompressed_mb: int = Field(default=512, ge=1, le=4096)
    max_member_mb: int = Field(default=64, ge=1, le=512)
    max_members: int = Field(default=10000, ge=1, le=100000)
    tool_timeout_seconds: int = Field(default=180, ge=1, le=3600)
    publish_timeout_seconds: int = Field(default=3600, ge=1, le=86400)


class ToolPaths(StrictModel):
    calibre_bin: Path | None = None
    epubcheck: Path | None = None
    epubcheck_jar: Path | None = None
    ace: Path | None = None
    rclone: Path | None = None


class Publishing(StrictModel):
    calibre_library: Path | None = None
    drive_remote: str | None = None
    backup_remote: str | None = None


class Config(StrictModel):
    root: Path = Field(default_factory=lambda: Path.home() / "Emend")
    quality: Quality = Field(default_factory=Quality)
    limits: Limits = Field(default_factory=Limits)
    tools: ToolPaths = Field(default_factory=ToolPaths)
    publish: Publishing = Field(default_factory=Publishing)

    def policy_hash(self) -> str:
        policy = {"quality": self.quality.model_dump(), "limits": self.limits.model_dump()}
        return hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()


def default_config_path() -> Path:
    return Path(os.environ.get("EMEND_CONFIG", "~/Emend/config.yaml")).expanduser()


def load_config(path: Path | None = None) -> Config:
    path = (path or default_config_path()).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Configuration not found: {path}. Run emend init first.")
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("Configuration must be a YAML mapping.")
    config = Config.model_validate(raw)
    # Resolve relative paths against the configuration, regardless of the caller's cwd.
    for obj, fields in [
        (config, ["root"]),
        (config.tools, list(ToolPaths.model_fields)),
        (config.publish, ["calibre_library"]),
    ]:
        for field in fields:
            value = getattr(obj, field)
            if value is not None:
                value = value.expanduser()
                setattr(
                    obj,
                    field,
                    (path.parent / value).resolve() if not value.is_absolute() else value.resolve(),
                )
    return config


def executable(name: str, config: Config) -> str | None:
    override = getattr(config.tools, name, None)
    if override is not None:
        path = Path(override)
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    if name in {"calibredb", "ebook-meta", "ebook-polish", "fetch-ebook-metadata"}:
        folder = config.tools.calibre_bin or Path("/Applications/calibre.app/Contents/MacOS")
        path = folder / name
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        if config.tools.calibre_bin is not None:
            return None
    return shutil.which(name)
