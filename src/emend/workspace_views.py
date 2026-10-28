"""HTML for the local workspace. Rendering reads saved state; it never changes a book."""

import hashlib
import html
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

from emend import ENGINE_VERSION, __version__
from emend.inspect import sha256_file
from emend.pipeline import get_report
from emend.research.discovery import current_source
from emend.review_guidance import (
    CHECK_NAMES,
    FIX_NAMES,
    SPACING_PRESETS,
    STATUS_NAMES,
    blockers,
    cover_observation,
    epubcheck_messages,
)

STATIONS = [
    ("import", "Import"),
    ("check", "Check"),
    ("repair", "Repair"),
    ("preview", "Preview"),
    ("refine", "Refine"),
    ("send", "Send"),
]
STATION_KEYS = [key for key, _ in STATIONS]
# Which station a background run step belongs to, per run kind.
RUN_STATIONS = {
    "keep": "import",
    "repair": "repair",
    "preview": "preview",
    "notes": "preview",
    "trial": "refine",
    "apply": "refine",
    "restore": "refine",
}
TAG_WORDS = {
    "done": "Done",
    "running": "Working",
    "waiting": "Waiting",
    "attention": "Needs you",
    "failed": "Stopped",
    "skipped": "Skipped",
    "optional": "Optional",
    "ready": "Ready",
}
SUPPORTED_REPAIRS = (
    "ZIP packaging, contents links whose letter case does not match the file, "
    "empty root language attributes and paragraph wrappers that only hold blocks"
)


def esc(value):
    return html.escape(str(value), quote=True)


def plural(count, word, many=None):
    return f"{count:,} {word if count == 1 else many or word + 's'}"


def short(digest):
    return digest[:12] if digest else ""


def nice_time(stamp):
    """ISO timestamp → '28 Oct 2026, 14:05 UTC' without pretending to know local time."""
    try:
        from datetime import datetime

        moment = datetime.fromisoformat(str(stamp))
        return moment.strftime("%-d %b %Y, %H:%M UTC")
    except (ValueError, TypeError):
        return str(stamp)[:32]


def nice_day(stamp):
    try:
        from datetime import datetime

        return datetime.fromisoformat(str(stamp)).strftime("%-d %b")
    except (ValueError, TypeError):
        return ""


# Assets ---------------------------------------------------------------------

FONTS = {
    "DMSans.ttf": ("DM Sans", "DMSans-OFL.txt", "100 1000"),
    "IBMPlexMono-Regular.ttf": ("IBM Plex Mono", "IBMPlexMono-OFL.txt", "400"),
}


@lru_cache(maxsize=1)
def stylesheet() -> bytes:
    assets = files("emend").joinpath("assets")
    css = []
    for filename, (family, license_file, weights) in FONTS.items():
        notice = assets.joinpath(license_file).read_text().replace("*/", "* /")
        css.append(f"/* {notice} */")
        css.append(
            f"@font-face {{ font-family: '{family}'; src: url('/assets/{filename}') format('truetype'); "
            f"font-weight: {weights}; font-style: normal; font-display: swap; }}"
        )
    css.append(assets.joinpath("workspace.css").read_text())
    return "\n".join(css).encode()


@lru_cache(maxsize=8)
def asset(name) -> tuple[bytes, str]:
    if name == "workspace.css":
        return stylesheet(), "text/css; charset=utf-8"
    if name == "workspace.js":
        data = files("emend").joinpath("assets", "workspace.js").read_bytes()
        return data, "text/javascript; charset=utf-8"
    if name == "favicon.svg":
        return files("emend").joinpath("assets", name).read_bytes(), "image/svg+xml"
    if name in FONTS:
        return files("emend").joinpath("assets", name).read_bytes(), "font/ttf"
    raise KeyError(name)


@lru_cache(maxsize=1)
def asset_version():
    digest = hashlib.sha256(__version__.encode())
    for name in ("workspace.css", "workspace.js"):
        digest.update(asset(name)[0])
    return digest.hexdigest()[:10]


# Small pieces ---------------------------------------------------------------

ICONS = {
    "done": '<svg viewBox="0 0 16 16" aria-hidden="true"><path class="stroke" d="M3.5 8.5l3 3 6-7"/></svg>',
    "ready": '<svg viewBox="0 0 16 16" aria-hidden="true"><path class="stroke" d="M3.5 8.5l3 3 6-7"/></svg>',
    "running": '<svg viewBox="0 0 16 16" aria-hidden="true"><g class="spin"><path class="stroke" d="M8 2.5a5.5 5.5 0 1 1-5.5 5.5"/></g></svg>',
    "waiting": '<svg viewBox="0 0 16 16" aria-hidden="true"><circle class="stroke" cx="8" cy="8" r="3"/></svg>',
    "optional": '<svg viewBox="0 0 16 16" aria-hidden="true"><circle class="stroke" cx="8" cy="8" r="3" stroke-dasharray="2 2"/></svg>',
    "attention": '<svg viewBox="0 0 16 16" aria-hidden="true"><path class="stroke" d="M8 3v6.5M8 12.5v.5"/></svg>',
    "failed": '<svg viewBox="0 0 16 16" aria-hidden="true"><path class="stroke" d="M4.5 4.5l7 7M11.5 4.5l-7 7"/></svg>',
    "skipped": '<svg viewBox="0 0 16 16" aria-hidden="true"><path class="stroke" d="M4 8h8"/></svg>',
}
MARK = (
    '<svg viewBox="0 0 20 20" aria-hidden="true"><rect class="stroke" x="3.75" y="2.75" width="12.5" '
    'height="14.5" rx="1"/><path class="stroke" d="M7.25 2.75v14.5M10 7h3.5M10 10h3.5"/></svg>'
)
BOOK_ICON = (
    '<svg viewBox="0 0 28 28" aria-hidden="true"><path class="stroke" d="M14 17V5M9 10l5-5 5 5"/>'
    '<path class="stroke" d="M5 17v4.5a1.5 1.5 0 0 0 1.5 1.5h15a1.5 1.5 0 0 0 1.5-1.5V17"/></svg>'
)


def tag(state, text=None):
    return (
        f'<span class="tag is-{esc(state)}">{ICONS.get(state, "")}'
        f"<span>{esc(text or TAG_WORDS.get(state, state))}</span></span>"
    )


def label(text, cls=""):
    return f'<p class="label{" " + cls if cls else ""}">{esc(text)}</p>'


def facts(rows):
    items = "".join(f"<div><dt>{esc(k)}</dt><dd>{v}</dd></div>" for k, v in rows if v)
    return f'<dl class="facts">{items}</dl>'


def spine_svg(spine, seen=(), flagged=(), name="Reading order"):
    """One tick per spine document, in reading order: previewed and problem files marked."""
    count = max(len(spine), 1)
    pitch = 14 if count <= 40 else 9 if count <= 120 else 6
    width = 3
    rects = []
    for index, document in enumerate(spine[:2000]):
        cls = "flag" if document in flagged else "seen" if document in seen else ""
        title = f"<title>{esc(document)}</title>" if len(spine) <= 400 else ""
        rects.append(
            f'<rect x="{index * pitch}" y="0" width="{width}" height="10"'
            f"{f' class={chr(34)}{cls}{chr(34)}' if cls else ''}>{title}</rect>"
        )
    extent = (min(count, 2000) - 1) * pitch + width
    return (
        f'<svg class="spine" width="{extent}" height="10" viewBox="0 0 {extent} 10" '
        f'preserveAspectRatio="none" role="img" '
        f'aria-label="{esc(name)}: {plural(len(spine), "document")}">{"".join(rects)}</svg>'
    )


def matches_document(where, spine):
    path = where.split(":", 1)[0].strip()
    if not path:
        return None
    for document in spine:
        if path == document or document.endswith("/" + path) or path.endswith("/" + document):
            return document
    return None


def checklist(steps):
    rows = []
    for step in steps:
        seconds = step.seconds
        clock = (
            f'<span class="label">{seconds}s</span>'
            if step.state in {"done", "failed"} and seconds is not None
            else "<span></span>"
        )
        note = f"<p>{esc(step.note)}</p>" if step.note else ""
        rows.append(
            f'<li class="is-{esc(step.state)}">{ICONS.get(step.state, "")}<span>{esc(step.label)}</span>{clock}{note}</li>'
        )
    return f'<ol class="checklist">{"".join(rows)}</ol>'


def page(title, body, *, notice="", status=None, version=0, kind="page"):
    asset_key = asset_version()
    poll = f' data-status="{esc(status)}" data-version="{version}"' if status else ""
    refresh = '<noscript><meta http-equiv="refresh" content="3"></noscript>' if status else ""
    note = (
        f'<div class="notice" role="status"><span class="label">Note</span><p>{esc(notice)}</p></div>'
        if notice
        else ""
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{esc(title)} · Emend</title>"
        '<link rel="icon" href="/assets/favicon.svg" type="image/svg+xml">'
        f'<link rel="stylesheet" href="/assets/workspace.css?v={asset_key}">'
        f'<script src="/assets/workspace.js?v={asset_key}" defer></script>{refresh}'
        f'</head><body class="{esc(kind)}"{poll}><a class="skip" href="#main">Skip to content</a>'
        f'<header class="bar"><div class="frame"><a class="mark" href="/">{MARK}<span>Emend</span></a>'
        '<span class="label">Local workspace · this computer only</span></div></header>'
        f'<main id="main"><div class="frame">{note}{body}</div></main>'
        '<footer class="colophon"><div class="frame"><p>Originals are kept byte for byte. '
        "Every change is made to a copy and checked before it is used.</p>"
        f'<p class="label">Emend {esc(__version__)}</p></div></footer></body></html>'
    )


def error_page(title, message, back="/", back_label="Back to the shelf"):
    return page(
        title,
        f'<section class="error-page">{label("Something went wrong", "accent")}'
        f"<h1>{esc(title)}</h1><p>{esc(message)}</p>"
        f'<p><a class="btn" href="{esc(back)}">{esc(back_label)}</a></p></section>',
    )


# Library --------------------------------------------------------------------


def library(app, offset=0, error=None):
    from emend.pipeline import get_report

    with app.store.db() as db:
        rows = db.execute(
            "SELECT * FROM books ORDER BY updated_at DESC LIMIT 101 OFFSET ?", (offset,)
        ).fetchall()
    known = {row["id"] for row in rows}
    items = []
    active = app.runner.active
    if active and active.book not in known and active.kind == "import":
        items.append(
            f'<li><a class="shelf-row" href="/books/{esc(active.book)}"><span class="shelf-title">'
            f'{esc(active.title)}</span><span class="shelf-author quiet">Arriving</span>'
            f'<span></span>{tag("running", "Working")}<time class="label"></time></a></li>'
        )
    for row in rows[:100]:
        author, spine = "", []
        try:
            if not row["report_path"] or Path(row["report_path"]).stat().st_size > 8 * 1024**2:
                raise ValueError("Report exceeds browser review limit.")
            report = get_report(app.store, row["id"])
            title = "; ".join(report.inspection.metadata.get("title", [])) or row["source_name"]
            author = "; ".join(report.inspection.metadata.get("creator", []))
            spine = report.inspection.spine
        except (ValueError, OSError):
            title = row["source_name"]
        run = app.runner.get(row["id"])
        if run and run.active:
            state = tag("running", "Working")
        elif row["state"] == "PUBLISHED":
            state = tag("done", "Sent")
        elif row["state"] == "VALIDATED":
            state = tag("ready", "Ready")
        elif row["state"] in {"FAILED", "REVIEW_REQUIRED"}:
            state = tag("attention", "Needs you")
        else:
            state = tag("waiting", "Unfinished")
        items.append(
            f'<li><a class="shelf-row" href="/books/{row["id"]}">'
            f'<span class="shelf-title">{esc(title[:2000])}</span>'
            f'<span class="shelf-author">{esc(author[:500])}</span>'
            f"{spine_svg(spine, name='Chapters') if spine else '<span></span>'}{state}"
            f'<time class="label" datetime="{esc(row["updated_at"])}">{esc(nice_day(row["updated_at"]))}</time></a></li>'
        )
    limit = min(app.config.limits.max_archive_mb, app.upload_limit)
    busy = " disabled" if active else ""
    zone = (
        f'<span class="drop-title">Working on {esc(active.title)}…</span>'
        '<span class="drop-hint">The next book can come in when it finishes.</span>'
        if active
        else '<span class="drop-title">Choose an EPUB</span><span class="drop-hint">or drop it here</span>'
    )
    upload = (
        '<form class="drop" method="post" action="/action" enctype="multipart/form-data">'
        f'<input type="hidden" name="csrf" value="{app.csrf}"><input type="hidden" name="action" value="import">'
        f'<label class="drop-zone{" is-busy" if active else ""}">{BOOK_ICON}'
        f'<input type="file" name="epub" accept=".epub,application/epub+zip" required data-autosubmit{busy}>'
        f'{zone}<span class="label">EPUB · up to {limit} MiB</span></label>'
        f'<button class="btn primary" type="submit" data-working="Importing…"{busy}>Import and check</button>'
        f'<p class="quiet">Up to {limit} MiB here; use the inbox or CLI for larger files. '
        "A book already on the shelf opens unchanged.</p></form>"
    )
    paging = ""
    if offset or len(rows) > 100:
        paging = (
            '<nav class="paging" aria-label="More books">'
            + (
                f'<a href="/?offset={max(0, offset - 100)}">← Newer books</a>'
                if offset
                else "<span></span>"
            )
            + (f'<a href="/?offset={offset + 100}">Older books →</a>' if len(rows) > 100 else "")
            + "</nav>"
        )
    shelf = (
        f'<ol class="shelf-list">{"".join(items)}</ol>{paging}'
        if items
        else '<div class="empty"><p>Nothing on the shelf yet. The first book you bring in will appear here.</p></div>'
    )
    count = len(rows[:100]) + (1 if items and len(items) > len(rows[:100]) else 0)
    backup = ""
    if app.config.publish.calibre_library:
        fields = (
            '<label class="check"><input type="checkbox" name="closed" value="yes" required>'
            "<span>Calibre and anything else that writes to the library are closed.</span></label>"
        )
        if app.config.publish.backup_remote:
            fields += (
                '<label class="check"><input type="checkbox" name="upload" value="yes"><span>'
                "Also upload the verified snapshot to the configured Drive remote.</span></label>"
            )
        backup = (
            '<details class="fold library-tools"><summary>Back up the Calibre library'
            '<span class="label">Whole library</span></summary><div class="fold-body">'
            "<p>Copies the entire library: book formats, covers, metadata, custom columns and notes. "
            "Calibre and its Content Server must be closed for the snapshot. Importing, checking and "
            "downloading here do not need that.</p>"
            + app.form("backup", "Create verified backup", fields, working="Backing up…")
            + "</div></details>"
        )
    alert = (
        f'<div class="alert" role="alert">{label("That could not start")}<h2>{esc(error)}</h2></div>'
        if error
        else ""
    )
    body = alert + (
        '<section class="intake"><div>'
        + label("Intake")
        + '<h1 class="display">Bring a book in.</h1><p class="lede">Emend keeps the original untouched, '
        "checks it against the EPUB standard, repairs what it can verify, and renders reader pages "
        "for you to look through. Adjusting spacing comes afterwards, only if you want it.</p></div>"
        + upload
        + "</section>"
        + f'<section aria-labelledby="shelf"><header class="section-head"><h2 id="shelf">Shelf</h2>'
        f'<span class="label">{plural(count, "book")}</span></header>{shelf}</section>' + backup
    )
    return page(
        "Shelf",
        body,
        notice=app.take_notice(),
        status="/status" if active else None,
        version=app.runner.version,
        kind="library",
    )


# Book -----------------------------------------------------------------------


class BookView:
    """Everything one book page shows, gathered once from saved state."""

    def __init__(self, app, row, source, digest, error=None, step=None, opened=""):
        self.app = app
        self.config = app.config
        self.store = app.store
        self.row = row
        self.book = row["id"]
        self.source = source
        self.digest = digest
        self.error = error
        self.opened = opened
        self.report = get_report(self.store, self.book)
        meta = self.report.inspection.metadata
        self.title = "; ".join(meta.get("title", [])) or row["source_name"]
        self.author = "; ".join(meta.get("creator", []))
        self.problems = blockers(row, self.report, digest, self.config)
        self.ready = not self.problems
        self.missing_tool = any(g.required and g.status == "unavailable" for g in self.report.gates)
        self.reviews = current_reviews(self.store, self.config, row, digest)
        self.applied = {
            "typography-trial": {c.trial_id for c in self.report.typography_changes},
            "repairs": {c.plan_id for c in self.report.repair_changes},
            "cover-review": {
                c.review_id for c in [*self.report.cover_changes, *self.report.cover_additions]
            },
        }
        self.run = app.runner.get(self.book)
        self.messages, self.message_note = (
            epubcheck_messages(self.report, self.config.root / "reports" / self.book)
            if any(name == "epubcheck" for name, _, _ in self.problems)
            else ([], None)
        )
        self.rejected = self.rejected_details()
        self.flagged = self.flagged_documents()
        visual = self.current("visual-review")
        self.seen = set(visual.selected_documents) if visual else set()
        self.stations = self.station_states()
        self.step = step if step in STATION_KEYS else self.default_step()

    # Saved evidence ---------------------------------------------------------

    def current(self, kind, complete=True):
        entry = self.reviews.get(kind)
        if not entry or entry[0] is None or not entry[1]:
            return None
        evidence = entry[0]
        if complete and getattr(evidence, "status", "complete") != "complete":
            return None
        return evidence

    def latest(self, kind):
        entry = self.reviews.get(kind)
        return entry if entry else (None, False, None)

    def run_step(self, key):
        """The last run's record for a step, if this server ran one for the book."""
        if not self.run:
            return None
        return next((s for s in self.run.steps if s.key == key), None)

    def rejected_repair(self):
        with self.store.db() as db:
            event = db.execute(
                "SELECT at, detail FROM events WHERE book_id=? AND state='REPAIR_REJECTED' "
                "ORDER BY id DESC LIMIT 1",
                (self.book,),
            ).fetchone()
        if event and event["at"] >= self.report.created_at:
            return event["detail"]
        return None

    def rejected_details(self):
        """The rejected candidate's own validator errors: what blocked the repair."""
        import re

        from emend.models import Report

        detail = self.rejected_repair()
        found = re.search(r"Evidence: (.+?report\.json)", detail or "")
        if not found:
            return None
        root = self.config.root / "reports" / self.book
        try:
            path = Path(found.group(1)).resolve()
            if not path.is_relative_to(root.resolve()) or path.stat().st_size > 8 * 1024**2:
                return None
            candidate = Report.model_validate_json(path.read_text())
            if candidate.book_id != self.book or not candidate.repair_changes:
                return None
            messages, _ = epubcheck_messages(candidate, root)
        except (ValueError, OSError):
            return None
        return FIX_NAMES.get(candidate.repair_changes[-1].action, "A supported repair"), messages

    def repair_attempted(self):
        plan = self.latest("repairs")
        scanned = plan[0] is not None and plan[1] and not plan[0].actions
        step = self.run_step("repair")
        return bool(
            scanned
            or self.rejected_repair()
            or (step and step.state in {"failed", "done"} and not self.ready)
        )

    def flagged_documents(self):
        spine = self.report.inspection.spine
        found = set()
        wheres = [
            f.location or "" for f in self.report.inspection.findings if f.severity == "error"
        ]
        wheres += [where for _, where in self.messages]
        if self.rejected:
            wheres += [where for _, where in self.rejected[1]]
        gate = next((g for g in self.report.gates if g.name == "accessibility"), None)
        if gate and gate.accessibility and gate.status == "failed":
            wheres += [f.document or "" for f in gate.accessibility.findings]
        for where in wheres:
            for part in where.split(";"):
                if document := matches_document(part, spine):
                    found.add(document)
        return found

    # Stations ---------------------------------------------------------------

    def station_states(self):
        report = self.report
        problems = {name for name, _, _ in self.problems}
        states = {"import": ("done", "Kept")}
        if self.missing_tool:
            states["check"] = ("attention", "Checker missing")
        elif problems & {"structure", "preservation", "epubcheck", "accessibility"}:
            count = len(self.messages) or sum(
                f.severity == "error" for f in report.inspection.findings
            )
            states["check"] = (
                "attention",
                plural(count, "problem") if count else "Needs you",
            )
        elif problems:
            states["check"] = ("attention", "Out of date")
        else:
            states["check"] = ("done", "Passed")
        if report.repair_changes and self.ready:
            states["repair"] = ("done", plural(len(report.repair_changes), "repair") + " applied")
        elif self.ready:
            states["repair"] = ("done", "None needed")
        elif self.missing_tool:
            states["repair"] = ("waiting", "Waits for checker")
        elif self.repair_attempted():
            states["repair"] = ("attention", "No safe repair")
        else:
            states["repair"] = ("waiting", "Not tried")
        visual = self.latest("visual-review")
        step = self.run_step("preview")
        if report.inspection.blocking:
            states["preview"] = ("skipped", "Waits on check")
        elif visual[0] is not None and visual[1]:
            pages = sum(len(s.screenshots) for s in visual[0].samples)
            states["preview"] = (
                ("done", plural(pages, "page"))
                if visual[0].status == "complete"
                else ("attention", "Incomplete")
            )
        elif step and step.state == "failed":
            states["preview"] = ("attention", "Could not render")
        else:
            states["preview"] = ("waiting", "Not rendered")
        trial = self.latest("typography-trial")
        if report.typography_changes:
            states["refine"] = ("done", "Spacing adjusted")
        elif trial[0] is not None and trial[1] and trial[0].status == "complete":
            states["refine"] = ("attention", "Comparison to review")
        else:
            states["refine"] = ("optional", "Optional")
        if self.ready and self.row["state"] == "PUBLISHED":
            states["send"] = ("done", "Sent")
        elif self.ready:
            states["send"] = ("ready", "Ready")
        else:
            states["send"] = ("waiting", "Not yet")
        if self.run and self.run.active:
            for run_step in self.run.steps:
                station = RUN_STATIONS.get(run_step.key)
                if self.run.kind == "recheck" and run_step.key == "keep":
                    station = "check"
                if not station:
                    continue
                if run_step.state == "running":
                    states[station] = ("running", "Working")
                elif run_step.state == "waiting" and states[station][0] != "running":
                    states[station] = ("waiting", "Queued")
        return states

    def default_step(self):
        if self.run and self.run.active:
            for key in STATION_KEYS:
                if self.stations[key][0] == "running":
                    return key
        for key in ("check", "repair"):
            if self.stations[key][0] == "attention":
                return key
        if self.ready:
            if self.stations["refine"][0] == "attention":
                return "refine"
            return "preview"
        return "check"

    # Helpers ----------------------------------------------------------------

    def form(self, action, text, fields="", hidden=None, disabled=False, **options):
        return self.app.form(
            action,
            text,
            fields,
            book=self.book,
            digest=self.digest,
            hidden=hidden,
            disabled=disabled,
            **options,
        )

    def evidence_url(self, kind, identifier):
        return f"/evidence/{self.book}/{kind}/{identifier}"

    def image_url(self, kind, identifier, phase, name):
        return f"/books/{self.book}/pages/{kind}/{identifier}/{phase}/{name}"

    def step_url(self, key):
        return f"/books/{self.book}?step={key}"

    def rerun_form(self, text="Run everything again", primary=False):
        return self.form(
            "revalidate",
            text,
            cls="btn primary" if primary else "btn",
            working="Starting…",
        )

    # Page -------------------------------------------------------------------

    def render(self):
        notice = self.app.take_notice(self.book)
        body = self.header() + self.strip()
        if self.run and self.run.active:
            if self.error:
                body += (
                    f'<div class="alert" role="alert">{label("That step could not finish")}'
                    f"<h2>{esc(self.error)}</h2></div>"
                )
            body += self.run_panel()
        else:
            body += self.panel()
        active = self.app.runner.active
        return page(
            self.title,
            body,
            notice=notice,
            status="/status" if active else None,
            version=self.app.runner.version,
            kind="book",
        )

    def header(self):
        meta = [f"<span>{esc(self.author)}</span>"] if self.author else []
        if self.report.inspection.epub_version:
            meta.append(f"<span>EPUB {esc(self.report.inspection.epub_version)}</span>")
        spine = self.report.inspection.spine
        if spine:
            meta.append(f"<span>{plural(len(spine), 'document')} in reading order</span>")
        if self.run and self.run.active:
            cta = tag("running", "Working")
        elif self.ready:
            cta = (
                f'<a class="btn primary" href="/books/{self.book}/download">Download EPUB</a>'
                + label(f"Checked · {nice_time(self.report.created_at)}")
            )
        else:
            cta = f'<a class="btn" href="{self.step_url("check")}">See what went wrong</a>'
            cta += label("Not ready to send")
        keys = [
            '<span class="key">Document</span>',
            *(['<span class="key seen">Previewed</span>'] if self.seen else []),
            *(['<span class="key flag">Has problems</span>'] if self.flagged else []),
        ]
        figure = (
            f'<figure class="spine-figure">{spine_svg(spine, self.seen, self.flagged)}'
            f'<figcaption class="label">{"".join(keys)}</figcaption></figure>'
            if spine
            else '<div class="spine-figure"></div>'
        )
        return (
            f'<nav class="crumbs label" aria-label="Breadcrumb"><a href="/">Shelf</a><span>/</span>'
            f"<span>{esc(self.title[:120])}</span></nav>"
            f'<header class="book-head"><div><h1 class="display">{esc(self.title)}</h1>'
            f'<p class="byline">{"".join(meta)}</p></div><div class="book-cta">{cta}</div></header>'
            + figure
        )

    def strip(self):
        items = []
        for number, (key, name) in enumerate(STATIONS, 1):
            state, text = self.stations[key]
            current = (
                ' aria-current="step"'
                if key == self.step and not (self.run and self.run.active)
                else ""
            )
            items.append(
                f'<li><a class="station is-{state}" href="{self.step_url(key)}"{current}>'
                f'<span class="num label">{number:02d}</span><span class="station-name">{esc(name)}</span>'
                f"{tag(state, text)}</a></li>"
            )
        index = STATION_KEYS.index(self.step)
        previous = STATION_KEYS[index - 1] if index else None
        following = STATION_KEYS[index + 1] if index + 1 < len(STATION_KEYS) else None
        if self.run and self.run.active:
            summary = tag(
                "running",
                "Working — "
                + next((s.label for s in self.run.steps if s.state == "running"), "starting"),
            )
        elif self.ready:
            summary = tag("ready", "Checked and ready to send")
        else:
            summary = tag("attention", "Needs you before it can be sent")
        nav = (
            '<nav class="step-nav" aria-label="Steps">'
            + (
                f'<a class="btn square" href="{self.step_url(previous)}" aria-label="Previous step">‹</a>'
                if previous
                else ""
            )
            + (
                f'<a class="btn square" href="{self.step_url(following)}" aria-label="Next step">›</a>'
                if following
                else ""
            )
            + "</nav>"
        )
        return (
            f'<ol class="stations" aria-label="Book steps">{"".join(items)}</ol>'
            f'<div class="progress">{summary}<div class="progress-meta">{nav}</div></div>'
        )

    def run_panel(self):
        run = self.run
        done = sum(s.state in {"done", "skipped", "failed"} for s in run.steps)
        return (
            '<section class="panel" aria-live="polite"><header class="panel-head">'
            '<span class="label">Now</span><h2>Working on it</h2>'
            f"{tag('running', f'{done} of {len(run.steps)}')}</header>"
            '<div class="panel-body"><div class="split"><div>'
            f"{checklist(run.steps)}</div><div>"
            "<p>Each step works on a copy and checks it before it is used. "
            "You can leave this page; the work continues and the shelf shows its progress.</p>"
            '<p class="quiet small">This page updates by itself.</p></div></div></div></section>'
        )

    def panel(self):
        number = STATION_KEYS.index(self.step) + 1
        name = dict(STATIONS)[self.step]
        state, text = self.stations[self.step]
        content = getattr(self, f"panel_{self.step}")()
        error = ""
        if self.error:
            message = (
                "This book is not ready to send yet. What went wrong, and what you can do, is below."
                if str(self.error).startswith("Publication blocked:")
                else str(self.error)
            )
            error = (
                '<div class="alert" role="alert">'
                + label("That step could not finish")
                + f"<h2>{esc(message)}</h2>"
                + (
                    f"<p>{esc(self.diagnosis())}</p>"
                    + self.problem_list()
                    + f'<p><a href="{self.step_url("check")}">See checks and next steps</a></p>'
                    if not self.ready and self.step != "check"
                    else "<p>Nothing in the book changed. Fix the cause above, then try again.</p>"
                )
                + "</div>"
            )
        index = STATION_KEYS.index(self.step)
        following = STATION_KEYS[index + 1] if index + 1 < len(STATION_KEYS) else None
        foot = (
            f'<footer class="panel-foot"><span class="label">Step {number} of {len(STATIONS)}</span>'
            + (
                f'<a href="{self.step_url(following)}">Continue to {esc(dict(STATIONS)[following])} →</a>'
                if following
                else '<a href="/">Back to the shelf →</a>'
            )
            + "</footer>"
        )
        return (
            f'<section class="panel" id="step" aria-labelledby="step-title"><header class="panel-head">'
            f'<span class="label">{number:02d}</span><h2 id="step-title">{esc(name)}</h2>{tag(state, text)}</header>'
            f'<div class="panel-body">{error}{self.last_run_note()}{content}</div>{foot}</section>'
        )

    def last_run_note(self):
        if not self.run or self.run.active:
            return ""
        stations = {
            self.step,
            *(["import"] if self.step == "check" else []),
        }
        failed = [
            s
            for s in self.run.steps
            if s.state == "failed"
            and (
                RUN_STATIONS.get(s.key) in stations
                or (self.run.kind == "recheck" and s.key == "keep" and self.step == "check")
            )
            and s.key != "repair"
        ]
        if not failed:
            return ""
        items = "".join(f"<li><h4>{esc(s.label)}</h4><p>{esc(s.note)}</p></li>" for s in failed)
        return (
            '<div class="alert" role="status">'
            + label("The last run stopped here")
            + f'<ol class="problems">{items}</ol></div>'
        )

    # Panels -----------------------------------------------------------------

    def panel_import(self):
        report = self.report
        inspection = report.inspection
        original = self.book
        changes = len(report.changes)
        current = (
            "Same as the original"
            if self.digest == original
            else f'<code title="{esc(self.digest)}">{short(self.digest)}</code>'
        )
        steps = [
            ("done", "Kept the original", "A byte-for-byte copy that is never edited."),
            (
                "done" if not inspection.blocking else "attention",
                "Read the package",
                f"{plural(len(inspection.findings), 'finding')}; "
                f"{plural(sum(f.severity == 'error' for f in inspection.findings), 'blocking problem')}.",
            ),
            (
                "done" if self.ready else "attention",
                "Ran the required checks",
                "All passed." if self.ready else "Some need you; see Check.",
            ),
        ]
        rows = "".join(
            f'<li class="is-{s}">{ICONS[s]}<span>{esc(t)}</span><span></span><p>{esc(n)}</p></li>'
            for s, t, n in steps
        )
        left = (
            '<div class="block">'
            + label("What happened")
            + f'<ol class="checklist">{rows}</ol></div>'
            + '<div class="block">'
            + label("Earlier versions")
            + "<p>Every applied change keeps the version before it. You can return to the "
            "original or to an earlier checked version; the current one moves aside, not away.</p>"
            + f'<p><a class="btn" href="/recovery/{self.book}">Recover an earlier version</a></p></div>'
        )
        right = facts(
            [
                ("File", esc(self.row["source_name"])),
                (
                    "Format",
                    f"EPUB {esc(inspection.epub_version)}"
                    if inspection.epub_version
                    else "Unknown",
                ),
                ("Reading order", plural(len(inspection.spine), "document")),
                ("Text", f"{inspection.text_characters:,} characters"),
                ("Original", f'<code title="{esc(original)}">{short(original)}</code>'),
                ("Current", current),
                ("Changes", plural(changes, "recorded change") if changes else "None"),
            ]
        )
        return f'<div class="split"><div>{left}</div><div>{right}</div></div>'

    # What went wrong --------------------------------------------------------

    def diagnosis(self):
        names = {name for name, _, _ in self.problems}
        unavailable = [
            CHECK_NAMES.get(g.name, g.name)
            for g in self.report.gates
            if g.required and g.status == "unavailable"
        ]
        if unavailable:
            return f"{' and '.join(unavailable)} is not installed, so the book cannot be verified."
        if "structure" in names:
            if self.rejected and self.rejected[1]:
                return (
                    f"“{self.rejected[0]}” worked, but the repaired copy still has "
                    f"{plural(len(self.rejected[1]), 'EPUBCheck error')}."
                )
            count = len(
                {f.message for f in self.report.inspection.findings if f.severity == "error"}
            )
            return f"The EPUB file has {plural(count, 'structural problem')} that stop readers opening it reliably."
        if "epubcheck" in names:
            total = len(self.messages)
            documents = {where.split(":", 1)[0] for _, where in self.messages if where}
            if total:
                more = "at least " if self.message_note else ""
                return f"EPUBCheck found {more}{plural(total, 'error')}" + (
                    f" in {plural(len(documents), 'file')}." if documents else "."
                )
            return "EPUB validation did not pass."
        if "accessibility" in names:
            return "The required accessibility audit did not pass."
        if "preservation" in names:
            return "A file changed while it was being checked."
        return "The saved checks no longer describe this file."

    def repair_story(self):
        changes = self.report.repair_changes
        if self.missing_tool:
            return "Repairs wait until the checker is installed, because every repair must be validated."
        if changes:
            names = ", ".join(f"“{FIX_NAMES.get(c.action, c.action)}”" for c in changes)
            return f"Emend applied {names}. The problems below have no supported repair."
        if self.rejected and self.rejected[1]:
            return (
                f"Emend applied “{self.rejected[0]}” to a copy, then EPUBCheck found the errors "
                "below in that copy, so it kept the previous version. Once they are fixed, the "
                "repair applies on the next run."
            )
        if self.rejected_repair():
            return "Emend tried a supported repair, but the repaired copy still failed, so it kept the previous version."
        if self.repair_attempted():
            return "Emend looked for a supported repair; none applies to these problems."
        names = {name for name, _, _ in self.problems}
        if names <= {"outdated", "identity", "approval", "revalidation", "recovery"}:
            return "Run everything again to refresh the checks for this file."
        return "Run everything again to let Emend try its supported repairs."

    def problem_list(self):
        items = []
        for name, title, detail in self.problems:
            body = ""
            if name == "structure":
                shown = {}
                for finding in [
                    f for f in self.report.inspection.findings if f.severity == "error"
                ][:20]:
                    message = (
                        "The EPUB file needs a packaging repair before readers can reliably open it."
                        if finding.code in {"zip.mimetype_order", "zip.mimetype_header"}
                        else finding.message
                    )
                    shown.setdefault(message, finding.location)
                body = (
                    "<ul>"
                    + "".join(
                        f"<li>{esc(text)}{f'<code>{esc(where)}</code>' if where else ''}</li>"
                        for text, where in shown.items()
                    )
                    + "</ul>"
                    if shown
                    else f"<p>{esc(detail)}</p>"
                )
            elif name == "epubcheck":
                if self.messages:
                    body = (
                        "<ul>"
                        + "".join(
                            f"<li>{esc(text)}{f'<code>{esc(where)}</code>' if where else ''}</li>"
                            for text, where in self.messages
                        )
                        + "</ul>"
                    )
                elif any(
                    g.name == "epubcheck" and g.status == "skipped" for g in self.report.gates
                ):
                    body = "<p>This check runs once the EPUB structure problems are resolved.</p>"
                else:
                    body = f"<p>{esc(detail)}</p>"
                if self.message_note:
                    body += f'<p class="quiet small">{esc(self.message_note)}</p>'
            elif name == "accessibility":
                gate = next(g for g in self.report.gates if g.name == name)
                body = f"<p>{esc(detail)}</p>"
                if gate.accessibility:
                    body += (
                        "<ul>"
                        + "".join(
                            f"<li>{esc(f.message)}{f'<code>{esc(f.document)}</code>' if f.document else ''}</li>"
                            for f in gate.accessibility.findings[:20]
                        )
                        + "</ul>"
                    )
            elif name in {"revalidation", "recovery", "approval"}:
                body = "<p>The checks have not finished for this file. Run everything again to continue.</p>"
            else:
                body = f"<p>{esc(detail)}</p>"
            gate = next((g for g in self.report.gates if g.name == name), None)
            if gate and gate.status == "unavailable":
                body += f"<p>{esc(STATUS_NAMES['unavailable'])}: the setup steps are below.</p>"
            items.append(f"<li><h4>{esc(title)}</h4>{body}</li>")
        if self.rejected and self.rejected[1]:
            items.append(
                "<li><h4>Found in the repaired copy</h4><ul>"
                + "".join(
                    f"<li>{esc(text)}{f'<code>{esc(where)}</code>' if where else ''}</li>"
                    for text, where in self.rejected[1]
                )
                + "</ul></li>"
            )
        return f'<ol class="problems">{"".join(items)}</ol>'

    def remedies(self):
        names = {name for name, _, _ in self.problems}
        steps = []
        for gate in self.report.gates:
            if not (gate.required and gate.status == "unavailable"):
                continue
            if gate.name == "epubcheck":
                steps.append(
                    "<li>Install EPUBCheck on your Mac. With Homebrew:"
                    '<br><code class="command">brew install epubcheck</code>'
                    "<p>Or set <code>tools.epubcheck</code> in your Emend configuration.</p></li>"
                )
            elif gate.name == "accessibility":
                steps.append(
                    "<li>Install and configure Ace by DAISY, then set <code>tools.ace</code>."
                    "<p>Ace is only required when your policy requires the accessibility audit.</p></li>"
                )
        if steps:
            steps.append(
                '<li><a href="https://github.com/specbug/emend/blob/main/docs/mac-validation.md" '
                'target="_blank" rel="noopener noreferrer">Open the tool setup instructions</a>'
                "<p>Then run everything again. Emend checks and repairs the book from where it stopped.</p></li>"
            )
        elif names & {"structure", "epubcheck", "accessibility"} and self.repair_attempted():
            steps += [
                "<li>Open the book in Calibre’s <strong>Edit book</strong> and go to the files and lines "
                "listed above.<p>Calibre’s <strong>Tools → Check book</strong> runs different checks "
                "from EPUBCheck: “No problems found” there does not mean these errors are resolved.</p></li>",
                "<li>Fix the markup the error describes.<p>Invalid <code>lang</code> or "
                "<code>xml:lang</code> values need a language tag matching the text, such as "
                "<code>en</code>. For an element “not allowed here”, look at its surrounding tags: "
                "headings and lists cannot sit inside a paragraph.</p></li>",
                "<li>Save, export an EPUB copy outside your Calibre library, and bring that copy in "
                "here.<p>Emend keeps its own copy, so running again here does not pick up edits made "
                'in Calibre. <a href="https://github.com/specbug/emend/blob/main/docs/epubcheck-troubleshooting.md" '
                'target="_blank" rel="noopener noreferrer">How to read these errors</a>.</p></li>',
            ]
        else:
            steps.append(
                "<li>Run everything again.<p>Emend re-checks the current file and applies any "
                f"supported repair: {SUPPORTED_REPAIRS}.</p></li>"
            )
        return f'<ol class="remedy">{"".join(steps)}</ol>'

    def trouble(self):
        return (
            '<div class="trouble">'
            + label("What went wrong")
            + f"<h3>{esc(self.diagnosis())}</h3>"
            + f"<p>{esc(self.repair_story())} Your original is untouched.</p>"
            + self.problem_list()
            + label("What you can do")
            + self.remedies()
            + f'<div class="actions">{self.rerun_form(primary=True)}'
            + '<p class="note quiet small">Checks can take a minute. Emend updates this page as it goes.</p></div></div>'
        )

    def panel_check(self):
        gates = "".join(
            f"<div><dt>{esc(CHECK_NAMES.get(g.name, g.name))}"
            f"{'' if g.required else ' <span class=quiet>(advisory)</span>'}</dt>"
            f"<dd>{tag(gate_state(g), STATUS_NAMES.get(g.status, g.status))}</dd></div>"
            for g in self.report.gates
            if g.name != "visual_qa"
        )
        right = (
            label("Checks for this file")
            + f'<dl class="facts">{gates}</dl>'
            + f'<p class="quiet small">Checked {esc(nice_time(self.report.created_at))}.</p>'
        )
        if self.ready:
            left = (
                "<h3>Everything required passed.</h3>"
                "<p>The structure is sound, every resource matches its recorded hash, and EPUBCheck "
                "accepts the file. Passing validation says the file is well formed; the pages in "
                "Preview show how it reads.</p>"
                f'<div class="actions"><a class="btn primary" href="{self.step_url("preview")}">Look at the pages</a>'
                f"{self.rerun_form('Check again')}</div>"
            )
        else:
            left = self.trouble()
        return f'<div class="split"><div>{left}</div><div>{right}</div></div>'

    def panel_repair(self):
        changes = self.report.repair_changes
        left = (
            "<p>Emend repairs only what it can verify: "
            f"{SUPPORTED_REPAIRS}. Each repair is made on a copy, compared against the book, and "
            "validated before it is used. A repair that does not pass leaves the previous version in place.</p>"
        )
        if changes:
            rows = "".join(
                f'<li class="is-done">{ICONS["done"]}<span>{esc(FIX_NAMES.get(c.action, c.action))}</span>'
                f'<span></span><p><a href="{self.evidence_url("repairs", c.plan_id)}" target="_blank" '
                f'rel="noopener noreferrer">See exactly what changed</a> · '
                f"<code>{short(c.before_sha256)} → {short(c.after_sha256)}</code></p></li>"
                for c in changes
            )
            left += (
                '<div class="block">'
                + label("Applied")
                + f'<ol class="checklist">{rows}</ol></div>'
            )
        step = self.run_step("repair")
        if step and step.note and not (self.run and self.run.active):
            left += '<div class="block">' + label("Last run") + checklist([step]) + "</div>"
        if not self.ready:
            left += (
                '<div class="block">'
                + label("Still to resolve")
                + f"<h3>{esc(self.diagnosis())}</h3><p>{esc(self.repair_story())}</p>"
                + f'<div class="actions"><a class="btn primary" href="{self.step_url("check")}">See the problems and next steps</a>'
                + f"{self.rerun_form()}</div></div>"
            )
        elif not changes:
            left += (
                "<h3>Nothing needed repair.</h3><p>The book passed its checks as it arrived.</p>"
            )
        plan = self.latest("repairs")
        scan = (
            "No supported repair for this file"
            if plan[0] is not None and plan[1] and not plan[0].actions
            else "Repair available"
            if plan[0] is not None and plan[1]
            else "Not needed"
            if self.ready
            else "Not scanned"
        )
        right = facts(
            [
                ("Applied", plural(len(changes), "repair") if changes else "None"),
                ("Latest scan", esc(scan)),
                (
                    "Plan",
                    f'<a href="{self.evidence_url("repairs", plan[0].id)}" target="_blank" rel="noopener noreferrer">Open the repair report</a>'
                    if plan[0] is not None
                    else "",
                ),
            ]
        )
        return f'<div class="split"><div>{left}</div><div>{right}</div></div>'

    def pages_grid(self, review):
        figures = []
        for sample in review.samples:
            for shot in sample.screenshots:
                position = (
                    f"p. {shot.page_number} of {sample.page_count}"
                    if shot.page_number is not None
                    else f"{shot.scroll_y:,} px down"
                )
                url = self.image_url("visual-review", review.id, "pages", shot.file)
                name = sample.document.rsplit("/", 1)[-1]
                figures.append(
                    f'<figure><a class="page-link" href="{url}" target="_blank" rel="noopener noreferrer">'
                    f'<img src="{url}" width="{sample.width}" height="{sample.height}" loading="lazy" '
                    f'alt="{esc(sample.document)}, {esc(position)}"></a>'
                    f'<figcaption class="label"><span>{esc(name)}</span><span>{esc(position)}</span></figcaption></figure>'
                )
        return f'<div class="pages">{"".join(figures)}</div>' if figures else ""

    def panel_preview(self):
        if self.report.inspection.blocking:
            return (
                '<div class="split"><div><h3>Pages can be rendered once the file is readable.</h3>'
                "<p>The EPUB structure has problems that stop a reader from opening it. "
                "Emend renders pages straight after they are resolved.</p>"
                f'<div class="actions"><a class="btn primary" href="{self.step_url("check")}">See what went wrong</a></div>'
                "</div><div></div></div>"
            )
        visual, current, problem = self.latest("visual-review")
        render = self.form(
            "visual",
            "Render again" if visual else "Render pages",
            '<label class="field"><span>Layout</span><select name="layout">'
            '<option value="paginated">Reader pages</option>'
            '<option value="scroll">Scrolling document</option></select></label>',
            cls="btn",
            form_class="inline-form",
            working="Rendering…",
        )
        body = ""
        if problem:
            body += f'<div class="alert">{label("The saved preview could not be opened")}<p>{esc(problem)}</p></div>'
        if visual is not None and current:
            pages = sum(len(s.screenshots) for s in visual.samples)
            body += (
                '<div class="preview-head"><p class="label">'
                f"{esc('Reader pages' if visual.layout == 'paginated' else 'Scrolling document')} · "
                f"{plural(pages, 'page')} from {plural(len(visual.selected_documents), 'document')} · "
                f"{viewport(visual)}</p></div>" + self.pages_grid(visual)
            )
            notes = [
                f"{s.document.rsplit('/', 1)[-1]}: {note}"
                for s in visual.samples
                for note in [*s.observations, *([s.error] if s.error else [])]
            ]
            body += (
                '<div class="block">'
                + label("Things to look at" if notes else "Rendering notes")
                + (
                    '<ul class="observations">'
                    + "".join(f"<li>{esc(n)}</li>" for n in notes[:30])
                    + "</ul>"
                    if notes
                    else "<p class='quiet'>No layout warnings in these pages. Look through them anyway; "
                    "a quiet report does not certify readability.</p>"
                )
                + "</div>"
            )
            body += (
                '<details class="fold"><summary>What this preview covers</summary><div class="fold-body">'
                '<ul class="observations">'
                + "".join(f"<li>{esc(n)}</li>" for n in visual.notes)
                + "</ul>"
                + f'<p class="quiet small">Rendered {esc(nice_time(visual.created_at))} with Chromium {esc(visual.browser_version)}.</p>'
                + "</div></details>"
            )
            body += (
                '<details class="fold"><summary>Render again<span class="label">Reader pages or scrolling</span>'
                f'</summary><div class="fold-body">{render}</div></details>'
            )
        else:
            stale = visual is not None and not current
            body += (
                '<div class="split"><div>'
                + (
                    "<h3>The book changed since these pages were rendered.</h3><p>Render them again to see the current version.</p>"
                    if stale
                    else "<h3>No pages rendered yet.</h3><p>Emend renders a few pages from the start, "
                    "middle and end of the book at a reader-sized width.</p>"
                )
                + f'<div class="actions">{render}</div></div><div></div></div>'
            )
        body += self.cover_fold() + self.reading_fold()
        return body

    def cover_fold(self):
        info = cover_observation(self.source, self.report.inspection)
        warnings = (
            '<ul class="observations">'
            + "".join(f"<li>{esc(w)}</li>" for w in info["warnings"])
            + "</ul>"
            if info["warnings"]
            else ""
        )
        return (
            f'<details class="fold"><summary>Cover<span class="label">{esc(info["title"])}</span></summary>'
            f'<div class="fold-body"><h3>{esc(info["title"])}</h3><p>{esc(info["detail"])}</p>{warnings}'
            f'<p><a href="{self.step_url("refine")}&amp;open=details#details">Review or change the cover</a></p>'
            "<h3>Cover missing on CrossPoint?</h3><p>CrossPoint 2.0.0-rc makes its own cover and thumbnail "
            "BMP caches from the EPUB. Emend exports the EPUB, without device-cache sidecars.</p>"
            "<p>For sleep, choose Settings → Display → Sleep Screen → Cover, then open the book and put the "
            "reader to sleep. A Calibre library thumbnail alone does not prove the EPUB contains its cover.</p>"
            "<p>If a valid embedded cover still does not appear, try the EPUB under a new filename to use a "
            "fresh device cache while keeping the old copy and its reading data. This is a diagnostic step, "
            "not a device-compatibility guarantee.</p></div></details>"
        )

    def reading_fold(self):
        review, current, problem = self.latest("reading-review")
        if problem:
            content = f"<p class='quiet'>The saved report could not be opened: {esc(problem)}</p>"
        elif review is not None and current:
            content = (
                f"<p>{plural(len(review.fonts), 'font resource')} and "
                f"{plural(len(review.notes), 'marked note link')}"
                f"{'' if review.complete else '; some files exceeded the review limits'}. "
                "This lists what the book declares; it does not certify how a reader draws the fonts "
                "or handles notes.</p>"
                f'<p><a href="{self.evidence_url("reading-review", review.id)}" target="_blank" '
                'rel="noopener noreferrer">Open the font and footnote report</a></p>'
            )
        else:
            content = "<p class='quiet'>No report for this version yet.</p>"
        return (
            '<details class="fold"><summary>Fonts and footnotes</summary><div class="fold-body">'
            + content
            + self.form("reading", "List fonts and footnotes", cls="btn", working="Reading…")
            + "</div></details>"
        )

    def panel_refine(self):
        body = (
            '<div class="split"><div><p>Optional. The checked book is your baseline. Choose a spacing, '
            "compare it with the baseline page by page, and apply it only if it reads better. "
            "Publisher fonts, alignment and indentation stay as they are.</p></div><div>"
            + facts(
                [
                    (
                        "Baseline",
                        f'<code title="{esc(self.report.typography_changes[0].before_sha256)}">'
                        f"{short(self.report.typography_changes[0].before_sha256)}</code>"
                        if self.report.typography_changes
                        else "This version",
                    ),
                    (
                        "Adjusted",
                        f"Applied {esc(nice_time(self.report.created_at))}"
                        if self.report.typography_changes
                        else "Not yet",
                    ),
                ]
            )
            + "</div></div>"
        )
        body += '<div class="block">' + label("Spacing") + self.spacing_form() + "</div>"
        body += self.comparison()
        body += '<div class="block">' + self.details_fold() + "</div>"
        return body

    def spacing_form(self):
        choices = []
        options = [
            ("compact", "Compact", "Tighter lines, more per page"),
            ("comfortable", "Comfortable", "A balanced default"),
            ("roomy", "Roomy", "Open lines and paragraphs"),
            ("custom", "Custom", "Set exact values"),
        ]
        for value, name, hint in options:
            numbers = SPACING_PRESETS.get(value)
            detail = f"Line {numbers[0]:g} · gap {numbers[1]:g} em" if numbers else hint
            choices.append(
                f'<label class="choice"><input type="radio" name="spacing_preset" value="{value}"'
                f"{' checked' if value == 'comfortable' else ''}>"
                f'<span class="lines {value}" aria-hidden="true">{"<i></i>" * 6}</span>'
                f'<span class="choice-name">{name}</span><span class="label">{esc(detail)}</span></label>'
            )
        fields = (
            '<fieldset><legend class="sr">Spacing</legend>'
            f'<div class="choices">{"".join(choices)}</div>'
            '<div class="custom-values"><label class="field"><span>Line height (multiple of text size)</span>'
            '<input type="number" name="line_height" min="1" max="2.4" step="0.05" placeholder="1.5"></label>'
            '<label class="field"><span>Space after paragraphs (em)</span>'
            '<input type="number" name="paragraph_gap" min="0" max="2" step="0.05" placeholder="0.5"></label></div>'
            "</fieldset>"
            '<input type="hidden" name="layout" value="paginated">'
        )
        return self.form(
            "typography-trial",
            "Compare with the baseline",
            fields,
            cls="btn primary",
            form_class="spacing-form",
            working="Rendering both…",
        ) + (
            '<p class="quiet small">Rendering both versions takes a little while. Nothing in the book changes until you apply it.</p>'
        )

    def comparison(self):
        trial, current, problem = self.latest("typography-trial")
        if problem:
            return f'<div class="alert">{label("The saved comparison could not be opened")}<p>{esc(problem)}</p></div>'
        if trial is None:
            return ""
        applied = trial.id in self.applied["typography-trial"]
        if not current and not applied:
            return (
                '<div class="block">'
                + label("Earlier comparison")
                + "<p>The book changed after this comparison was made, so it can no longer be applied. "
                "Compare again to see the current version.</p></div>"
            )
        values = []
        if trial.settings.line_height is not None:
            values.append(f"Line height {trial.settings.line_height:g}")
        if trial.settings.paragraph_gap is not None:
            values.append(f"Paragraph gap {trial.settings.paragraph_gap:g} em")
        pairs = []
        for document in trial.selected_documents:
            for profile in trial.profiles:
                before = next(
                    (s for s in trial.before if s.document == document and s.profile == profile),
                    None,
                )
                after = next(
                    (s for s in trial.after if s.document == document and s.profile == profile),
                    None,
                )
                shots_before = before.screenshots if before else []
                shots_after = after.screenshots if after else []
                for index in range(max(len(shots_before), len(shots_after))):
                    cells = []
                    for phase, sample, shots, cls, name in (
                        ("before", before, shots_before, "is-base", "Baseline"),
                        ("after", after, shots_after, "is-next", "Adjusted"),
                    ):
                        if index >= len(shots):
                            cells.append(
                                f'<figure class="{cls}"><p class="quiet small">No page here.</p></figure>'
                            )
                            continue
                        shot = shots[index]
                        position = (
                            f"p. {shot.page_number} of {sample.page_count}"
                            if shot.page_number is not None
                            else f"{shot.scroll_y:,} px down"
                        )
                        url = self.image_url("typography-trial", trial.id, phase, shot.file)
                        cells.append(
                            f'<figure class="{cls}"><a class="page-link" href="{url}" target="_blank" rel="noopener noreferrer">'
                            f'<img src="{url}" width="{sample.width}" height="{sample.height}" loading="lazy" '
                            f'alt="{name}: {esc(document)}, {esc(position)}"></a><figcaption class="label">'
                            f'<span class="{"is-next-label" if phase == "after" else ""}">{name}</span><span>{esc(position)}</span></figcaption></figure>'
                        )
                    pairs.append(
                        (
                            index,
                            f'<div class="pair"><p class="label pair-title">{esc(document.rsplit("/", 1)[-1])}</p>{"".join(cells)}</div>',
                        )
                    )
        # First pages of every document come first; later pages wait in a fold.
        ordered = [markup for _, markup in sorted(pairs, key=lambda item: item[0])]
        shown, hidden = ordered[:3], ordered[3:]
        rest = (
            f'<details class="fold more-pairs"><summary>{plural(len(hidden), "more page pair")}</summary>'
            f'<div class="pairs-rest">{"".join(hidden)}</div></details>'
            if hidden
            else ""
        )
        status = (
            tag("done", "Applied to the book")
            if applied
            else tag("attention", "Not applied yet")
            if trial.status == "complete"
            else tag("failed", "Incomplete")
        )
        body = (
            '<div class="block">'
            + label("Baseline and adjusted" if not applied else "Baseline and your adjustment")
            + f'<div class="values">{status}'
            + "".join(f'<span class="label">{esc(v)}</span>' for v in values)
            + "</div>"
            + '<div class="compare"><input type="radio" name="cmp" id="cmp-side" checked>'
            '<input type="radio" name="cmp" id="cmp-base"><input type="radio" name="cmp" id="cmp-next">'
            '<div class="seg" role="group" aria-label="Show"><label for="cmp-side">Side by side</label>'
            '<label for="cmp-base">Baseline</label><label for="cmp-next">Adjusted</label></div>'
            f'<div class="pairs">{"".join(shown)}{rest}</div></div>'
        )
        if trial.status != "complete":
            body += "<p>Some pages could not be rendered, so this comparison cannot be applied. Compare again.</p>"
        elif not applied:
            body += (
                '<div class="actions">'
                + self.form(
                    "apply-typography",
                    "Apply this spacing",
                    '<label class="check"><input type="checkbox" name="all_spine" value="yes" checked>'
                    "<span>Apply to every chapter<small>The comparison shows "
                    f"{plural(len(trial.selected_documents), 'document')}. Unchecked, only those change.</small></span></label>",
                    {"trial": trial.id},
                    disabled=not self.ready,
                    cls="btn primary",
                    working="Applying…",
                )
                + (
                    ""
                    if self.ready
                    else '<p class="note quiet small">Spacing can be applied once the required checks pass.</p>'
                )
                + "</div>"
            )
        else:
            body += self.baseline_restore()
        body += (
            f'<p class="quiet small">Both sides come from the same file and the same browser; only the '
            f'spacing rules differ. <a href="{self.evidence_url("typography-trial", trial.id)}" target="_blank" '
            'rel="noopener noreferrer">Open the full comparison report</a>.</p></div>'
        )
        return body

    def baseline_restore(self):
        from emend.recovery import recovery_options

        change = self.report.typography_changes[-1]
        try:
            options = {o["sha256"] for o in recovery_options(self.book, self.config)}
        except (ValueError, OSError):
            options = set()
        if change.before_sha256 not in options:
            return ""
        return (
            '<div class="actions">'
            + self.form(
                "restore-baseline",
                "Return to the baseline",
                hidden={
                    "revision": change.before_sha256,
                    "expected_report": self.row["report_path"] or "",
                },
                cls="btn",
                working="Restoring…",
            )
            + '<p class="note quiet small">Restores the version before this spacing and checks it again. '
            "The adjusted version is kept aside.</p></div>"
        )

    def details_fold(self):
        opened = self.opened == "details"
        return (
            f'<details class="fold" id="details"{" open" if opened else ""}><summary>Book details and cover'
            '<span class="label">Uses public catalogues</span></summary><div class="fold-body">'
            + self.details()
            + "</div></details>"
        )

    def details(self):
        return details_section(self)

    def panel_send(self):
        ready = self.ready
        rows = []
        download = (
            f'<a class="btn primary" href="/books/{self.book}/download">Download EPUB</a>'
            if ready
            else '<button class="btn primary" type="button" disabled>Download EPUB</button>'
        )
        rows.append(
            "<li><div><h3>Download the EPUB</h3><p>The checked file, for CrossPoint’s file-transfer "
            "page or anywhere else. Calibre can stay open.</p></div>" + download + "</li>"
        )
        for target, name, detail, configured in [
            (
                "calibre",
                "Add to Calibre",
                "Imports directly with calibredb. Close Calibre first; you can keep it open while reviewing or downloading.",
                self.config.publish.calibre_library,
            ),
            (
                "drive",
                "Save EPUB to Drive",
                "Copies the checked file to your configured Drive folder.",
                self.config.publish.drive_remote,
            ),
        ]:
            if configured:
                rows.append(
                    f"<li><div><h3>{esc(name)}</h3><p>{esc(detail)}</p></div>"
                    + self.form(
                        "publish",
                        name,
                        hidden={"target": target},
                        disabled=not ready,
                        cls="btn",
                        working="Sending…",
                    )
                    + "</li>"
                )
        if self.app.reader_directory:
            rows.append(
                f"<li><div><h3>Copy to reader</h3><p>Into <code>{esc(self.app.reader_directory)}</code>. "
                "Confirm the reader is mounted, then eject it safely afterwards. Existing files are never replaced.</p></div>"
                + self.form(
                    "reader-export",
                    "Copy to reader",
                    '<label class="field"><span>Filename, optional</span><input name="filename" maxlength="240"></label>',
                    disabled=not ready,
                    cls="btn",
                    working="Copying…",
                )
                + "</li>"
            )
        intro = (
            "<p>This file passed the required checks. Every route below re-checks it before it leaves.</p>"
            if ready
            else f'<div class="alert" role="status">{label("Not ready")}<p>Sending opens once the '
            f'required checks pass. <a href="{self.step_url("check")}">See what went wrong</a>.</p></div>'
        )
        body = intro + f'<ul class="deliver">{"".join(rows)}</ul>'
        with self.store.db() as db:
            receipts = db.execute(
                "SELECT status,receipt,updated_at FROM publications WHERE book_id=? ORDER BY updated_at DESC LIMIT 5",
                (self.book,),
            ).fetchall()
        if receipts:
            body += (
                '<details class="fold"><summary>Recent transfers</summary><div class="fold-body"><ul class="receipts">'
                + "".join(
                    f"<li><details><summary>{esc(r['status'].title())} · {esc(nice_time(r['updated_at']))}</summary>"
                    f"<pre>{esc(r['receipt'][:4000])}</pre></details></li>"
                    for r in receipts
                )
                + "</ul></div></details>"
            )
        body += self.technical()
        return body

    def technical(self):
        gates = "".join(
            f"<li><strong>{esc(CHECK_NAMES.get(g.name, g.name))}</strong>: "
            f"{esc(STATUS_NAMES[g.status])} ({'required' if g.required else 'advisory'})"
            f"<p class='quiet small'>{esc(g.detail)}</p></li>"
            for g in self.report.gates
        )
        changes = "".join(f"<li>{esc(c)}</li>" for c in self.report.changes[:100])
        return (
            '<details class="fold" id="technical"><summary>Technical details and recovery</summary>'
            '<div class="fold-body">'
            + facts(
                [
                    ("Recorded state", esc(self.row["state"])),
                    ("Book ID", f"<code>{self.book}</code>"),
                    ("Current file", f"<code>{esc(self.digest)}</code>"),
                    ("Engine", esc(f"{__version__} · engine {ENGINE_VERSION}")),
                ]
            )
            + f'<ul class="observations">{gates}</ul>'
            + (
                f'<h3>Recorded changes</h3><ul class="observations">{changes}</ul>'
                if changes
                else ""
            )
            + f'<p><a href="/recovery/{self.book}">Recover original or retained revision</a></p>'
            + "</div></details>"
        )


def viewport(review):
    sample = next(iter(review.samples), None)
    return f"{sample.width} × {sample.height}" if sample else ""


def gate_state(gate):
    if gate.status == "passed":
        return "done"
    if gate.status == "skipped":
        return "skipped"
    return "attention" if gate.required else "waiting"


def arriving(app, run):
    """A book whose import has not produced a saved report yet."""
    stations = "".join(
        f'<li><span class="station is-{"running" if run.active and key == "import" else "failed" if key == "import" and run.failed else "waiting"}">'
        f'<span class="num label">{n:02d}</span><span class="station-name">{esc(name)}</span>'
        f"{tag('running' if run.active and key == 'import' else 'failed' if key == 'import' and run.failed else 'waiting', 'Working' if run.active and key == 'import' else 'Stopped' if key == 'import' and run.failed else 'Waiting')}</span></li>"
        for n, (key, name) in enumerate(STATIONS, 1)
    )
    if run.active:
        panel = (
            '<section class="panel" aria-live="polite"><header class="panel-head"><span class="label">Now</span>'
            f'<h2>Working on it</h2>{tag("running", "Importing")}</header><div class="panel-body"><div class="split"><div>'
            f"{checklist(run.steps)}</div><div><p>Emend is copying the file into its library and running the "
            "required checks. Then it repairs what it can and renders pages.</p>"
            '<p class="quiet small">This page updates by itself.</p></div></div></div></section>'
        )
    else:
        failed = run.failed[0] if run.failed else None
        panel = (
            '<section class="panel"><header class="panel-head"><span class="label">01</span>'
            f'<h2>Import</h2>{tag("failed", "Stopped")}</header><div class="panel-body"><div class="split"><div>'
            '<div class="trouble">'
            + label("What went wrong")
            + "<h3>The book could not be brought in.</h3>"
            + f"<p>{esc(failed.note if failed else 'The import stopped before it finished.')}</p>"
            + label("What you can do")
            + '<ol class="remedy"><li>Check that the file is a complete EPUB, then choose it again.'
            "<p>If it was still downloading or syncing, wait for it to finish first.</p></li>"
            "<li>For very large files, use the inbox or <code>emend ingest</code> in a terminal.</li></ol>"
            f'<div class="actions"><a class="btn primary" href="/">Back to the shelf</a></div></div>'
            f"</div><div>{checklist(run.steps)}</div></div></div></section>"
        )
    body = (
        '<nav class="crumbs label" aria-label="Breadcrumb"><a href="/">Shelf</a><span>/</span>'
        f"<span>{esc(run.title)}</span></nav>"
        f'<header class="book-head"><div><h1 class="display">{esc(run.title)}</h1>'
        '<p class="byline"><span>Arriving</span></p></div></header><div class="spine-figure"></div>'
        f'<ol class="stations" aria-label="Book steps">{stations}</ol>{panel}'
    )
    return page(
        run.title,
        body,
        status="/status" if app.runner.active else None,
        version=app.runner.version,
        kind="book",
    )


def recovery(app, book, reason=""):
    from emend.recovery import recovery_options

    row = app.store.resolve(book)
    options = recovery_options(row["id"], app.config)
    choices = "".join(
        f'<li><label class="check"><input type="radio" name="revision" value="{esc(c["sha256"])}" required>'
        f"<span>{'Preserved original' if c['original'] else 'Previously approved revision'}"
        f"<small><code>{esc(c['sha256'])}</code>"
        f"{' · approved ' + esc(nice_time(c['approved_at'])) if c.get('approved_at') else ''}</small></span></label></li>"
        for c in options
    )
    body = (
        f'<nav class="crumbs label" aria-label="Breadcrumb"><a href="/">Shelf</a><span>/</span>'
        f'<a href="/books/{row["id"]}">{esc(row["source_name"])}</a><span>/</span><span>Recovery</span></nav>'
        f'<header class="book-head"><div><h1 class="display">Recover a book</h1>'
        f'<p class="byline"><span>{esc(row["source_name"])}</span></p></div></header><div class="spine-figure"></div>'
        '<section class="panel"><header class="panel-head"><span class="label">Recovery</span>'
        '<h2>Choose a version to restore</h2></header><div class="panel-body">'
    )
    if reason:
        body += (
            f'<div class="alert" role="alert">{label("Why you are here")}<p>{esc(reason)}</p></div>'
        )
    body += (
        '<div class="split"><div>'
        "<p>The current file, if there is one, moves to quarantine; the original stays preserved. "
        "Approval is revoked before anything is replaced, and every required check runs again. "
        "Each listed file is verified when recovery runs.</p>"
    )
    if choices:
        fields = (
            f'<ul class="recovery-list">{choices}</ul>'
            '<label class="check"><input type="checkbox" name="approve" value="yes" required>'
            "<span>I approve replacing the current artifact with this revision.</span></label>"
        )
        body += app.form(
            "recover",
            "Restore selected revision and revalidate",
            fields,
            book=row["id"],
            hidden={"expected_report": row["report_path"] or ""},
            cls="btn primary",
            working="Restoring…",
        )
    else:
        body += "<p>No retained source is available. Restore preserved source files from your own backup first.</p>"
    body += "</div><div></div></div></div></section>"
    return page("Recovery", body, kind="book")


def evidence_page_image(store, book, kind, identifier, phase, name):
    """Serve one recorded screenshot only after its registered and recorded hashes match."""
    from emend.typography import TypographyTrial
    from emend.visual import VisualReview

    row = store.document(book, kind, identifier)
    path = Path(row["path"])
    expected = (
        store.root / ("visual" if kind == "visual-review" else "typography") / book / identifier
    )
    if path.parent.resolve() != expected.resolve() or path.stat().st_size > 8 * 1024**2:
        raise ValueError("Invalid review location or size.")
    if sha256_file(path) != row["sha256"]:
        raise ValueError("Review changed after registration.")
    if kind == "visual-review":
        review = VisualReview.model_validate_json(path.read_text())
        samples, folder = (review.samples if phase == "pages" else []), path.parent
    else:
        review = TypographyTrial.model_validate_json(path.read_text())
        samples = getattr(review, phase) if phase in {"before", "after"} else []
        folder = path.parent / phase
    if review.id != identifier or review.book_id != book:
        raise ValueError("Review identity mismatch.")
    shot = next(
        (shot for sample in samples for shot in sample.screenshots if shot.file == name), None
    )
    if shot is None or Path(name).name != name:
        raise ValueError("Unknown page image.")
    image = folder / name
    if image.stat().st_size > 48 * 1024**2:
        raise ValueError("Page image exceeds serving limit.")
    data = image.read_bytes()
    if hashlib.sha256(data).hexdigest() != shot.sha256:
        raise ValueError("Page image hash mismatch.")
    return data


def book_page(app, book, error=None, step=None, opened=""):
    try:
        saved = app.store.resolve(book)
    except ValueError:
        run = app.runner.get(book) if len(book) == 64 else None
        if run:
            return arriving(app, run)
        raise
    run = app.runner.get(saved["id"])
    if run and not saved["report_path"]:
        return arriving(app, run)
    try:
        if not saved["report_path"] or Path(saved["report_path"]).stat().st_size > 8 * 1024**2:
            raise ValueError("No bounded completed report is available.")
        row, source, digest = current_source(app.store, book, app.config)
    except (ValueError, OSError) as exc:
        if run and run.active:
            return arriving(app, run)
        return recovery(app, book, str(exc))
    return BookView(app, row, source, digest, error=error, step=step, opened=opened).render()


# Saved evidence -------------------------------------------------------------

EVIDENCE = {"cover-review", "repairs", "visual-review", "typography-trial", "reading-review"}


def load_evidence(store, book, kind, identifier):
    if kind == "cover-review":
        from emend.covers import load_cover_review

        return load_cover_review(store, book, identifier)
    if kind == "repairs":
        from emend.repairs import load_repairs

        return load_repairs(store, book, identifier)
    if kind == "visual-review":
        from emend.visual import load_visual_review

        return load_visual_review(store, book, identifier)
    if kind == "typography-trial":
        from emend.typography import load_trial

        return load_trial(store, book, identifier)
    if kind == "reading-review":
        from emend.reading_review import load_reading_review

        return load_reading_review(store, book, identifier)
    raise ValueError("Unsupported review type.")


def current_reviews(store, config, row, digest):
    """Latest verified evidence per kind: (evidence, current, problem). GET never starts a scan."""
    with store.db() as db:
        records = db.execute(
            "SELECT * FROM review_documents WHERE book_id=? ORDER BY created_at DESC LIMIT 100",
            (row["id"],),
        ).fetchall()
    reviews = {}
    for record in records:
        kind = record["kind"]
        if kind not in EVIDENCE or kind in reviews:
            continue
        try:
            evidence = load_evidence(store, row["id"], kind, record["id"])
            current = (
                getattr(evidence, "artifact_sha256", getattr(evidence, "input_sha256", None))
                == digest
            )
            current = current and evidence.engine_version == ENGINE_VERSION
            if kind == "repairs":
                current = (
                    current
                    and evidence.policy_sha256 == config.policy_hash()
                    and evidence.report_sha256 == sha256_file(Path(row["report_path"]))
                )
            reviews[kind] = (evidence, current, None)
        except (ValueError, OSError) as exc:
            reviews[kind] = (None, False, str(exc)[:500])
    return reviews


def details_section(view):
    """Edition lookups, suggested details and covers. Nothing changes until it is saved."""
    from emend.research.discovery import load_discovery

    book, digest, config, form = view.book, view.digest, view.config, view.form
    body = (
        "<p>Lookups send the book’s title, author or ISBN to Open Library and Google Books, "
        "never its text. Suggestions keep their evidence; nothing changes until you save it.</p>"
        '<div class="split"><div>'
        + form(
            "research",
            "Look up ISBN",
            '<label class="field"><span>ISBN, optional override</span><input name="isbn" maxlength="32"></label>',
            cls="btn",
            working="Looking up…",
        )
        + "</div><div>"
        + form(
            "discover",
            "Search for editions",
            '<label class="field"><span>Title, optional override</span><input name="title" maxlength="2000"></label>'
            '<label class="field"><span>Author, optional override</span><input name="author" maxlength="2000"></label>',
            cls="btn",
            working="Searching…",
        )
        + "</div></div>"
    )
    try:
        discovery = load_discovery(view.store, book)
        current = (
            discovery.artifact_sha256 == digest
            and discovery.engine_version == ENGINE_VERSION
            and discovery.policy_sha256 == config.metadata.fingerprint()
        )
        options = "".join(
            f'<li><label class="check"><input type="radio" name="edition" value="{esc(c.edition.record_id)}" required>'
            f'<span class="edition"><strong>{esc(c.edition.title)}</strong>'
            f"<span>{esc(' · '.join(filter(None, ['; '.join(c.edition.creators), '; '.join(c.edition.publisher), '; '.join(c.edition.date), '; '.join(c.edition.language)])))}</span>"
            f"<small>{esc('; '.join(c.matches + c.differences))}</small></span></label></li>"
            for c in discovery.candidates
        )
        body += '<div class="block">' + label("Editions found")
        if not current:
            body += "<p class='quiet'>These results describe an earlier version. Search again.</p>"
        elif options:
            body += form(
                "choose-edition",
                "Use this edition for suggestions",
                f'<ul class="cover-choices">{options}</ul>',
                {"discovery": discovery.id},
                cls="btn",
                working="Gathering…",
            )
        else:
            body += "<p class='quiet'>No matching editions were found.</p>"
        body += "</div>"
    except (ValueError, OSError):
        pass
    try:
        from emend.research.service import load_plan

        plan = load_plan(view.store, book)
        current = (
            plan.artifact_sha256 == digest
            and plan.engine_version == ENGINE_VERSION
            and plan.policy_sha256 == config.metadata.fingerprint()
        )
        body += '<div class="block">' + label("Suggested details")
        if not current:
            body += "<p class='quiet'>The book changed. Look up details again to refresh suggestions.</p>"
        else:
            body += "<p>Suggestions for this version. Select the ones to save.</p>"
            options = "".join(
                f'<li><label class="check"><input type="checkbox" name="proposal" value="{esc(p.id)}">'
                f"<span><strong>{esc(p.field)}</strong>: {esc('; '.join(p.before) or '(missing)')} → "
                f"{esc('; '.join(p.after))}<small>{esc(p.reason)} · {esc(p.decision)}</small></span></label></li>"
                for p in plan.proposals
            )
            if options:
                body += form(
                    "apply-metadata",
                    "Save selected book details",
                    f'<ul class="cover-choices">{options}</ul>',
                    {"plan": plan.id},
                    disabled=not view.ready,
                    cls="btn primary",
                    working="Saving…",
                )
                if not view.ready:
                    body += "<p class='quiet small'>Resolve the required check failures before saving book details.</p>"
            body += '<div class="actions">' + form(
                "cover-review",
                "Find covers from these sources",
                hidden={"plan": plan.id},
                cls="btn",
                working="Finding covers…",
            )
            body += form(
                "ai-review",
                "Ask Meta for advice",
                hidden={"plan": plan.id},
                cls="btn",
                working="Asking…",
            )
            body += (
                '<p class="note quiet small">Meta advice is an optional API request that may incur a '
                "charge. Advice cannot approve or apply changes.</p></div>"
            )
            try:
                from emend.research.ai_review import review_text, show_review

                body += (
                    '<details class="fold"><summary>Saved Meta advice</summary><div class="fold-body">'
                    f'<pre class="advice">{esc(review_text(show_review(book, config, plan.id)))}</pre></div></details>'
                )
            except (ValueError, OSError):
                pass
        if plan.provider_errors:
            body += "<p class='quiet small'>Some providers were unavailable; check for missing evidence before saving.</p>"
        body += "</div>"
    except (ValueError, OSError):
        body += "<p class='quiet small'>Look up an ISBN or search for an edition to get suggestions.</p>"
    body += '<div class="block" id="cover-review">' + label("Cover")
    body += cover_choices(view)
    body += (
        '<h3>Use your own cover</h3><form method="post" action="/action" enctype="multipart/form-data">'
        f'<input type="hidden" name="csrf" value="{view.app.csrf}"><input type="hidden" name="action" value="cover-local">'
        f'<input type="hidden" name="book" value="{book}"><input type="hidden" name="artifact" value="{digest}">'
        '<label class="field"><span>JPEG or PNG cover, up to 20 MiB</span>'
        '<input type="file" name="cover" accept="image/jpeg,image/png" required></label>'
        '<label class="field"><span>Source/provenance note</span><input name="source_note" maxlength="2000" required></label>'
        '<button class="btn" type="submit" data-working="Comparing…">Compare local cover</button></form></div>'
    )
    return body


def cover_choices(view):
    review, current, problem = view.latest("cover-review")
    if problem:
        return f"<p class='quiet'>The saved cover comparison could not be opened. Find covers again. ({esc(problem)})</p>"
    if review is None:
        return "<p class='quiet'>Find covers from catalogue sources above, or compare an image of your own.</p>"
    url = view.evidence_url("cover-review", review.id)
    body = (
        f'<p><a href="{url}" target="_blank" rel="noopener noreferrer">Open the cover comparison</a>'
        + (
            " · applied; kept for reference"
            if review.id in view.applied["cover-review"]
            else ""
            if current
            else " · the book changed since; compare again"
        )
        + "</p>"
    )
    if not (current and review.candidates):
        return body
    info = cover_observation(view.source, view.report.inspection)
    choices = "".join(
        f'<li><label class="check"><input type="radio" name="candidate" value="{esc(c.id)}" required>'
        f"<span>{c.width} × {c.height} pixels<small>{esc(c.source)}</small></span></label></li>"
        for c in review.candidates
    )
    fields = f'<ul class="cover-choices">{choices}</ul>'
    if review.original:
        fields += (
            '<details class="fold"><summary>Allow a size or shape exception</summary><div class="fold-body">'
            '<label class="check"><input type="checkbox" name="allow_smaller" value="yes"><span>Allow a smaller image.</span></label>'
            '<label class="check"><input type="checkbox" name="allow_aspect_change" value="yes"><span>Allow a different image shape.</span></label>'
            "</div></details>"
        )
        body += view.form(
            "apply-cover",
            "Apply reviewed cover",
            fields,
            {"review": review.id},
            disabled=not view.ready,
            cls="btn primary",
            working="Applying…",
        )
    else:
        fields += (
            "<p>Adds a cover image and a first cover page while keeping the existing reading order.</p>"
            '<details class="fold"><summary>Allow a small source image</summary><div class="fold-body">'
            '<label class="check"><input type="checkbox" name="allow_small" value="yes"><span>Approve an image '
            "below 600 pixels on its short edge after checking readability.</span></label></div></details>"
        )
        body += view.form(
            "create-cover",
            "Create reviewed missing cover",
            fields,
            {"review": review.id},
            disabled=not view.ready or not info["can_add"],
            cls="btn primary",
            working="Adding…",
        )
        if not info["can_add"]:
            body += (
                "<p class='quiet small'>This book’s existing cover declaration or layout needs manual "
                "review before Emend can add another cover page.</p>"
            )
    return body
