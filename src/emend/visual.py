"""Offline, bounded Chromium document sampling. Advisory; never a publication gate."""

import base64
import html
import importlib.util
import shutil
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Literal
from urllib.parse import quote, unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field

from emend import ENGINE_VERSION
from emend.config import Config
from emend.inspect import NS, inspect_epub, local_target, parse_xml, sha256_file
from emend.pagination import check_package, page_indices, paginate, validate_layout
from emend.reports import report_styles
from emend.research.discovery import current_source
from emend.storage import Store, now

ORIGIN = "https://epub.emend.invalid"
PROFILES = {"compact": (600, 800), "wide": (1000, 1400)}
MAX_SCREENSHOT_BYTES = 48 * 1024**2
MAX_DOCUMENT_BYTES = 8 * 1024**2
CSP = (
    "default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "font-src 'self' data:; script-src 'none'; connect-src 'none'; "
    "frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
)


class Shot(BaseModel):
    file: str
    sha256: str
    scroll_y: int
    scroll_x: int = 0
    page_number: int | None = None


class Sample(BaseModel):
    document: str
    profile: str
    width: int
    height: int
    document_height: int = 0
    page_count: int | None = None
    observations: list[str] = Field(default_factory=list)
    screenshots: list[Shot] = Field(default_factory=list)
    error: str | None = None


class VisualReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    book_id: str
    artifact_sha256: str
    engine_version: str
    created_at: str
    browser_version: str
    layout: Literal["scroll", "paginated"] = "scroll"
    page_margin: int | None = None
    title: str
    total_spine_documents: int
    selected_documents: list[str]
    profiles: list[str]
    status: Literal["complete", "incomplete"]
    samples: list[Sample]
    notes: list[str] = Field(
        default_factory=lambda: [
            "Advisory Chromium document samples; no approval or publication state changed.",
            "Publisher styles are retained. Screenshots use scrolling documents, not ebook pagination.",
            "Compact and wide are generic browser sizes, not XTEINK or Apple Books emulation.",
            "At most three vertical positions per selected document are sampled; other content is unreviewed.",
            "Scripts, network access, embedded frames, and media playback are disabled. Obfuscated fonts may not render.",
            "Geometry observations are review hints. Absence of warnings does not certify readability or accessibility.",
        ]
    )


def availability(config: Config) -> str:
    if importlib.util.find_spec("playwright") is None:
        return "optional: install emend[visual] and run playwright install chromium"
    if config.tools.chromium:
        return f"configured Chromium: {config.tools.chromium} (launch checked by visual-review)"
    return "Playwright installed; visual-review checks its Chromium installation on launch"


def select_documents(spine: list[str], nav: list[str], limit: int) -> list[str]:
    if not 1 <= limit <= 9:
        raise ValueError("Choose between 1 and 9 documents.")
    if not spine:
        raise ValueError("No spine documents to render.")
    # Prioritize beginning/middle/end, then navigation and evenly spaced extra positions.
    candidates = [spine[0], spine[len(spine) // 2], spine[-1], *nav]
    candidates += [spine[round(i * (len(spine) - 1) / 8)] for i in range(9)]
    return list(dict.fromkeys(candidates))[:limit]


def resource_name(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "epub.emend.invalid" or parsed.query:
        return None
    name = unquote(parsed.path.removeprefix("/"))
    if not name or "\\" in name or any(p in {"", ".", ".."} for p in name.split("/")):
        return None
    return name


# Only our automation evaluates this code; EPUB JavaScript is disabled in the context.
MEASURE = """() => {
    const root = document.documentElement;
    const elements = [...document.querySelectorAll('body *')].slice(0, 20000);
    const visible = e => { const r=e.getBoundingClientRect(), s=getComputedStyle(e);
        return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden'; };
    const tiny = elements.filter(e => visible(e) && [...e.childNodes].some(n =>
        n.nodeType===3 && n.textContent.trim()) && parseFloat(getComputedStyle(e).fontSize)<10).length;
    const images = [...document.images];
    return {width:Math.max(root.scrollWidth, document.body?.scrollWidth || 0),
        height:Math.max(root.scrollHeight, document.body?.scrollHeight || 0),
        broken:images.filter(i=>!i.complete || !i.naturalWidth).length,
        images:images.length, text:(document.body?.innerText || '').trim().length,
        tiny, fonts:[...document.fonts].filter(f=>f.status==='error').length,
        truncated:document.querySelectorAll('body *').length>20000};
}"""


def render(
    snapshot: Path,
    directory: Path,
    selected: list[str],
    media: dict[str, str],
    profiles: list[str],
    config: Config,
    *,
    layout: str = "scroll",
    page_margin: int = 40,
) -> tuple[str, list[Sample]]:
    validate_layout(layout, page_margin)
    try:
        from playwright.sync_api import Error as BrowserError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ValueError(
            "Install the visual extra (uv sync --extra visual), then run uv run playwright install chromium."
        ) from exc
    samples = []
    budget = MAX_SCREENSHOT_BYTES
    deadline = time.monotonic() + min(config.limits.tool_timeout_seconds, 180)
    with zipfile.ZipFile(snapshot) as archive, sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(
                executable_path=str(config.tools.chromium) if config.tools.chromium else None,
                timeout=15000,
            )
        except BrowserError as exc:
            raise ValueError(
                "Chromium could not launch. Run playwright install chromium or set tools.chromium to an installed executable."
            ) from exc
        try:
            version = browser.version
            for profile in profiles:
                width, height = PROFILES[profile]
                context = browser.new_context(
                    viewport={"width": width, "height": height},
                    device_scale_factor=1,
                    java_script_enabled=False,
                    service_workers="block",
                    accept_downloads=False,
                    offline=True,
                    locale="en-US",
                    timezone_id="UTC",
                    color_scheme="light",
                    reduced_motion="reduce",
                )
                try:
                    for index, document in enumerate(selected):
                        sample = Sample(
                            document=document, profile=profile, width=width, height=height
                        )
                        samples.append(sample)
                        if time.monotonic() >= deadline or budget <= 0:
                            sample.error = "Review time or screenshot budget exhausted; this sample was not rendered."
                            continue
                        blocked = set()
                        navigation = {"served": False, "rejected": False}
                        page = context.new_page()
                        page.set_default_timeout(10000)

                        def route_resource(
                            route, blocked=blocked, navigation=navigation, document=document
                        ):
                            name = resource_name(route.request.url)
                            if name not in media or route.request.method != "GET":
                                blocked.add(name or "external or unsupported URL")
                                route.abort()
                                return
                            if route.request.is_navigation_request():
                                if name != document or navigation["served"]:
                                    navigation["rejected"] = True
                                    route.abort()
                                    return
                                navigation["served"] = True
                            try:
                                info = archive.getinfo(name)
                                if info.file_size > MAX_DOCUMENT_BYTES:
                                    raise ValueError("resource size")
                                route.fulfill(
                                    body=archive.read(name),
                                    content_type=media[name],
                                    headers={
                                        "Content-Security-Policy": CSP,
                                        "X-Content-Type-Options": "nosniff",
                                    },
                                )
                            except (KeyError, ValueError, OSError, zipfile.BadZipFile):
                                blocked.add(name)
                                route.abort()

                        page.route("**/*", route_resource)
                        try:
                            page.goto(
                                ORIGIN + "/" + quote(document, safe="/"),
                                wait_until="load",
                                timeout=10000,
                            )
                            page.wait_for_function(
                                "document.fonts.status === 'loaded'", timeout=5000
                            )
                            if navigation["rejected"]:
                                raise ValueError(
                                    "Document attempted automatic navigation; sample is incomplete."
                                )
                            if layout == "paginated":
                                pagination = paginate(page, width, height, page_margin)
                                sample.page_count = pagination["page_count"]
                                sample.observations.extend(pagination["observations"])
                            metrics = page.evaluate(MEASURE)
                            sample.document_height = metrics["height"]
                            if layout == "scroll" and metrics["width"] > width + 2:
                                sample.observations.append(
                                    f"Horizontal overflow: {metrics['width']} CSS px in a {width} px viewport."
                                )
                            for key, label in [
                                ("broken", "Images failed to load"),
                                ("tiny", "Visible text elements below 10 CSS px"),
                                ("fonts", "Font faces failed to load"),
                            ]:
                                if metrics[key]:
                                    sample.observations.append(f"{label}: {metrics[key]}.")
                            if not metrics["text"] and not metrics["images"]:
                                sample.observations.append(
                                    "No body text or HTML images detected; inspect SVG or other content manually."
                                )
                            if metrics["truncated"]:
                                sample.observations.append(
                                    "Text geometry inspection stopped at 20,000 elements."
                                )
                            if blocked:
                                sample.observations.append(
                                    f"Blocked or unavailable resources: {len(blocked)}."
                                )
                            bottom = max(0, metrics["height"] - height)
                            offsets = (
                                [
                                    (number * width, 0, number + 1)
                                    for number in page_indices(sample.page_count)
                                ]
                                if layout == "paginated"
                                else [(0, y, None) for y in sorted({0, bottom // 2, bottom})]
                            )
                            for position, (x, y, page_number) in enumerate(offsets):
                                if time.monotonic() >= deadline:
                                    raise ValueError("Review time budget exhausted.")
                                page.evaluate("([x, y]) => window.scrollTo(x, y)", [x, y])
                                actual_x, actual_y = page.evaluate(
                                    "[Math.round(window.scrollX), Math.round(window.scrollY)]"
                                )
                                if layout == "paginated" and (
                                    abs(actual_x - x) > 1 or actual_y != 0
                                ):
                                    raise ValueError(
                                        "Browser could not reach the requested page offset; sample is incomplete."
                                    )
                                data = page.screenshot(
                                    type="png",
                                    animations="allow" if layout == "paginated" else "disabled",
                                    timeout=10000,
                                )
                                if navigation["rejected"] or resource_name(page.url) != document:
                                    raise ValueError(
                                        "Document navigated during capture; sample is incomplete."
                                    )
                                if len(data) > budget:
                                    raise ValueError("Screenshot storage budget exhausted.")
                                budget -= len(data)
                                path = directory / f"{profile}-{index + 1}-{position + 1}.png"
                                path.write_bytes(data)
                                sample.screenshots.append(
                                    Shot(
                                        file=path.name,
                                        sha256=sha256_file(path),
                                        scroll_y=actual_y,
                                        scroll_x=actual_x,
                                        page_number=page_number,
                                    )
                                )
                        except (BrowserError, ValueError) as exc:
                            # Do not embed browser diagnostics containing arbitrary source text/URLs.
                            sample.error = (
                                str(exc)
                                if isinstance(exc, ValueError)
                                else "Browser could not finish this document; review is incomplete."
                            )
                        finally:
                            page.close()
                finally:
                    context.close()
            return version, samples
        finally:
            browser.close()


def visual_review(
    book_id: str,
    config: Config,
    *,
    profiles: list[str] | None = None,
    max_documents: int = 5,
    layout: str = "scroll",
    page_margin: int = 40,
) -> VisualReview:
    validate_layout(layout, page_margin)
    profiles = profiles if profiles is not None else list(PROFILES)
    if (
        not profiles
        or len(profiles) != len(set(profiles))
        or any(p not in PROFILES for p in profiles)
    ):
        raise ValueError("Use compact and/or wide once each.")
    store = Store(config.root)
    with store.locked():
        row, source, digest = current_source(store, book_id, config)
        with tempfile.TemporaryDirectory(
            prefix="visual-", dir=config.root / ".staging"
        ) as temporary:
            snapshot = Path(temporary) / "source.epub"
            shutil.copyfile(source, snapshot)
            if sha256_file(snapshot) != digest:
                raise ValueError("Artifact changed before visual review.")
            inspection = inspect_epub(snapshot, config.limits)
            if inspection.blocking or not inspection.package_path:
                raise ValueError("Resolve structural inspection errors before visual review.")
            media, nav = {}, []
            with zipfile.ZipFile(snapshot) as archive:
                package = parse_xml(archive.read(inspection.package_path))
                if layout == "paginated":
                    check_package(package)
                for item in package.xpath("opf:manifest/opf:item", namespaces=NS):
                    target = local_target(inspection.package_path, item.get("href", ""))
                    if target:
                        name, _ = target
                        media[name] = item.get("media-type", "application/octet-stream")
                        if "nav" in item.get("properties", "").split():
                            nav.append(name)
            selected = select_documents(inspection.spine, nav, max_documents)
            if any(media.get(name) != "application/xhtml+xml" for name in selected):
                raise ValueError("Visual review currently supports XHTML spine documents only.")
            identifier = uuid.uuid4().hex
            directory = config.root / "visual" / row["id"] / identifier
            directory.mkdir(parents=True)
            try:
                version, samples = render(
                    snapshot,
                    directory,
                    selected,
                    media,
                    profiles,
                    config,
                    layout=layout,
                    page_margin=page_margin,
                )
                _, _, latest = current_source(store, row["id"], config)
                if latest != digest:
                    raise ValueError("Artifact changed during visual review.")
                result = VisualReview(
                    id=identifier,
                    book_id=row["id"],
                    artifact_sha256=digest,
                    engine_version=ENGINE_VERSION,
                    created_at=now(),
                    browser_version=version,
                    layout=layout,
                    page_margin=page_margin if layout == "paginated" else None,
                    title="; ".join(inspection.metadata.get("title", [])),
                    total_spine_documents=len(inspection.spine),
                    selected_documents=selected,
                    profiles=profiles,
                    status="incomplete" if any(s.error for s in samples) else "complete",
                    samples=samples,
                )
                if layout == "paginated":
                    result.notes[1] = (
                        f"Chromium column pagination with {page_margin} CSS px page margins. Preview geometry and forced page breaks are adapted; EPUB bytes and publisher font/text styles stay unchanged."
                    )
                    result.notes[3] = (
                        "First, middle, and last pages of each selected document are sampled. Page numbers restart per document and are not book-wide reader locations."
                    )
                    result.notes.append(
                        "Horizontal LTR reflowable documents only. Fixed layouts, RTL/vertical flow, column spanners, and left/right page parity need another reader."
                    )
                path = directory / "review.json"
                path.write_text(result.model_dump_json(indent=2))
                write_html(result, directory)
                store.save_document(identifier, row["id"], "visual-review", path, sha256_file(path))
                return result
            except Exception:
                shutil.rmtree(directory)
                raise


def load_visual_review(store: Store, book_id: str, identifier: str | None = None) -> VisualReview:
    row = store.document(book_id, "visual-review", identifier)
    path = Path(row["path"])
    if sha256_file(path) != row["sha256"]:
        raise ValueError("Visual review changed after registration.")
    result = VisualReview.model_validate_json(path.read_text())
    if result.id != row["id"] or result.book_id != book_id:
        raise ValueError("Visual review identity mismatch.")
    for sample in result.samples:
        for shot in sample.screenshots:
            if (
                Path(shot.file).name != shot.file
                or sha256_file(path.parent / shot.file) != shot.sha256
            ):
                raise ValueError("Visual screenshot hash mismatch.")
    return result


def write_html(review: VisualReview, directory: Path):
    esc = html.escape
    sections = []
    for sample in review.samples:
        figures = []
        for shot in sample.screenshots:
            data = base64.b64encode((directory / shot.file).read_bytes()).decode()
            position = (
                f"Page {shot.page_number} of {sample.page_count}"
                if shot.page_number is not None
                else f"Offset {shot.scroll_y} px"
            )
            label = f"{sample.document}, {sample.profile}, {position}"
            figures.append(
                f'<figure><img src="data:image/png;base64,{data}" width="{sample.width}" height="{sample.height}" alt="{esc(label)}" loading="lazy"><figcaption>{esc(position)} · {sample.width} × {sample.height}</figcaption></figure>'
            )
        notes = [*sample.observations, *([sample.error] if sample.error else [])]
        if not notes:
            notes = ["No geometry warnings detected in this sample. Review the screenshots."]
        extent = (
            f"{sample.page_count} {'page' if sample.page_count == 1 else 'pages'} in document"
            if sample.page_count
            else f"document height {sample.document_height:,} px"
        )
        sections.append(
            f'<section><h2>{esc(sample.document)}</h2><p>{esc(sample.profile.title())} · {esc(extent)}</p><ul>{"".join(f"<li>{esc(n)}</li>" for n in notes)}</ul><div class="shots">{"".join(figures)}</div></section>'
        )
    (
        directory / "review.html"
    ).write_text(f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:"><title>Emend visual review</title><style>{report_styles()}
.shots {{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,17rem),1fr));gap:1.5rem;margin-top:1.5rem}}
figure {{margin:0;min-width:0}} figure img {{display:block;width:auto;max-width:100%;height:auto;border:1px solid var(--rule)}}
figcaption {{font-family:var(--mono);font-size:.75rem;margin-top:.5rem;color:var(--muted)}}
section {{border-bottom:1px solid var(--rule);padding-bottom:2rem}} h2 {{overflow-wrap:anywhere}}
</style></head><body><main><header><div class="brand">EMEND / VISUAL REVIEW</div><h1>{esc(review.title)}</h1><p>Sampling {esc(review.status)} · {esc(review.layout.title())} layout · {len(review.selected_documents)} selected documents · {review.total_spine_documents} spine documents in book</p><p>Review layout, spacing, illustrations, and reading comfort at the recorded viewport sizes.</p><code>Artifact {review.artifact_sha256}</code></header><h2>Scope of this review</h2><ul>{"".join(f"<li>{esc(n)}</li>" for n in review.notes)}</ul>{"".join(sections)}<footer>Generated {esc(review.created_at)} using Chromium {esc(review.browser_version)}. Screenshots belong to the recorded artifact; a later edit requires a new review. Browser rendering is not device certification.</footer></main></body></html>""")
