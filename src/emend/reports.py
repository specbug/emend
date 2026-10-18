import base64
import html
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

from emend.models import Report


@lru_cache(maxsize=1)
def report_styles() -> str:
    assets = files("emend").joinpath("assets")
    css = []
    for family, filename, license_file, weights in [
        ("DM Sans", "DMSans.ttf", "DMSans-OFL.txt", "100 1000"),
        ("IBM Plex Mono", "IBMPlexMono-Regular.ttf", "IBMPlexMono-OFL.txt", "400"),
    ]:
        encoded = base64.b64encode(assets.joinpath(filename).read_bytes()).decode("ascii")
        notice = assets.joinpath(license_file).read_text().replace("*/", "* /")
        css.append(f"/* {notice} */")
        css.append(
            f"@font-face {{ font-family: '{family}'; src: url(data:font/ttf;base64,{encoded}) format('truetype'); font-weight: {weights}; font-style: normal; font-display: swap; }}"
        )
    css.append(assets.joinpath("report.css").read_text())
    return "\n".join(css)


def report_text(report: Report) -> str:
    title = "; ".join(report.inspection.metadata.get("title", [])) or report.source_name
    lines = [
        title,
        f"{report.state} · {report.book_id}",
        "",
        "Original EPUB preserved. "
        + (
            "Curated changes recorded below."
            if report.changes
            else "No editorial transformations applied."
        ),
        *report.changes,
        "",
    ]
    for gate in report.gates:
        lines.append(
            f"{gate.name}: {gate.status} ({'required' if gate.required else 'optional'}) — {gate.detail}"
        )
    lines.append("")
    for finding in report.inspection.findings:
        lines.append(
            f"{finding.severity.upper()} {finding.code}: {finding.message}"
            + (f" [{finding.location}]" if finding.location else "")
        )
    return "\n".join(lines)


def write_reports(report: Report, directory: Path) -> Path:
    path = directory / "report.json"
    path.write_text(report.model_dump_json(indent=2))
    (directory / "report.txt").write_text(report_text(report))
    esc = html.escape
    title = "; ".join(report.inspection.metadata.get("title", [])) or report.source_name
    labels = {
        "structure": "EPUB structure",
        "preservation": "Content preservation",
        "epubcheck": "EPUBCheck",
        "accessibility": "Accessibility",
        "visual_qa": "Visual inspection",
    }
    gates = "".join(
        f'<tr><th scope="row">{esc(labels.get(g.name, g.name))}</th><td class="{g.status}">{g.status.capitalize()}</td><td class="policy">{"Required" if g.required else "Optional"}</td><td class="detail">{esc(g.detail)}</td></tr>'
        for g in report.gates
    )
    findings = (
        "".join(
            f"<li><strong>{esc(f.severity.upper())} · {esc(f.code)}</strong><p>{esc(f.message)}</p><code>{esc(f.location or '')}</code></li>"
            for f in report.inspection.findings
        )
        or "<li>No findings from the built-in inspector.</li>"
    )
    changes = ""
    for change in report.metadata_changes:
        sources = []
        for url in change.evidence_urls:
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.hostname not in {
                "openlibrary.org",
                "www.googleapis.com",
            }:
                continue
            label = (
                "Google Books"
                if parsed.hostname == "www.googleapis.com"
                else "Open Library author"
                if "/authors/" in parsed.path
                else "Open Library edition"
            )
            sources.append(f'<a href="{esc(url, quote=True)}">{label}</a>')
        changes += (
            f"<li><strong>{esc(change.field.capitalize())}</strong>"
            f"<p>{esc('; '.join(change.before) or '(missing)')} → {esc('; '.join(change.after))}</p>"
            f'<p class="sources">Sources: {" · ".join(sources)}</p></li>'
        )
    if not changes and report.cover_changes:
        changes = "<li>No metadata changes recorded.</li>"
    if not changes:
        changes = (
            "".join(f"<li>{esc(change)}</li>" for change in report.changes)
            or "<li>No editorial changes.</li>"
        )
    cover_changes = "".join(
        f"<li><strong>Cover image</strong><p>{c.before_dimensions[0]} × {c.before_dimensions[1]} → {c.after_dimensions[0]} × {c.after_dimensions[1]} px</p><p>{esc(c.source)}</p><code>{esc(c.after_sha256)}</code></li>"
        for c in report.cover_changes
    )
    cover_section = f"<h2>Cover changes</h2><ul>{cover_changes}</ul>" if cover_changes else ""
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; font-src data:">
<title>{esc(title)} — Emend report</title>
<style>
{report_styles()}
</style></head><body>
<main>
<header><div class="brand">EMEND / QUALITY REPORT</div><h1>{esc(title)}</h1><p class="state">{report.state}</p>
<p>Original preserved · {"Curated changes recorded" if report.changes else "No editorial changes"}</p><code>{report.book_id}</code></header>
<h2>Publication gates</h2><table><thead><tr><th>Check</th><th>Result</th><th>Policy</th><th>Evidence summary</th></tr></thead><tbody>{gates}</tbody></table>
<h2>Metadata changes</h2><ul>{changes}</ul>
{cover_section}
<h2>Inspection findings</h2><ul>{findings}</ul>
<h2>Preservation</h2><p>{len(report.inspection.resource_hashes)} resources fingerprinted · {len(report.inspection.spine)} spine documents · {report.inspection.text_characters:,} text characters inventoried</p>
<p>Artifact SHA-256: <code>{esc(report.artifact_sha256 or "No approved artifact")}</code></p>
<footer>Generated {esc(report.created_at)} · Engine {esc(report.engine_version)}. This report records automated checks, not publication-quality or device certification. Visual QA is not implemented. Edition identity and artwork selection require evidence review; neither is certified by automated checks.</footer>
</main>
</body></html>"""
    (directory / "report.html").write_text(page)
    return path
