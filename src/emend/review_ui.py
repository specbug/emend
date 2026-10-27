"""Local-only review workspace. Mutations use the existing explicit, gated APIs."""

import hmac
import html
import secrets
import socket
import tempfile
import threading
from email import policy
from email.parser import BytesParser
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from emend import ENGINE_VERSION
from emend.config import Config
from emend.inspect import sha256_file
from emend.pipeline import get_report, ingest_new
from emend.reports import report_styles
from emend.research.discovery import current_source
from emend.review_guidance import (
    ACTION_PANELS,
    CHECK_NAMES,
    FIX_NAMES,
    SPACING_PRESETS,
    STATUS_NAMES,
    blockers,
    cover_observation,
    epubcheck_messages,
)
from emend.storage import OperationBusyError, Store

EPUB_UPLOAD_MAX_MIB = 128

EVIDENCE = {"cover-review", "repairs", "visual-review", "typography-trial", "reading-review"}


def esc(value):
    return html.escape(str(value), quote=True)


def same_token(provided, expected):
    return (
        isinstance(provided, str) and provided.isascii() and hmac.compare_digest(provided, expected)
    )


def value(form, key, default=""):
    values = form.get(key, [default])
    if len(values) != 1:
        raise ValueError(f"Provide one value for {key}.")
    return values[0]


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


class ReviewApplication:
    def __init__(self, config, reader_directory=None):
        self.config = config
        self.reader_directory = (
            Path(reader_directory).expanduser() if reader_directory is not None else None
        )
        self.store = Store(config.root)
        self.token = secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        self.operation = threading.Lock()
        self.notice = ""
        self.notice_book = None

    def take_notice(self, book=None):
        if self.notice_book != book:
            return ""
        message, self.notice = self.notice, ""
        return message

    def page(self, title, body, notice=""):
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)} · Emend</title><style>{report_styles()}
form {{margin:1rem 0 2rem}} label {{display:block;margin:.5rem 0}} input,select,button {{font:inherit;max-width:100%;padding:.5rem .75rem;border:1px solid var(--rule);border-radius:2px;background:transparent;color:inherit}}
input[type=checkbox],input[type=radio] {{margin-right:.5rem;min-height:0}} button {{cursor:pointer;background:var(--ink);color:var(--paper);margin-top:.5rem}} button:disabled {{opacity:.5;cursor:default}}
fieldset {{border:1px solid var(--rule);padding:1rem;margin:1rem 0}} legend {{padding:0 .5rem}} section {{border-bottom:1px solid var(--rule);padding-bottom:1rem}} .error {{color:var(--signal)}} .quiet {{color:var(--muted)}} .controls {{display:flex;gap:1rem;flex-wrap:wrap;align-items:end}} .controls form {{margin-bottom:1rem}}
a,p,summary {{overflow-wrap:anywhere}} iframe {{width:100%;height:40rem;border:1px solid var(--rule);margin:1rem 0}} details {{margin:1rem 0}} pre {{white-space:pre-wrap;overflow-wrap:anywhere}} nav {{margin-bottom:2rem}}

.book-steps {{display:flex;gap:1.5rem;flex-wrap:wrap;padding:1.5rem 0;margin:0;border-bottom:1px solid var(--rule)}}
.book-steps a {{font-size:.875rem}} .book-status {{margin:1.5rem 0 0}}
section {{scroll-margin-top:1rem}} .optional {{border-bottom:1px solid var(--rule);padding:1rem 0;margin:0}}
.optional>summary {{font-weight:500;cursor:pointer}} .disclosure-content {{padding:1rem 0 0}}
.saved-review {{margin:1rem 0}} .saved-review summary {{cursor:pointer}}
.problems {{list-style:none;padding:0}} .problems>li {{padding:1rem 0;border-bottom:1px solid var(--rule);max-width:none}}
.problems>li>strong {{font-size:1rem;color:var(--signal)}}
.action-error {{padding:1.5rem 0;border-bottom:1px solid var(--rule)}} .action-error h2 {{margin:0 0 1rem;color:var(--signal)}}
button,input,select {{min-height:2.75rem}} button:disabled {{opacity:1;color:var(--muted);background:transparent;cursor:not-allowed}}
.button-link {{display:inline-block;padding:.75rem 1.5rem;background:var(--ink);color:var(--paper);border-radius:2px;text-decoration:none}}
button:focus-visible,input:focus-visible,select:focus-visible,a:focus-visible,summary:focus-visible {{outline:2px solid var(--signal);outline-offset:4px}}
</style></head><body><main><nav><a href="/">Emend library</a></nav>{f'<p role="status">{esc(notice)}</p>' if notice else ""}{body}<footer>Original EPUBs are kept unchanged. Changes are checked before use.</footer></main></body></html>"""

    def form(self, action, label, fields="", *, book="", digest="", hidden=None, disabled=False):
        values = {
            "action": action,
            "csrf": self.csrf,
            "book": book,
            "artifact": digest,
            **(hidden or {}),
        }
        inputs = "".join(
            f'<input type="hidden" name="{esc(k)}" value="{esc(v)}">' for k, v in values.items()
        )
        return f'<form method="post" action="/action">{inputs}{fields}<button type="submit"{" disabled" if disabled else ""}>{esc(label)}</button></form>'

    def library(self, offset=0):
        with self.store.db() as db:
            rows = db.execute(
                "SELECT * FROM books ORDER BY updated_at DESC LIMIT 101 OFFSET ?", (offset,)
            ).fetchall()
        items = []
        for row in rows[:100]:
            try:
                if not row["report_path"] or Path(row["report_path"]).stat().st_size > 8 * 1024**2:
                    raise ValueError("Report exceeds browser review limit.")
                report = get_report(self.store, row["id"])
                title = "; ".join(report.inspection.metadata.get("title", [])) or row["source_name"]
            except (ValueError, OSError):
                title = row["source_name"]
            status = (
                "Checks passed"
                if row["state"] in {"VALIDATED", "PUBLISHED"}
                else "Needs attention"
                if row["state"] in {"FAILED", "REVIEW_REQUIRED"}
                else "Checks unfinished"
            )
            items.append(
                f'<tr><th><a href="/books/{row["id"]}">{esc(title[:2000])}</a></th><td>{esc(status)}</td></tr>'
            )
        upload = f'''<form method="post" action="/action" enctype="multipart/form-data"><input type="hidden" name="csrf" value="{self.csrf}"><input type="hidden" name="action" value="import"><label>Choose an EPUB <input type="file" name="epub" accept=".epub,application/epub+zip" required></label><p class="quiet">Up to {min(self.config.limits.max_archive_mb, EPUB_UPLOAD_MAX_MIB)} MiB here. Use the inbox/CLI for larger files. Known books are left unchanged.</p><button>Import EPUB</button></form>'''
        paging = (
            f'<a href="/?offset={max(0, offset - 100)}">Previous books</a> ' if offset else ""
        ) + (f'<a href="/?offset={offset + 100}">Next books</a>' if len(rows) > 100 else "")
        backup = ""
        if self.config.publish.calibre_library:
            fields = '<label><input type="checkbox" name="closed" value="yes" required>Calibre and other library writers are closed.</label>'
            if self.config.publish.backup_remote:
                fields += '<label><input type="checkbox" name="upload" value="yes">Upload the verified complete-library snapshot to the configured Drive remote.</label>'
            backup = (
                '<details class="optional"><summary>Back up the Calibre library</summary><div class="disclosure-content"><p>Copies the entire library, including book formats, covers, metadata and notes. Backups need Calibre and its Content Server closed. Importing, reviewing and downloading EPUBs do not.</p>'
                + self.form("backup", "Create verified backup", fields)
                + "</div></details>"
            )
        return self.page(
            "Library",
            f'<header><div class="brand">EMEND / REVIEW</div><h1>Your library</h1><p>Import an EPUB, resolve any problems, preview it, then download or send your book.</p></header><section><h2>Import a book</h2>{upload}</section><section><h2>Books</h2><p class="quiet">Open a book to see its checks, available fixes and next step.</p><table><thead><tr><th>Book</th><th>Check status</th></tr></thead><tbody>{"".join(items)}</tbody></table>{paging if items else "<p>No books yet. Import an EPUB above.</p>"}</section>{backup}',
            notice=self.take_notice(),
        )

    def recovery(self, book, reason=""):
        from emend.recovery import recovery_options

        row = self.store.resolve(book)
        options = recovery_options(row["id"], self.config)
        choices = "".join(
            f'<label><input type="radio" name="revision" value="{esc(c["sha256"])}" required>{"Preserved original" if c["original"] else "Previously approved revision"} · <code>{esc(c["sha256"])}</code></label>'
            for c in options
        )
        body = f'<header><div class="brand">EMEND / RECOVERY</div><h1>Recover a book</h1><p>{esc(row["source_name"])}</p></header>'
        if reason:
            body += f'<p class="error">{esc(reason)}</p>'
        body += "<p>Choose a revision to restore. Current artifact bytes, if present, move to quarantine. Original bytes stay preserved. Approval is revoked before replacement and all required checks run again. Listed files are verified when recovery runs.</p>"
        if choices:
            choices += '<label><input type="checkbox" name="approve" value="yes" required>I approve replacing the current artifact with this revision.</label>'
            body += self.form(
                "recover",
                "Restore selected revision and revalidate",
                choices,
                book=row["id"],
                hidden={"expected_report": row["report_path"] or ""},
            )
        else:
            body += "<p>No retained source is available. Restore preserved source files from your own backup first.</p>"
        return self.page("Recovery", body)

    def metadata_options(self, book, digest, form, extra="", opened=False, ready=True):
        body = (
            '<details class="optional"><summary>Book details and cover</summary><div class="disclosure-content"><p>Lookups send selected bibliographic metadata to public providers, never EPUB text. Inspect edition evidence before choosing changes.</p><div class="controls">'
            + form(
                "research",
                "Look up ISBN",
                '<label>ISBN override, optional <input name="isbn" maxlength="32"></label>',
            )
            + form(
                "discover",
                "Search for editions",
                '<label>Title override, optional <input name="title" maxlength="2000"></label><label>Author override, optional <input name="author" maxlength="2000"></label>',
            )
            + "</div>"
        )
        try:
            from emend.research.discovery import load_discovery

            discovery = load_discovery(self.store, book)
            current = (
                discovery.artifact_sha256 == digest
                and discovery.engine_version == ENGINE_VERSION
                and discovery.policy_sha256 == self.config.metadata.fingerprint()
            )
            options = "".join(
                f'<label><input type="radio" name="edition" value="{esc(c.edition.record_id)}" required>{esc(c.edition.title)} · {esc("; ".join(c.edition.creators))} · {esc("; ".join(c.edition.language))} · {esc("; ".join(c.edition.publisher))} · {esc("; ".join(c.edition.date))}<p class="quiet">{esc("; ".join(c.matches + c.differences))}</p></label>'
                for c in discovery.candidates
            )
            body += f"<p>Edition discovery: {'current' if current else 'stale; run discovery again'}.</p>"
            if current and options:
                body += form(
                    "choose-edition",
                    "Use this edition for suggestions",
                    options,
                    {"discovery": discovery.id},
                )
        except (ValueError, OSError):
            pass
        try:
            from emend.research.service import load_plan

            plan = load_plan(self.store, book)
            current = (
                plan.artifact_sha256 == digest
                and plan.engine_version == ENGINE_VERSION
                and plan.policy_sha256 == self.config.metadata.fingerprint()
            )
            body += f"<p>{'Suggestions for this version' if current else 'The book changed. Look up details again to refresh suggestions.'}</p>"
            if current:
                options = "".join(
                    f'<label><input type="checkbox" name="proposal" value="{esc(p.id)}"><strong>{esc(p.field)}</strong>: {esc("; ".join(p.before) or "(missing)")} → {esc("; ".join(p.after))}<p class="quiet">{esc(p.reason)} · {esc(p.decision)}</p></label>'
                    for p in plan.proposals
                )
                if options:
                    body += form(
                        "apply-metadata",
                        "Save selected book details",
                        options,
                        {"plan": plan.id},
                        disabled=not ready,
                    )
                    if not ready:
                        body += (
                            "<p>Resolve the required check failures before saving book details.</p>"
                        )
                body += form(
                    "cover-review", "Find covers from these sources", hidden={"plan": plan.id}
                )
                body += form(
                    "ai-review",
                    "Ask Meta for advice",
                    '<p class="quiet">Optional API request may incur a charge. Advice cannot approve or apply changes.</p>',
                    {"plan": plan.id},
                )
                try:
                    from emend.research.ai_review import review_text, show_review

                    body += f"<details><summary>Saved Meta advice</summary><pre>{esc(review_text(show_review(book, self.config, plan.id)))}</pre></details>"
                except (ValueError, OSError):
                    pass
            if plan.provider_errors:
                body += '<p class="error">Some providers were unavailable; inspect missing evidence before editing.</p>'
        except (ValueError, OSError):
            body += '<p class="quiet">Look up an ISBN or search for an edition to get suggestions. Nothing changes until you select and save it.</p>'
        body += f'<h3>Use your own cover</h3><form method="post" action="/action" enctype="multipart/form-data"><input type="hidden" name="csrf" value="{self.csrf}"><input type="hidden" name="action" value="cover-local"><input type="hidden" name="book" value="{book}"><input type="hidden" name="artifact" value="{digest}"><label>JPEG or PNG cover, up to 20 MiB <input type="file" name="cover" accept="image/jpeg,image/png" required></label><label>Source/provenance note <input name="source_note" maxlength="2000" required></label><button>Compare local cover</button></form>'
        body = body.replace(
            '<details class="optional">',
            f'<details class="optional" id="book-details"{" open" if opened else ""}>',
            1,
        )
        return body + extra + "</div></details>"

    def book(self, book, error=None, panel=""):
        try:
            saved = self.store.resolve(book)
            if not saved["report_path"] or Path(saved["report_path"]).stat().st_size > 8 * 1024**2:
                raise ValueError("No bounded completed report is available.")
            row, source, digest = current_source(self.store, book, self.config)
        except (ValueError, OSError) as exc:
            return self.recovery(book, str(exc))
        book = row["id"]
        report = get_report(self.store, book)
        title = "; ".join(report.inspection.metadata.get("title", [])) or row["source_name"]
        author = "; ".join(report.inspection.metadata.get("creator", []))
        problems = blockers(row, report, digest, self.config)
        ready = not problems
        cover_info = cover_observation(source, report.inspection)

        def form(action, label, fields="", hidden=None, disabled=False):
            return self.form(
                action, label, fields, book=book, digest=digest, hidden=hidden, disabled=disabled
            )

        # Read registered evidence only. GET never starts a scan or changes a book.
        with self.store.db() as db:
            records = db.execute(
                "SELECT * FROM review_documents WHERE book_id=? ORDER BY created_at DESC LIMIT 100",
                (book,),
            ).fetchall()
        reviews = {}
        for record in records:
            kind = record["kind"]
            if kind not in EVIDENCE or kind in reviews:
                continue
            try:
                evidence = load_evidence(self.store, book, kind, record["id"])
                current = (
                    getattr(evidence, "artifact_sha256", getattr(evidence, "input_sha256", None))
                    == digest
                )
                current = current and evidence.engine_version == ENGINE_VERSION
                if kind == "repairs":
                    current = (
                        current
                        and evidence.policy_sha256 == self.config.policy_hash()
                        and evidence.report_sha256 == sha256_file(Path(row["report_path"]))
                    )
                reviews[kind] = (evidence, current, None)
            except (ValueError, OSError) as exc:
                reviews[kind] = (None, False, str(exc)[:500])

        applied = {
            "typography-trial": {c.trial_id for c in report.typography_changes},
            "repairs": {c.plan_id for c in report.repair_changes},
            "cover-review": {c.review_id for c in [*report.cover_changes, *report.cover_additions]},
        }

        def review(kind, label):
            if kind not in reviews:
                return ""
            evidence, current, problem = reviews[kind]
            if problem:
                return f'<p class="error">{esc(label)} could not be opened. Run this step again.</p><details><summary>Error details</summary><p>{esc(problem)}</p></details>'
            complete = (
                getattr(
                    evidence,
                    "status",
                    "complete" if getattr(evidence, "complete", True) else "incomplete",
                )
                == "complete"
            )
            status = (
                "Applied; kept for reference"
                if evidence.id in applied.get(kind, set())
                else "Ready to view"
                if current and complete
                else "Incomplete; run this step again"
                if current
                else "The book or checks changed; run this step again"
            )
            url = f"/evidence/{book}/{kind}/{evidence.id}"
            return f'<p class="quiet">{esc(label)}: {status}.</p><details class="saved-review"><summary>View {esc(label.lower())}</summary><iframe sandbox loading="lazy" src="{url}" title="{esc(label)}"></iframe><p><a href="{url}" target="_blank" rel="noopener noreferrer">Open full review in a new tab</a></p></details>'

        body = f'<header><div class="brand">EMEND / BOOK</div><h1>{esc(title)}</h1><p>{esc(author)}</p><p class="book-status">{"Needs attention before sending" if problems else "Checks passed · ready to send"}</p></header>'
        if error:
            message = (
                "This book is not ready to send. The problems and next steps are below."
                if str(error).startswith("Publication blocked:")
                else error
            )
            body += f'<div class="action-error" role="alert"><h2>That step could not finish</h2><p>{esc(message)}</p><a href="#checks">See checks and next steps</a></div>'
        body += '<nav class="book-steps" aria-label="Book workflow"><a href="#checks">1. Check &amp; fix</a><a href="#preview">2. Preview</a><a href="#send">3. Send</a></nav>'
        body += '<section id="checks"><h2>1. Check &amp; fix</h2>'
        issues = ""
        if ready:
            body += "<p>The required checks passed. Preview a few pages, then download or send your EPUB.</p>"
        else:
            body += "<p>Resolve the problems below before downloading or sending this book. Your original is preserved.</p>"
            issues += '<ul class="problems">'
            messages, note = epubcheck_messages(report, self.config.root / "reports" / book)
            for name, label, detail in problems:
                issues += f"<li><strong>{esc(label)}</strong>"
                if name == "structure":
                    findings = [f for f in report.inspection.findings if f.severity == "error"]
                    shown_findings = {}
                    for finding in findings[:20]:
                        message = (
                            "The EPUB file needs a packaging repair before readers can reliably open it."
                            if finding.code in {"zip.mimetype_order", "zip.mimetype_header"}
                            else finding.message
                        )
                        shown_findings.setdefault(message, finding.location)
                    issues += (
                        "<ul>"
                        + "".join(
                            f"<li>{esc(text)}{f' <span class=quiet>({esc(where)})</span>' if where else ''}</li>"
                            for text, where in shown_findings.items()
                        )
                        + "</ul>"
                        if findings
                        else f"<p>{esc(detail)}</p>"
                    )
                elif name == "epubcheck":
                    issues += (
                        "<ul>"
                        + "".join(
                            f"<li>{esc(text)}{f' <code>{esc(where)}</code>' if where else ''}</li>"
                            for text, where in messages
                        )
                        + "</ul>"
                        if messages
                        else "<p>This check will run after the EPUB structure problems are resolved.</p>"
                        if any(
                            g.name == "epubcheck" and g.status == "skipped" for g in report.gates
                        )
                        else f"<p>{esc(detail)}</p>"
                    )
                    if note:
                        issues += f'<p class="quiet">{esc(note)}</p>'
                elif name == "accessibility":
                    gate = next(g for g in report.gates if g.name == name)
                    issues += f"<p>{esc(detail)}</p>"
                    if gate.accessibility:
                        issues += (
                            "<ul>"
                            + "".join(
                                f"<li>{esc(f.message)}{f' <code>{esc(f.document)}</code>' if f.document else ''}</li>"
                                for f in gate.accessibility.findings[:20]
                            )
                            + "</ul>"
                        )
                elif name in {"revalidation", "recovery", "approval"}:
                    issues += "<p>The checks have not finished. Run checks again to continue.</p>"
                else:
                    issues += f"<p>{esc(detail)}</p>"
                gate = next((g for g in report.gates if g.name == name), None)
                if gate and gate.status == "unavailable":
                    if name == "epubcheck":
                        issues += "<p>Install EPUBCheck on your Mac, then run the checks again. With Homebrew: <code>brew install epubcheck</code>.</p>"
                    elif name == "accessibility":
                        issues += (
                            "<p>Install and configure Ace by DAISY, then run the checks again.</p>"
                        )
                    issues += '<p><a href="https://github.com/specbug/emend/blob/main/docs/mac-validation.md" target="_blank" rel="noopener noreferrer">Open tool setup instructions</a></p>'
                issues += "</li>"
            issues += "</ul>"

        repair = reviews.get("repairs")
        missing_tool = any(g.required and g.status == "unavailable" for g in report.gates)
        if repair and repair[0] is not None and repair[1]:
            plan = repair[0]
            if plan.actions:
                body += "<h3>Available fix</h3><p>Applies this fix, then checks the book again. A rejected change leaves the previous book in place.</p>"
                body += (
                    "<details><summary>What will change</summary>"
                    + f"<p>{esc(plan.detail)}</p></details>"
                )
                for action in plan.actions:
                    body += form(
                        "apply-repairs",
                        FIX_NAMES.get(action, "Apply this fix"),
                        hidden={"plan": plan.id, "approved_action": action},
                        disabled=missing_tool,
                    )
                if missing_tool:
                    body += "<p>Install the missing checker above before applying this fix.</p>"
                body += review("repairs", "Proposed fix")
            elif problems:
                body += "<h3>No automatic fix for these problems</h3><p>This scan found no fix Emend can safely apply. Use Calibre’s <strong>Edit book → Tools → Check book</strong> to inspect the listed file errors. Save an edited EPUB outside your Calibre library and import that file into Emend for fresh checks.</p><p>Emend currently fixes EPUB packaging and certain broken contents links. It cannot repair every validator or accessibility error.</p>"
        elif problems and not missing_tool:
            body += form("repair-plan", "Find available fixes")
            body += '<p class="quiet">Looks for supported fixes without changing the book. You choose whether to apply them.</p>'
        if issues:
            body += "<h3>Problems to resolve</h3>" + issues
        body += form("revalidate", "Run checks again")
        body += '<p class="quiet">Use this after fixing tool setup or changing your check settings. Checks can take a minute.</p>'
        body += '</section><section id="preview"><h2>2. Preview</h2><p>Check how the book looks before sending it. These are browser samples; also inspect it on your reader.</p>'
        body += f"<h3>{esc(cover_info['title'])}</h3><p>{esc(cover_info['detail'])}</p>"
        if cover_info["warnings"]:
            body += "<ul>" + "".join(f"<li>{esc(w)}</li>" for w in cover_info["warnings"]) + "</ul>"
        body += f'<p><a href="/books/{book}?panel=details#book-details">Review or change the cover</a></p><details><summary>Cover missing on CrossPoint?</summary><p>CrossPoint 2.0.0-rc makes its own cover and thumbnail BMP caches from the EPUB. Emend exports the EPUB, without device-cache sidecars.</p><p>For sleep, choose Settings → Display → Sleep Screen → Cover, then open the book and put the reader to sleep. A Calibre library thumbnail alone does not prove the EPUB contains its cover.</p><p>If a valid embedded cover still does not appear, try the EPUB under a new filename to use a fresh device cache while keeping the old copy and its reading data. This is a diagnostic step, not a device-compatibility guarantee.</p></details>'
        body += form(
            "visual",
            "Preview pages",
            '<label>View as <select name="layout"><option value="scroll">Scrolling pages</option><option value="paginated">Reader-style pages</option></select></label>',
        )
        body += review("visual-review", "Page preview")
        body += f'<details class="optional" id="spacing"{" open" if panel == "spacing" else ""}><summary>Adjust spacing</summary><div class="disclosure-content"><p>Optional. Choose a starting point and compare it with the current book. A preview does not change the EPUB.</p>'
        body += form(
            "typography-trial",
            "Preview spacing",
            '<label>Spacing <select name="spacing_preset"><option value="comfortable">Comfortable</option><option value="compact">Compact</option><option value="roomy">Roomy</option><option value="custom">Custom</option></select></label><details><summary>Custom values</summary><p>Enter either value to override the preset. Leave the other blank to keep the book’s current value.</p><label>Line spacing multiplier <input type="number" name="line_height" min="1" max="2.4" step="0.05"></label><label>Space after paragraphs, in text-size units <input type="number" name="paragraph_gap" min="0" max="2" step="0.05"></label></details><input type="hidden" name="layout" value="scroll">',
        )
        body += review("typography-trial", "Spacing comparison")
        trial = reviews.get("typography-trial")
        if (
            trial
            and trial[0]
            and trial[1]
            and trial[0].status == "complete"
            and trial[0].id not in applied["typography-trial"]
        ):
            body += form(
                "apply-typography",
                "Apply this spacing",
                '<p>Applies to the previewed chapters. Review the comparison first.</p><label><input type="checkbox" name="all_spine" value="yes">Also apply to the remaining chapters, which were not previewed.</label>',
                {"trial": trial[0].id},
                disabled=not ready,
            )
            if not ready:
                body += "<p>Resolve the required check failures before applying spacing.</p>"
        body += f'</div></details><details class="optional" id="reading"{" open" if panel == "reading" else ""}><summary>Fonts and footnotes</summary><div class="disclosure-content"><p>Lists font resources and marked note links. It does not certify font appearance or reader behavior.</p>'
        body += (
            form("reading", "Check fonts and footnotes")
            + review("reading-review", "Font and footnote report")
            + "</div></details></section>"
        )
        cover_body = '<div id="cover-review">' + review("cover-review", "Cover comparison")
        cover = reviews.get("cover-review")
        if cover and cover[0] and cover[1] and cover[0].candidates:
            evidence = cover[0]
            choices = "".join(
                f'<label><input type="radio" name="candidate" value="{esc(c.id)}" required>{c.width} × {c.height} pixels · {esc(c.source)}</label>'
                for c in evidence.candidates
            )
            if evidence.original:
                choices += '<details><summary>Allow a size or shape exception</summary><label><input type="checkbox" name="allow_smaller" value="yes">Allow a smaller image.</label><label><input type="checkbox" name="allow_aspect_change" value="yes">Allow a different image shape.</label></details>'
                cover_body += form(
                    "apply-cover",
                    "Apply reviewed cover",
                    choices,
                    {"review": evidence.id},
                    disabled=not ready,
                )
            else:
                choices += '<p>Adds a cover image and first cover page while keeping the existing reading order.</p><details><summary>Allow a small source image</summary><label><input type="checkbox" name="allow_small" value="yes">Approve an image below 600 pixels on its short edge after checking readability.</label></details>'
                cover_body += form(
                    "create-cover",
                    "Create reviewed missing cover",
                    choices,
                    {"review": evidence.id},
                    disabled=not ready or not cover_info["can_add"],
                )
                if not cover_info["can_add"]:
                    cover_body += "<p>This book’s existing cover declaration or layout needs manual review before Emend can add another cover page.</p>"
        body += self.metadata_options(
            book, digest, form, cover_body + "</div>", panel == "details", ready=ready
        )
        body += '<section id="send"><h2>3. Send your book</h2>'
        if ready:
            body += f'<p>Your EPUB passed the required checks. Download it for CrossPoint’s file-transfer page, or use a configured destination below.</p><p><a class="button-link" href="/books/{book}/download">Download EPUB</a></p>'
        else:
            body += '<p>Sending is unavailable until the required checks pass. <a href="#checks">See the problems and next steps</a>.</p><button disabled>Download EPUB</button>'
        for target, label, configured in [
            ("calibre", "Add to Calibre", self.config.publish.calibre_library),
            ("drive", "Save EPUB to Drive", self.config.publish.drive_remote),
        ]:
            if configured:
                body += form("publish", label, hidden={"target": target}, disabled=not ready)
                if target == "calibre":
                    body += '<p class="quiet">Close Calibre before this direct import. You can keep it open while reviewing or downloading an EPUB.</p>'
        if self.reader_directory:
            body += f"<p>Reader folder: <code>{esc(self.reader_directory)}</code>. Confirm the reader is mounted, then safely eject after copying.</p>"
            body += form(
                "reader-export",
                "Copy to reader",
                '<details><summary>Choose a filename</summary><label>Filename <input name="filename" maxlength="240"></label></details>',
                disabled=not ready,
            )
        body += '</section><details class="optional" id="technical"><summary>Technical details and recovery</summary><div class="disclosure-content">'
        body += f"<p>Recorded state: {esc(row['state'])}. Book ID: <code>{book}</code>. Current file: <code>{esc(digest)}</code>.</p>"
        body += (
            "<ul>"
            + "".join(
                f"<li>{esc(CHECK_NAMES.get(g.name, g.name))}: {esc(STATUS_NAMES[g.status])} ({'required' if g.required else 'optional'})<p>{esc(g.detail)}</p></li>"
                for g in report.gates
            )
            + "</ul>"
        )
        body += f'<p><a href="/recovery/{book}">Recover original or retained revision</a></p>'
        with self.store.db() as db:
            receipts = db.execute(
                "SELECT status,receipt FROM publications WHERE book_id=? ORDER BY updated_at DESC LIMIT 5",
                (book,),
            ).fetchall()
        if receipts:
            body += "<h3>Recent transfers</h3>" + "".join(
                f"<details><summary>{esc(r['status'])}</summary><pre>{esc(r['receipt'][:4000])}</pre></details>"
                for r in receipts
            )
        body += "</div></details>"
        return self.page(title, body, notice=self.take_notice(book))

    def execute(self, form):
        action = value(form, "action")
        book = value(form, "book")
        self.notice = ""
        self.notice_book = None
        if action == "backup":
            from emend.backup import backup_library

            if value(form, "closed") != "yes":
                raise ValueError("Confirm Calibre and other library writers are closed.")
            result = backup_library(self.config, True, upload=value(form, "upload") == "yes")
            self.notice = f"Verified backup saved: {result['archive']}. Cloud status: {result.get('cloud_status', 'local only')}."
            return "/"
        if action == "recover":
            from emend.recovery import recover

            if value(form, "approve") != "yes":
                raise ValueError("Explicitly approve replacing the current artifact.")
            recover(
                book,
                self.config,
                revision=value(form, "revision"),
                approve=True,
                expected_report_path=value(form, "expected_report"),
            )
            return "/books/" + self.store.resolve(book)["id"]
        row, _, digest = current_source(self.store, book, self.config)
        book = row["id"]
        self.notice_book = book
        if value(form, "artifact") != digest:
            raise ValueError("This page is stale; reopen the book before taking action.")
        if action == "revalidate":
            from emend.revalidation import revalidate

            result = revalidate(book, self.config)
            self.notice = (
                "Checks passed. Preview your book or download it below."
                if result.state == "VALIDATED"
                else "Checks finished. The remaining problems are listed below."
            )
        elif action == "reading":
            from emend.reading_review import reading_review

            reading_review(book, self.config)
        elif action == "visual":
            from emend.visual import visual_review

            result = visual_review(
                book, self.config, max_documents=3, layout=value(form, "layout", "scroll")
            )
            self.notice = (
                "Page preview saved. Open it under Preview."
                if result.status == "complete"
                else "The preview is incomplete. See its details under Preview."
            )
        elif action == "repair-plan":
            from emend.repairs import prepare_repairs

            plan, _ = prepare_repairs(book, self.config)
            self.notice = (
                "A supported fix is available under Check & fix. Review it before applying."
                if plan.actions
                else "No automatic fix was found. See the problems and manual next steps below."
            )
        elif action == "apply-repairs":
            from emend.repairs import apply_repairs

            apply_repairs(
                book,
                self.config,
                plan_id=value(form, "plan"),
                approve=form.get("approved_action", []),
            )
            self.notice = "Fix applied and required checks passed. Preview your book, then download or send it."
        elif action == "typography-trial":
            from emend.typography import TypographySettings, typography_trial

            line, gap = value(form, "line_height"), value(form, "paragraph_gap")
            if line or gap:
                settings = TypographySettings(
                    line_height=float(line) if line else None,
                    paragraph_gap=float(gap) if gap else None,
                )
            else:
                preset = value(form, "spacing_preset")
                if preset not in SPACING_PRESETS:
                    raise ValueError(
                        "Choose a spacing preset, or enter a custom line or paragraph spacing value."
                    )
                line, gap = SPACING_PRESETS[preset]
                settings = TypographySettings(line_height=line, paragraph_gap=gap)
            typography_trial(
                book, self.config, settings, max_documents=3, layout=value(form, "layout", "scroll")
            )
        elif action == "apply-typography":
            from emend.typography_apply import apply_typography

            apply_typography(
                book,
                self.config,
                trial_id=value(form, "trial"),
                approve=True,
                all_spine=value(form, "all_spine") == "yes",
            )
            self.notice = "Spacing applied and required checks passed. The comparison is kept below for reference."
        elif action == "research" or action == "choose-edition":
            from emend.research.service import research

            research(
                book,
                self.config,
                edition_id=value(form, "edition") or None,
                discovery_id=value(form, "discovery") or None,
                isbn=value(form, "isbn") or None,
            )
        elif action == "discover":
            from emend.research.discovery import discover

            discover(
                book,
                self.config,
                title=value(form, "title") or None,
                author=value(form, "author") or None,
            )
        elif action == "apply-metadata":
            from emend.curation import apply_proposals

            if not form.get("proposal"):
                raise ValueError("Select at least one suggested change before saving book details.")
            apply_proposals(
                book, self.config, plan_id=value(form, "plan"), approve=form.get("proposal", [])
            )
            self.notice = "Book details saved and required checks passed."
        elif action == "cover-review":
            from emend.covers import cover_review

            cover_review(book, self.config, plan_id=value(form, "plan"))
        elif action == "apply-cover":
            from emend.cover_apply import apply_cover

            apply_cover(
                book,
                self.config,
                review_id=value(form, "review"),
                approve=value(form, "candidate"),
                allow_smaller=value(form, "allow_smaller") == "yes",
                allow_aspect_change=value(form, "allow_aspect_change") == "yes",
            )
            self.notice = "Cover updated in the EPUB and required checks passed."
        elif action == "create-cover":
            from emend.cover_create import create_cover

            create_cover(
                book,
                self.config,
                review_id=value(form, "review"),
                approve=value(form, "candidate"),
                allow_small=value(form, "allow_small") == "yes",
            )
            self.notice = "Cover added to the EPUB and required checks passed."
        elif action == "ai-review":
            from emend.research.ai_review import review_plan

            review_plan(book, self.config, plan_id=value(form, "plan"))
        elif action == "reader-export":
            from emend.reader_export import export_reader

            if not self.reader_directory:
                raise ValueError(
                    "Start the workspace with --reader-directory for mounted-folder delivery."
                )
            result = export_reader(
                book,
                self.reader_directory,
                self.config,
                filename=value(form, "filename") or None,
                expected_artifact_sha256=digest,
            )
            self.notice = f"EPUB bytes verified: {result['receipt']['directory']}/{result['receipt']['filename']}. Use the operating system's safe eject before disconnecting."
        elif action == "publish":
            from emend.publish import publish

            results = publish(
                book, [value(form, "target")], self.config, expected_artifact_sha256=digest
            )
            pending = [r for r in results.values() if r["status"] == "pending"]
            if pending:
                reason = str(
                    pending[0].get("receipt", {}).get("error", "The transfer did not finish.")
                )[:1200]
                if "another calibre program" in reason.lower():
                    raise ValueError(
                        "Calibre is open, so direct import could not run. Keep it open and use Download EPUB, then add the file through Calibre’s Add books. Or close Calibre and retry Add to Calibre here."
                    )
                raise ValueError(f"The transfer did not finish: {reason}")
            self.notice = (
                "Book sent successfully."
                if any(r["status"] == "published" for r in results.values())
                else "This version was already sent."
            )
        else:
            raise ValueError("Unsupported review action.")
        panel, anchor = ACTION_PANELS.get(action, ("", ""))
        return f"/books/{book}" + (
            f"?panel={panel}#{anchor}" if panel else "#preview" if action == "visual" else ""
        )


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, config: Config, port=0, reader_directory=None):
        self.app = ReviewApplication(config, reader_directory)
        super().__init__(("127.0.0.1", port), ReviewHandler)

    @property
    def entry_url(self):
        return f"http://127.0.0.1:{self.server_port}/?" + urlencode({"token": self.app.token})


class ReviewHandler(BaseHTTPRequestHandler):
    server: ReviewServer

    def log_message(self, *args):
        pass  # Do not log session tokens, metadata or local paths.

    def send(self, status, body="", *, location=None, cookie=False, evidence=False):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer" if evidence else "same-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            ("sandbox; " if evidence else "")
            + "default-src 'none'; img-src data:; font-src data:; style-src 'unsafe-inline'; frame-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'self'",
        )
        if location:
            self.send_header("Location", location)
        if cookie:
            self.send_header(
                "Set-Cookie",
                f"emend_session={self.server.app.token}; HttpOnly; SameSite=Strict; Path=/",
            )
        self.end_headers()
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def host_ok(self):
        return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

    def authorized(self):
        try:
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            token = cookies.get("emend_session")
            return bool(token and same_token(token.value, self.server.app.token))
        except (CookieError, TypeError):
            return False

    def error(self, status, message):
        self.send(
            status,
            self.server.app.page(
                "Review request",
                f'<h1>Review request</h1><p class="error">{esc(message)}</p><p><a href="/">Return to library</a></p>',
            ),
        )

        # A rejected POST may still have unread body bytes. Send a FIN before a
        # bounded drain so closing does not reset an already-sent error response.
        try:
            self.connection.shutdown(socket.SHUT_WR)
            self.connection.settimeout(0.05)
            remaining = 1024**2
            while remaining > 0:
                chunk = self.connection.recv(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass

    def action_error(self, status, message, form):
        try:
            book = value(form or {}, "book")
            if not book:
                raise ValueError("No book context")
            panel, _ = ACTION_PANELS.get(value(form, "action"), ("", ""))
            page = self.server.app.book(book, error=message, panel=panel)
        except (ValueError, OSError, KeyError, TypeError):
            self.error(status, message)
        else:
            self.send(status, page)

    def download_epub(self, book):
        from emend.publish import approved_artifact
        from emend.reader_export import default_filename
        from emend.revalidation import snapshot

        app = self.server.app
        with tempfile.TemporaryDirectory(dir=app.config.root / ".staging") as staging:
            target = Path(staging) / "approved.epub"
            with app.store.locked():
                row, report, source = approved_artifact(app.store, book, app.config)
                snapshot(source, target, app.config.limits.max_archive_mb * 1024**2)
                if sha256_file(target) != report.artifact_sha256:
                    raise ValueError(
                        "The EPUB changed while preparing the download. Run checks again."
                    )
                filename = default_filename(report)
            try:
                self.connection.settimeout(30)
                self.send_response(200)
                self.send_header("Content-Type", "application/epub+zip")
                self.send_header("Content-Length", str(target.stat().st_size))
                self.send_header(
                    "Content-Disposition",
                    f"attachment; filename=emend-{row['id'][:12]}.epub; filename*=UTF-8''{quote(filename, safe='')}",
                )
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                with target.open("rb") as stream:
                    while chunk := stream.read(64 * 1024):
                        self.wfile.write(chunk)
            except (OSError, TimeoutError):
                self.close_connection = True

    def do_GET(self):
        if not self.host_ok():
            self.error(403, "Use the local address printed by Emend.")
            return
        url = urlsplit(self.path)
        if url.scheme or url.netloc:
            self.error(400, "Invalid request path.")
            return
        try:
            query = parse_qs(url.query, max_num_fields=16)
            token = value(query, "token")
        except ValueError:
            self.error(400, "Invalid query fields.")
            return
        if url.path == "/" and token and same_token(token, self.server.app.token):
            self.send(303, location="/", cookie=True)
            return
        if not self.authorized():
            self.error(401, "Open the session URL printed by Emend in the terminal.")
            return
        try:
            if url.path == "/":
                offset = int(value(query, "offset", "0"))
                if not 0 <= offset <= 1000000:
                    raise ValueError("Invalid page offset.")
                self.send(200, self.server.app.library(offset))
                return
            parts = url.path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "books" and parts[2] == "download":
                try:
                    self.download_epub(parts[1])
                except (ValueError, OSError) as exc:
                    self.action_error(400, str(exc)[:2000], {"book": [parts[1]]})
                return
            if len(parts) == 2 and parts[0] == "books":
                self.send(200, self.server.app.book(parts[1], panel=value(query, "panel")))
                return
            elif len(parts) == 2 and parts[0] == "recovery":
                self.send(200, self.server.app.recovery(parts[1]))
                return
            if len(parts) == 4 and parts[0] == "evidence" and parts[2] in EVIDENCE:
                _, book, kind, identifier = parts
                row = self.server.app.store.resolve(book)
                book = row["id"]
                load_evidence(self.server.app.store, book, kind, identifier)
                record = self.server.app.store.document(book, kind, identifier)
                path = (
                    Path(record["path"]).parent / "review.html"
                    if kind == "repairs"
                    else Path(record["path"]).with_suffix(".html")
                )
                if path.stat().st_size > 140 * 1024**2:
                    raise ValueError("Review HTML exceeds serving limit.")
                self.send(200, path.read_bytes(), evidence=True)
                return
            self.error(404, "Review page not found.")
        except (ValueError, OSError, KeyError, TypeError) as exc:
            self.error(400, str(exc)[:2000])

    def do_POST(self):
        if (
            not self.host_ok()
            or self.headers.get("Origin") != f"http://127.0.0.1:{self.server.server_port}"
            or not self.authorized()
        ):
            self.error(403, "This action requires the local authenticated review session.")
            return
        if self.path != "/action":
            self.error(404, "Action not found.")
            return
        content_type = self.headers.get("Content-Type", "")
        multipart = content_type.lower().startswith("multipart/form-data;")
        limit = (
            min(self.server.app.config.limits.max_archive_mb, EPUB_UPLOAD_MAX_MIB) * 1024**2 + 65536
            if multipart
            else 65536
        )
        form = None
        try:
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("Chunked requests are not supported.")
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= limit:
                raise ValueError("Request exceeds the local upload/form limit.")
            self.connection.settimeout(30)
            body = self.rfile.read(size)
            if len(body) != size:
                raise ValueError("Incomplete request body.")
            upload = None
            if multipart:
                message = BytesParser(policy=policy.default).parsebytes(
                    b"Content-Type: "
                    + content_type.encode()
                    + b"\r\nMIME-Version: 1.0\r\n\r\n"
                    + body
                )
                parts = list(message.iter_parts())
                if not message.is_multipart() or len(parts) > 8:
                    raise ValueError("Invalid multipart upload.")
                form = {}
                for part in parts:
                    name = part.get_param("name", header="content-disposition")
                    data = part.get_payload(decode=True)
                    if not name or data is None:
                        raise ValueError("Invalid multipart field.")
                    if name in {"epub", "cover"}:
                        if upload:
                            raise ValueError("Upload one file at a time.")
                        filename = (
                            (part.get_filename() or "upload.bin").replace("\\", "/").split("/")[-1]
                        )
                        if (
                            len(filename) > 200
                            or any(ord(c) < 32 for c in filename)
                            or Path(filename).suffix.lower()
                            not in ({".epub"} if name == "epub" else {".jpg", ".jpeg", ".png"})
                        ):
                            raise ValueError("Choose a supported file with a simple filename.")
                        if (
                            len(data)
                            > (
                                min(
                                    self.server.app.config.limits.max_archive_mb,
                                    EPUB_UPLOAD_MAX_MIB,
                                )
                                if name == "epub"
                                else 20
                            )
                            * 1024**2
                        ):
                            raise ValueError(
                                "EPUB exceeds the upload size limit; use the inbox/CLI."
                            )
                        upload = (name, filename, data)
                    else:
                        if len(data) > 4096:
                            raise ValueError("Form field is too long.")
                        form.setdefault(name, []).append(data.decode("utf-8"))
            elif content_type.split(";")[0] == "application/x-www-form-urlencoded":
                form = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=256)
            else:
                raise ValueError("Unsupported form content type.")
            if not same_token(value(form, "csrf"), self.server.app.csrf):
                self.error(403, "Invalid or expired form token.")
                return
            if not self.server.app.operation.acquire(blocking=False):
                self.error(409, "Another review action is running. Wait for it to finish.")
                return
            try:
                if upload:
                    self.server.app.notice = ""
                    action = value(form, "action")
                    if (upload[0], action) not in {("epub", "import"), ("cover", "cover-local")}:
                        raise ValueError("Unsupported upload action.")
                    with tempfile.TemporaryDirectory(
                        dir=self.server.app.config.root / ".staging"
                    ) as staging:
                        path = Path(staging) / upload[1]
                        path.write_bytes(upload[2])
                        if action == "import":
                            book = ingest_new(path, self.server.app.config).book_id
                        else:
                            from emend.covers import cover_review

                            row, _, digest = current_source(
                                self.server.app.store, value(form, "book"), self.server.app.config
                            )
                            book = row["id"]
                            if value(form, "artifact") != digest:
                                raise ValueError("This cover page is stale; reopen the book.")
                            cover_review(
                                book,
                                self.server.app.config,
                                image=path,
                                source_note=value(form, "source_note"),
                            )
                    location = f"/books/{book}" + (
                        "?panel=details#cover-review" if action == "cover-local" else ""
                    )
                else:
                    location = self.server.app.execute(form)
            finally:
                self.server.app.operation.release()
            self.send(303, location=location)
        except OperationBusyError as exc:
            self.action_error(409, str(exc), form)
        except (ValueError, OSError, KeyError, TypeError, TimeoutError) as exc:
            self.action_error(400, str(exc)[:2000], form)
