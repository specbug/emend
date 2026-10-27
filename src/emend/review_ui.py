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
from urllib.parse import parse_qs, urlencode, urlsplit

from emend.config import Config
from emend.pipeline import get_report, ingest_new
from emend.reports import report_styles
from emend.research.discovery import current_source
from emend.storage import OperationBusyError, Store

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

    def page(self, title, body):
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)} · Emend</title><style>{report_styles()}
form {{margin:1rem 0 2rem}} label {{display:block;margin:.5rem 0}} input,select,button {{font:inherit;max-width:100%;padding:.5rem .75rem;border:1px solid var(--rule);border-radius:2px;background:transparent;color:inherit}}
input[type=checkbox],input[type=radio] {{margin-right:.5rem}} button {{cursor:pointer;background:var(--ink);color:var(--paper);margin-top:.5rem}} button:disabled {{opacity:.5;cursor:default}}
fieldset {{border:1px solid var(--rule);padding:1rem;margin:1rem 0}} legend {{padding:0 .5rem}} section {{border-bottom:1px solid var(--rule);padding-bottom:1rem}} .error {{color:var(--signal)}} .quiet {{color:var(--muted)}} .controls {{display:flex;gap:1rem;flex-wrap:wrap;align-items:end}} .controls form {{margin-bottom:1rem}}
a,p,summary {{overflow-wrap:anywhere}} iframe {{width:100%;height:40rem;border:1px solid var(--rule);margin:1rem 0}} details {{margin:1rem 0}} pre {{white-space:pre-wrap;overflow-wrap:anywhere}} nav {{margin-bottom:2rem}}
</style></head><body><main><nav><a href="/">Emend library</a></nav>{f'<p role="status">{esc(self.notice)}</p>' if self.notice else ""}{body}<footer>Local review workspace. Originals are preserved; edits and publishing stay explicit. Close the server to end this session.</footer></main></body></html>"""

    def form(self, action, label, fields="", *, book="", digest="", hidden=None):
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
        return f'<form method="post" action="/action">{inputs}{fields}<button type="submit">{esc(label)}</button></form>'

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
            items.append(
                f'<tr><th><a href="/books/{row["id"]}">{esc(title[:2000])}</a></th><td>{esc(row["state"])}</td><td><code>{row["id"][:12]}</code></td></tr>'
            )
        upload = f'''<form method="post" action="/action" enctype="multipart/form-data"><input type="hidden" name="csrf" value="{self.csrf}"><input type="hidden" name="action" value="import"><label>Choose an EPUB <input type="file" name="epub" accept=".epub,application/epub+zip" required></label><p class="quiet">Up to 64 MiB here. Use the inbox/CLI for larger files. Known books are left unchanged.</p><button>Import EPUB</button></form>'''
        paging = (
            f'<a href="/?offset={max(0, offset - 100)}">Previous books</a> ' if offset else ""
        ) + (f'<a href="/?offset={offset + 100}">Next books</a>' if len(rows) > 100 else "")
        backup = ""
        if self.config.publish.calibre_library:
            fields = '<label><input type="checkbox" name="closed" value="yes" required>Calibre and other library writers are closed.</label>'
            if self.config.publish.backup_remote:
                fields += '<label><input type="checkbox" name="upload" value="yes">Upload the verified complete-library snapshot to the configured Drive remote.</label>'
            backup = (
                "<section><h2>Back up the Calibre library</h2><p>Includes the whole rich library. Quit Calibre and its content server before continuing.</p>"
                + self.form("backup", "Create verified backup", fields)
                + "</section>"
            )
        return self.page(
            "Library",
            f'<header><div class="brand">EMEND / REVIEW</div><h1>Your library</h1><p>Inspect evidence, review proposed changes, and publish when the required checks pass.</p></header><section><h2>Import a book</h2>{upload}</section><section><h2>Books</h2><p class="quiet">Recorded states are a snapshot. Opening a book checks its current identity.</p><table><thead><tr><th>Book</th><th>Recorded state</th><th>Identity</th></tr></thead><tbody>{"".join(items)}</tbody></table>{paging if items else "<p>No books yet. Import an EPUB above.</p>"}</section>{backup}',
        )

    def book(self, book):
        saved = self.store.resolve(book)
        if not saved["report_path"] or Path(saved["report_path"]).stat().st_size > 8 * 1024**2:
            raise ValueError(
                "No bounded completed report is available; inspect/recover this book using the CLI."
            )
        row, _, digest = current_source(self.store, book, self.config)
        book = row["id"]
        report = get_report(self.store, book)
        title = "; ".join(report.inspection.metadata.get("title", [])) or row["source_name"]
        author = "; ".join(report.inspection.metadata.get("creator", []))

        def form(action, label, fields="", hidden=None):
            return self.form(action, label, fields, book=book, digest=digest, hidden=hidden)

        gates = "".join(
            f"<li><strong>{esc(g.name)}: {esc(g.status)}</strong> · {'required' if g.required else 'advisory'}<p>{esc(g.detail)}</p></li>"
            for g in report.gates
        )
        body = (
            f'<header><div class="brand">EMEND / BOOK REVIEW</div><h1>{esc(title)}</h1><p>{esc(author)}</p><p>Recorded state: {esc(row["state"])}</p><code>Current artifact {digest}</code></header><section><h2>Quality checks</h2><ul>{gates}</ul>'
            + form("revalidate", "Revalidate current EPUB")
            + "</section>"
        )
        inspection_section = (
            '<section><h2>Inspect layout and reading details</h2><p>These reviews are advisory. They do not change the EPUB or approve it for a device.</p><div class="controls">'
            + form("visual", "Capture visual review")
            + form("reading", "Review fonts and marked notes")
            + form("repair-plan", "Prepare supported repair plan")
            + "</div></section>"
        )
        typography_section = (
            "<section><h2>Try typography</h2><p>Browser-only trial. Leave a setting blank to retain its current behavior; fonts and alignment stay authored. Inspect verse, notes and captions.</p>"
            + form(
                "typography-trial",
                "Compare current and trial styling",
                '<label>Line height, unitless <input type="number" name="line_height" min="1" max="2.4" step="0.05"></label><label>Paragraph gap, em <input type="number" name="paragraph_gap" min="0" max="2" step="0.05"></label><label>Preview layout <select name="layout"><option value="scroll">Scrolling</option><option value="paginated">Paginated</option></select></label>',
            )
            + "</section>"
        )
        body += (
            '<section><h2>Metadata and edition evidence</h2><p>Lookups send selected bibliographic metadata to public providers, never EPUB text. Inspect edition evidence before choosing changes.</p><div class="controls">'
            + form(
                "research",
                "Research existing ISBN",
                '<label>ISBN override, optional <input name="isbn" maxlength="32"></label>',
            )
            + form(
                "discover",
                "Find candidate editions",
                '<label>Title override, optional <input name="title" maxlength="2000"></label><label>Author override, optional <input name="author" maxlength="2000"></label>',
            )
            + "</div>"
        )
        try:
            from emend.research.discovery import load_discovery

            discovery = load_discovery(self.store, book)
            current = discovery.artifact_sha256 == digest
            options = "".join(
                f'<label><input type="radio" name="edition" value="{esc(c.edition.record_id)}" required>{esc(c.edition.title)} · {esc("; ".join(c.edition.creators))} · {esc("; ".join(c.edition.language))} · {esc("; ".join(c.edition.publisher))} · {esc("; ".join(c.edition.date))}<p class="quiet">{esc("; ".join(c.matches + c.differences))}</p></label>'
                for c in discovery.candidates
            )
            body += f"<p>Edition discovery: {'current' if current else 'stale; run discovery again'}.</p>"
            if current and options:
                body += form(
                    "choose-edition",
                    "Prepare proposals for selected edition",
                    options,
                    {"discovery": discovery.id},
                )
        except (ValueError, OSError):
            pass
        try:
            from emend.research.service import load_plan

            plan = load_plan(self.store, book)
            current = plan.artifact_sha256 == digest
            body += f"<p>Metadata plan <code>{esc(plan.id)}</code>: {'current' if current else 'stale; research again'}.</p>"
            if current:
                options = "".join(
                    f'<label><input type="checkbox" name="proposal" value="{esc(p.id)}"><strong>{esc(p.field)}</strong>: {esc("; ".join(p.before) or "(missing)")} → {esc("; ".join(p.after))}<p class="quiet">{esc(p.reason)} · {esc(p.decision)}</p></label>'
                    for p in plan.proposals
                )
                if options:
                    body += form(
                        "apply-metadata",
                        "Apply selected reviewed metadata",
                        options,
                        {"plan": plan.id},
                    )
                body += form(
                    "cover-review", "Compare recorded cover candidates", hidden={"plan": plan.id}
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
            body += '<p class="quiet">No usable saved metadata plan.</p>'
        body += f'<h3>Compare a local cover</h3><form method="post" action="/action" enctype="multipart/form-data"><input type="hidden" name="csrf" value="{self.csrf}"><input type="hidden" name="action" value="cover-local"><input type="hidden" name="book" value="{book}"><input type="hidden" name="artifact" value="{digest}"><label>JPEG or PNG cover, up to 20 MiB <input type="file" name="cover" accept="image/jpeg,image/png" required></label><label>Source/provenance note <input name="source_note" maxlength="2000" required></label><button>Compare local cover</button></form>'
        body += (
            "</section>"
            + inspection_section
            + typography_section
            + "<section><h2>Saved review evidence</h2>"
        )
        with self.store.db() as db:
            records = db.execute(
                "SELECT * FROM review_documents WHERE book_id=? ORDER BY created_at DESC LIMIT 100",
                (book,),
            ).fetchall()
        seen = set()
        for record in records:
            kind = record["kind"]
            if kind not in EVIDENCE or kind in seen:
                continue
            seen.add(kind)
            try:
                evidence = load_evidence(self.store, book, kind, record["id"])
                identity = getattr(
                    evidence, "artifact_sha256", getattr(evidence, "input_sha256", None)
                )
                current = identity == digest
                status = getattr(
                    evidence,
                    "status",
                    "complete" if getattr(evidence, "complete", True) else "incomplete",
                )
                url = f"/evidence/{book}/{kind}/{record['id']}"
                body += f'<h3>{esc(kind.replace("-", " ").title())}</h3><p>{"Current artifact" if current else "Stale artifact; capture new evidence"} · {esc(status)} · <code>{esc(record["id"])}</code></p><details><summary>Open saved review</summary><iframe sandbox loading="lazy" src="{url}" title="{esc(kind)}"></iframe><p><a href="{url}" target="_blank" rel="noopener noreferrer">Open full review</a></p></details>'
                if current and kind == "repairs" and evidence.actions:
                    options = "".join(
                        f'<label><input type="checkbox" name="approved_action" value="{esc(a)}" required>{esc(a)}: {esc(evidence.detail)}</label>'
                        for a in evidence.actions
                    )
                    body += form(
                        "apply-repairs",
                        "Apply selected reviewed repair",
                        options,
                        {"plan": evidence.id},
                    )
                elif current and kind == "typography-trial" and evidence.status == "complete":
                    body += form(
                        "apply-typography",
                        "Apply reviewed trial CSS",
                        '<p>Default: sampled documents only. Read the comparison and CSS first.</p><label><input type="checkbox" name="all_spine" value="yes">Extend to all XHTML spine documents, including unsampled chapters.</label>',
                        {"trial": evidence.id},
                    )
                elif (
                    current and kind == "cover-review" and evidence.original and evidence.candidates
                ):
                    choices = "".join(
                        f'<label><input type="radio" name="candidate" value="{esc(c.id)}" required>{esc(c.id)} · {c.width} × {c.height} · {esc(c.source)}</label>'
                        for c in evidence.candidates
                    )
                    choices += '<p>Exceptions below are recorded. Review image resolution and existing cover-page fit first.</p><label><input type="checkbox" name="allow_smaller" value="yes">Allow a smaller source image.</label><label><input type="checkbox" name="allow_aspect_change" value="yes">Allow an aspect-ratio change.</label>'
                    body += form(
                        "apply-cover", "Apply reviewed cover", choices, {"review": evidence.id}
                    )
            except (ValueError, OSError) as exc:
                body += (
                    f'<p class="error">{esc(kind)} evidence unavailable: {esc(str(exc)[:500])}</p>'
                )
        body += "</section><section><h2>Publish approved EPUB</h2><p>Publishing rechecks required gates and current bytes. Delivery is separate from review and editing.</p>"
        for target, label in [
            ("calibre", "Publish to configured Calibre library"),
            ("drive", "Export EPUB to configured Drive remote"),
        ]:
            configured = (
                self.config.publish.calibre_library
                if target == "calibre"
                else self.config.publish.drive_remote
            )
            if configured:
                body += form("publish", label, hidden={"target": target})
        if not self.config.publish.calibre_library and not self.config.publish.drive_remote:
            body += '<p class="quiet">No publishing destination configured. Add it to Emend configuration when ready.</p>'
        if self.reader_directory:
            body += (
                f"<h3>Reader folder</h3><p>Selected folder: <code>{esc(self.reader_directory)}</code>. Confirm the intended reader/card is mounted. Export verifies files, not hardware or firmware compatibility. Nothing is overwritten; older revisions stay available.</p>"
                + form(
                    "reader-export",
                    "Export approved EPUB to reader folder",
                    '<label>Filename override, optional <input name="filename" maxlength="240"></label>',
                )
            )
        with self.store.db() as db:
            receipts = db.execute(
                "SELECT status,receipt FROM publications WHERE book_id=? ORDER BY updated_at DESC LIMIT 5",
                (book,),
            ).fetchall()
        if receipts:
            body += "<h3>Recent delivery receipts</h3>" + "".join(
                f"<details><summary>{esc(r['status'])}</summary><pre>{esc(r['receipt'][:4000])}</pre></details>"
                for r in receipts
            )
        body += "</section>"
        return self.page(title, body)

    def execute(self, form):
        action = value(form, "action")
        book = value(form, "book")
        if action == "backup":
            from emend.backup import backup_library

            if value(form, "closed") != "yes":
                raise ValueError("Confirm Calibre and other library writers are closed.")
            result = backup_library(self.config, True, upload=value(form, "upload") == "yes")
            self.notice = f"Verified backup saved: {result['archive']}. Cloud status: {result.get('cloud_status', 'local only')}."
            return "/"
        row, _, digest = current_source(self.store, book, self.config)
        book = row["id"]
        if value(form, "artifact") != digest:
            raise ValueError("This page is stale; reopen the book before taking action.")
        if action == "revalidate":
            from emend.revalidation import revalidate

            revalidate(book, self.config)
        elif action == "reading":
            from emend.reading_review import reading_review

            reading_review(book, self.config)
        elif action == "visual":
            from emend.visual import visual_review

            visual_review(book, self.config, max_documents=3)
        elif action == "repair-plan":
            from emend.repairs import prepare_repairs

            prepare_repairs(book, self.config)
        elif action == "apply-repairs":
            from emend.repairs import apply_repairs

            apply_repairs(
                book,
                self.config,
                plan_id=value(form, "plan"),
                approve=form.get("approved_action", []),
            )
        elif action == "typography-trial":
            from emend.typography import TypographySettings, typography_trial

            settings = TypographySettings(
                line_height=float(value(form, "line_height"))
                if value(form, "line_height")
                else None,
                paragraph_gap=float(value(form, "paragraph_gap"))
                if value(form, "paragraph_gap")
                else None,
            )
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

            apply_proposals(
                book, self.config, plan_id=value(form, "plan"), approve=form.get("proposal", [])
            )
        elif action == "cover-review":
            from emend.covers import cover_review

            cover_review(book, self.config, plan_id=value(form, "plan"))
        elif action == "apply-cover":
            from emend.cover_apply import apply_cover

            apply_cover(
                book, self.config, review_id=value(form, "review"), approve=value(form, "candidate")
            )
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
            if any(r["status"] == "pending" for r in results.values()):
                raise ValueError(
                    "Delivery remains pending; inspect its receipt/CLI output before retrying."
                )
        else:
            raise ValueError("Unsupported review action.")
        return f"/books/{book}"


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
            if len(parts) == 2 and parts[0] == "books":
                self.send(200, self.server.app.book(parts[1]))
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
            min(self.server.app.config.limits.max_archive_mb, 64) * 1024**2 + 65536
            if multipart
            else 65536
        )
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
                                min(self.server.app.config.limits.max_archive_mb, 64)
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
                    location = f"/books/{book}"
                else:
                    location = self.server.app.execute(form)
            finally:
                self.server.app.operation.release()
            self.send(303, location=location)
        except OperationBusyError as exc:
            self.error(409, str(exc))
        except (ValueError, OSError, KeyError, TypeError, TimeoutError) as exc:
            self.error(400, str(exc)[:2000])
