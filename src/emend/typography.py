"""Offline typography trials. Explicit numeric overrides affect browser previews only."""

import base64
import html
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from emend import ENGINE_VERSION
from emend.config import Config
from emend.inspect import NS, inspect_epub, local_target, parse_xml, sha256_file
from emend.pagination import check_package, validate_layout
from emend.reports import report_styles
from emend.research.discovery import current_source
from emend.revalidation import snapshot
from emend.storage import Store, now
from emend.visual import PROFILES, Sample, render, select_documents


class TypographySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    line_height: float | None = Field(default=None, ge=1, le=2.4, allow_inf_nan=False)
    paragraph_gap: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False)

    @model_validator(mode="after")
    def explicit_values(self):
        if self.line_height is None and self.paragraph_gap is None:
            raise ValueError("Choose --line-height and/or --paragraph-gap for the trial.")
        return self

    def css(self) -> str:
        rules = []
        if self.line_height is not None:
            rules.append(f"body, p {{ line-height: {self.line_height:g} !important; }}")
        if self.paragraph_gap is not None:
            rules.append(
                f"p {{ margin-block-start: 0 !important; margin-block-end: {self.paragraph_gap:g}em !important; }}"
            )
        return "\n".join(rules) + "\n"


class TypographyTrial(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    book_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    artifact_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    engine_version: str
    created_at: str
    browser_version: str
    title: str
    settings: TypographySettings
    css_sha256: str
    layout: Literal["scroll", "paginated"]
    page_margin: int | None = None
    selected_documents: list[str]
    profiles: list[str]
    status: Literal["complete", "incomplete"]
    before: list[Sample]
    after: list[Sample]


def typography_trial(
    book_id: str,
    config: Config,
    settings: TypographySettings,
    *,
    profiles: list[str] | None = None,
    max_documents: int = 5,
    layout: str = "scroll",
    page_margin: int = 40,
) -> TypographyTrial:
    validate_layout(layout, page_margin)
    profiles = profiles if profiles is not None else list(PROFILES)
    if (
        not profiles
        or len(set(profiles)) != len(profiles)
        or any(p not in PROFILES for p in profiles)
    ):
        raise ValueError("Use compact and/or wide once each.")
    # Revalidate mutable model instances supplied through the Python API.
    settings = TypographySettings.model_validate(settings.model_dump())
    store = Store(config.root)
    with store.locked():
        row, source, digest = current_source(store, book_id, config)
        with tempfile.TemporaryDirectory(dir=config.root / ".staging") as staging:
            current = Path(staging) / "current.epub"
            snapshot(source, current, config.limits.max_archive_mb * 1024**2)
            inspection = inspect_epub(current, config.limits)
            if (
                inspection.source_sha256 != digest
                or inspection.blocking
                or not inspection.package_path
            ):
                raise ValueError(
                    "Resolve structural errors or changed inputs before a typography trial."
                )
            media, nav = {}, []
            with zipfile.ZipFile(current) as archive:
                package = parse_xml(archive.read(inspection.package_path))
                if layout == "paginated":
                    check_package(package)
                for item in package.xpath("opf:manifest/opf:item", namespaces=NS):
                    target = local_target(inspection.package_path, item.get("href", ""))
                    if target:
                        media[target[0]] = item.get("media-type", "application/octet-stream")
                        if "nav" in item.get("properties", "").split():
                            nav.append(target[0])
            selected = select_documents(inspection.spine, nav, max_documents)
            if any(media.get(name) != "application/xhtml+xml" for name in selected):
                raise ValueError("Typography trials support XHTML documents only.")
            identifier = uuid.uuid4().hex
            directory = config.root / "typography" / row["id"] / identifier
            directory.mkdir(parents=True)
            try:
                for phase in ("before", "after"):
                    (directory / phase).mkdir()
                css = settings.css()
                css_path = directory / "trial.css"
                css_path.write_text(css)
                version, before = render(
                    current,
                    directory / "before",
                    selected,
                    media,
                    profiles,
                    config,
                    layout=layout,
                    page_margin=page_margin,
                )
                after_version, after = render(
                    current,
                    directory / "after",
                    selected,
                    media,
                    profiles,
                    config,
                    layout=layout,
                    page_margin=page_margin,
                    preview_css=css,
                )
                if after_version != version:
                    raise ValueError("Browser version changed between comparison captures.")
                _, _, latest = current_source(store, row["id"], config)
                if latest != digest or sha256_file(current) != digest:
                    raise ValueError("An EPUB changed during typography capture.")
                expected = {(name, profile) for name in selected for profile in profiles}
                complete = all(
                    len(samples) == len(expected)
                    and {(s.document, s.profile) for s in samples} == expected
                    and all(not s.error and s.screenshots for s in samples)
                    for samples in (before, after)
                )
                result = TypographyTrial(
                    id=identifier,
                    book_id=row["id"],
                    artifact_sha256=digest,
                    engine_version=ENGINE_VERSION,
                    created_at=now(),
                    browser_version=version,
                    title="; ".join(inspection.metadata.get("title", [])),
                    settings=settings,
                    css_sha256=sha256_file(css_path),
                    layout=layout,
                    page_margin=page_margin if layout == "paginated" else None,
                    selected_documents=selected,
                    profiles=profiles,
                    status="complete" if complete else "incomplete",
                    before=before,
                    after=after,
                )
                path = directory / "trial.json"
                path.write_text(result.model_dump_json(indent=2))
                write_trial_html(result, directory)
                store.save_document(
                    identifier, row["id"], "typography-trial", path, sha256_file(path)
                )
                return result
            except Exception:
                shutil.rmtree(directory)
                raise


def load_trial(store: Store, book_id: str, identifier: str | None = None) -> TypographyTrial:
    document = store.document(book_id, "typography-trial", identifier)
    path = Path(document["path"])
    expected = store.root / "typography" / book_id / document["id"] / "trial.json"
    if path.resolve() != expected.resolve() or path.stat().st_size > 8 * 1024**2:
        raise ValueError("Invalid typography trial location or size.")
    if sha256_file(path) != document["sha256"]:
        raise ValueError("Typography trial changed after registration.")
    result = TypographyTrial.model_validate_json(path.read_text())
    if result.id != document["id"] or result.book_id != book_id:
        raise ValueError("Typography trial identity mismatch.")
    css = path.parent / "trial.css"
    if (
        css.stat().st_size > 4096
        or sha256_file(css) != result.css_sha256
        or css.read_text() != result.settings.css()
    ):
        raise ValueError("Typography trial stylesheet changed after capture.")
    for phase in ("before", "after"):
        for sample in getattr(result, phase):
            for shot in sample.screenshots:
                if (
                    Path(shot.file).name != shot.file
                    or sha256_file(path.parent / phase / shot.file) != shot.sha256
                ):
                    raise ValueError("Typography screenshot hash mismatch.")
    return result


def write_trial_html(trial: TypographyTrial, directory: Path):
    esc = html.escape
    sections = []
    for name in trial.selected_documents:
        for profile in trial.profiles:
            columns = []
            for phase, label in (("before", "Current styling"), ("after", "Trial styling")):
                sample = next(
                    (
                        s
                        for s in getattr(trial, phase)
                        if s.document == name and s.profile == profile
                    ),
                    None,
                )
                figures = []
                notes = []
                if sample:
                    notes = [*sample.observations, *([sample.error] if sample.error else [])]
                    for shot in sample.screenshots:
                        data = base64.b64encode(
                            (directory / phase / shot.file).read_bytes()
                        ).decode()
                        position = (
                            f"Page {shot.page_number} of {sample.page_count}"
                            if shot.page_number is not None
                            else f"Offset {shot.scroll_y} px"
                        )
                        caption = f"{label} · {position} · {sample.width} × {sample.height}"
                        figures.append(
                            f'<figure><img src="data:image/png;base64,{data}" alt="{esc(name + ": " + caption)}" width="{sample.width}" height="{sample.height}" loading="lazy"><figcaption>{esc(caption)}</figcaption></figure>'
                        )
                if not figures:
                    notes.append("No screenshots available for this sample.")
                columns.append(
                    f'<div class="comparison-side"><h3>{label}</h3><ul>{"".join(f"<li>{esc(n)}</li>" for n in notes)}</ul>{"".join(figures)}</div>'
                )
            sections.append(
                f'<section><h2>{esc(name)} · {esc(profile)}</h2><div class="comparison">{"".join(columns)}</div></section>'
            )
    values = []
    if trial.settings.line_height is not None:
        values.append(f"body/p line height: {trial.settings.line_height:g} (unitless)")
    if trial.settings.paragraph_gap is not None:
        values.append(f"p block margins: 0 before / {trial.settings.paragraph_gap:g}em after")
    (
        directory / "trial.html"
    ).write_text(f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:"><title>Emend typography trial</title><style>{report_styles()}
.comparison {{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:2rem}} .comparison-side {{min-width:0}}
figure {{margin:0 0 2rem}} figure img {{max-width:100%;height:auto;display:block;border:1px solid var(--rule)}}
figcaption {{font: .75rem/1.6 var(--mono);margin-top:.5rem;color:var(--muted)}} section {{border-bottom:1px solid var(--rule);padding-bottom:2rem}}
h2, p {{overflow-wrap:anywhere}} pre {{white-space:pre-wrap;overflow-wrap:anywhere}}
@media(max-width:650px) {{.comparison {{grid-template-columns:1fr}}}}
</style></head><body><main><header><div class="brand">EMEND / TYPOGRAPHY TRIAL</div><h1>{esc(trial.title)}</h1><p>{esc(trial.status.title())} · {esc(trial.layout)} preview</p><p>{esc("; ".join(values))}</p><code>Artifact {trial.artifact_sha256}</code></header>
<h2>Scope</h2><p>These are explicit browser-only overrides, not recommendations or EPUB edits. The current and trial views use the same preserved snapshot and viewport sizes. Only the chosen line-height/margin rules change; publisher fonts, alignment, indentation, and other styles are retained.</p><p>The trial uses !important rules on body/p; nested author rules may still affect individual text. Paragraph overrides also affect captions, verse and notes marked as p. Review every kind of content before considering an edit. Images below sample document starts/middles/ends; changing flow can change sampled text and page counts. This is not a line-aligned comparison or a device preview.</p><p>No approval, publication, or quality state changed. Each view has its own bounded capture budget; an incomplete comparison is not a passing review.</p><h2>Trial CSS</h2><pre><code>{esc(trial.settings.css())}</code></pre>{"".join(sections)}<footer>Generated {esc(trial.created_at)} with Chromium {esc(trial.browser_version)}. Saved CSS and screenshot hashes are verified when reopening the trial.</footer></main></body></html>""")
