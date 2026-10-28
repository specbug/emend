"""Local-only review workspace. Mutations use the existing explicit, gated APIs.

Importing a book starts one background run: keep and check the original, apply
supported repairs that pass their own preservation and validation gates, then
render reader pages. Everything after that (spacing, details, covers, sending)
stays an explicit action.
"""

import hashlib
import hmac
import json
import secrets
import shutil
import socket
import tempfile
import threading
from email import policy
from email.parser import BytesParser
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from emend import workspace_runs as runs
from emend import workspace_views as views
from emend.config import Config
from emend.inspect import sha256_file
from emend.pipeline import get_report
from emend.research.discovery import current_source
from emend.review_guidance import ACTION_STEPS, SPACING_PRESETS
from emend.storage import OperationBusyError, Store
from emend.workspace_views import EVIDENCE, esc, load_evidence

EPUB_UPLOAD_MAX_MIB = 128
WORKSPACE_CSP = (
    "default-src 'none'; img-src 'self'; font-src 'self'; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)
EVIDENCE_CSP = (
    "sandbox; default-src 'none'; img-src data:; font-src data:; style-src 'unsafe-inline'; "
    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)
DETAILS = {"research", "discover", "choose-edition", "apply-metadata", "ai-review"}
COVERS = {"cover-review", "apply-cover", "create-cover", "cover-local"}

__all__ = ["EVIDENCE", "ReviewApplication", "ReviewServer", "esc", "load_evidence"]


def same_token(provided, expected):
    return (
        isinstance(provided, str) and provided.isascii() and hmac.compare_digest(provided, expected)
    )


def value(form, key, default=""):
    values = form.get(key, [default])
    if len(values) != 1:
        raise ValueError(f"Provide one value for {key}.")
    return values[0]


def book_location(book, action):
    step = ACTION_STEPS.get(action)
    if action in DETAILS:
        return f"/books/{book}?step=refine&open=details#details"
    if action in COVERS:
        return f"/books/{book}?step=refine&open=details#cover-review"
    return f"/books/{book}" + (f"?step={step}" if step and action != "revalidate" else "")


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
        self.runner = runs.Runner(self.operation)
        self.upload_limit = EPUB_UPLOAD_MAX_MIB
        self.notice = ""
        self.notice_book = None

    def take_notice(self, book=None):
        if self.notice and self.notice_book == book:
            message, self.notice = self.notice, ""
            return message
        run = self.runner.get(book) if book else None
        if run and not run.active and run.notice:
            message, run.notice = run.notice, ""
            return message
        return ""

    def form(
        self,
        action,
        label,
        fields="",
        *,
        book="",
        digest="",
        hidden=None,
        disabled=False,
        cls="btn",
        form_class="",
        working="",
    ):
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
        return (
            f'<form method="post" action="/action"{f" class={chr(34)}{esc(form_class)}{chr(34)}" if form_class else ""}>'
            f'{inputs}{fields}<button class="{esc(cls)}" type="submit"'
            f"{f' data-working={chr(34)}{esc(working)}{chr(34)}' if working else ''}"
            f"{' disabled' if disabled else ''}>{esc(label)}</button></form>"
        )

    def library(self, offset=0, error=None):
        return views.library(self, offset, error=error)

    def book(self, book, error=None, step=None, opened=""):
        return views.book_page(self, book, error=error, step=step, opened=opened)

    def recovery(self, book, reason=""):
        return views.recovery(self, book, reason)

    def status(self):
        return {"active": self.runner.active is not None, "version": self.runner.version}

    def start(self, book, kind, steps, work, title=None):
        """Hand the held operation lock to a background run."""
        if title is None:
            try:
                report = get_report(self.store, book)
                title = "; ".join(report.inspection.metadata.get("title", [])) or report.source_name
            except (ValueError, OSError):
                title = book[:12]
        self.runner.start(book, title, kind, steps, work)

    def start_import(self, filename, data):
        digest = hashlib.sha256(data).hexdigest()
        staging = Path(tempfile.mkdtemp(dir=self.config.root / ".staging"))
        path = staging / filename
        path.write_bytes(data)

        def work(run):
            try:
                runs.import_book(run, self.config, path)
            finally:
                shutil.rmtree(staging, ignore_errors=True)

        self.start(digest, "import", runs.STEPS_IMPORT, work, title=Path(filename).stem)
        return f"/books/{digest}"

    def execute(self, form):
        """Return (location, started). A started run owns the operation lock."""
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
            return "/", False
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
            return "/books/" + self.store.resolve(book)["id"], False
        row, _, digest = current_source(self.store, book, self.config)
        book = row["id"]
        self.notice_book = book
        if value(form, "artifact") != digest:
            raise ValueError("This page is stale; reopen the book before taking action.")
        config = self.config
        location = book_location(book, action)
        if action == "revalidate":
            self.start(
                book, "recheck", runs.STEPS_RECHECK, lambda r: runs.recheck_book(r, config, book)
            )
            return location, True
        if action == "visual":
            layout = value(form, "layout", "paginated")
            if layout not in {"scroll", "paginated"}:
                raise ValueError("Layout must be scroll or paginated.")
            self.start(
                book,
                "preview",
                [("preview", "Render reader pages")],
                lambda r: runs.preview_only(r, config, book, layout),
            )
            return location, True
        if action == "typography-trial":
            from emend.typography import TypographySettings

            line, gap = value(form, "line_height"), value(form, "paragraph_gap")
            preset = value(form, "spacing_preset")
            if preset in SPACING_PRESETS:
                line, gap = SPACING_PRESETS[preset]
                settings = TypographySettings(line_height=line, paragraph_gap=gap)
            elif preset in {"", "custom"} and (line or gap):
                settings = TypographySettings(
                    line_height=float(line) if line else None,
                    paragraph_gap=float(gap) if gap else None,
                )
            else:
                raise ValueError(
                    "Choose a spacing preset, or enter a custom line or paragraph spacing value."
                )
            layout = value(form, "layout", "paginated")
            if layout not in {"scroll", "paginated"}:
                raise ValueError("Layout must be scroll or paginated.")
            self.start(
                book,
                "trial",
                [("trial", "Render baseline and adjusted pages")],
                lambda r: runs.spacing_trial(r, config, book, settings, layout),
            )
            return location, True
        if action == "apply-typography":
            trial, all_spine = value(form, "trial"), value(form, "all_spine") == "yes"
            self.start(
                book,
                "apply",
                [("apply", "Apply spacing and check the book"), ("preview", "Render reader pages")],
                lambda r: runs.apply_spacing(r, config, book, trial, all_spine),
            )
            return location, True
        if action == "text-size-trial":
            layout = value(form, "layout", "paginated")
            if layout not in {"scroll", "paginated"}:
                raise ValueError("Layout must be scroll or paginated.")
            self.start(
                book,
                "trial",
                [("trial", "Render the book as published and at your reader's size")],
                lambda r: runs.size_trial(r, config, book, layout),
            )
            return location, True
        if action == "image-conversion-trial":
            layout = value(form, "layout", "paginated")
            if layout not in {"scroll", "paginated"}:
                raise ValueError("Layout must be scroll or paginated.")
            self.start(
                book,
                "trial",
                [("trial", "Compare image formats")],
                lambda r: runs.image_trial(r, config, book, layout),
            )
            return location, True
        if action == "apply-image-conversion":
            trial = value(form, "trial")
            self.start(
                book,
                "apply",
                [
                    ("apply", "Convert images and check the book"),
                    ("preview", "Render reader pages"),
                ],
                lambda r: runs.apply_images(r, config, book, trial),
            )
            return location, True
        if action == "apply-text-size":
            trial = value(form, "trial")
            self.start(
                book,
                "apply",
                [
                    ("apply", "Reset body-text size and check the book"),
                    ("preview", "Render reader pages"),
                ],
                lambda r: runs.apply_size(r, config, book, trial),
            )
            return location, True
        if action == "restore-baseline":
            revision, expected = value(form, "revision"), value(form, "expected_report")
            self.start(
                book,
                "restore",
                [
                    ("restore", "Restore the baseline and check it"),
                    ("preview", "Render reader pages"),
                ],
                lambda r: runs.restore(r, config, book, revision, expected),
            )
            return location, True
        if action == "reading":
            from emend.reading_review import reading_review

            reading_review(book, config)
            self.notice = "Font and footnote report saved."
        elif action == "research" or action == "choose-edition":
            from emend.research.service import research

            research(
                book,
                config,
                edition_id=value(form, "edition") or None,
                discovery_id=value(form, "discovery") or None,
                isbn=value(form, "isbn") or None,
            )
            self.notice = "Suggestions gathered. Nothing changes until you save them."
        elif action == "discover":
            from emend.research.discovery import discover

            discover(
                book,
                config,
                title=value(form, "title") or None,
                author=value(form, "author") or None,
            )
            self.notice = "Edition search finished."
        elif action == "apply-metadata":
            from emend.curation import apply_proposals

            if not form.get("proposal"):
                raise ValueError("Select at least one suggested change before saving book details.")
            apply_proposals(
                book, config, plan_id=value(form, "plan"), approve=form.get("proposal", [])
            )
            self.notice = "Book details saved and required checks passed."
        elif action == "cover-review":
            from emend.covers import cover_review

            cover_review(book, config, plan_id=value(form, "plan"))
            self.notice = "Cover candidates gathered. Compare them before applying one."
        elif action == "apply-cover":
            from emend.cover_apply import apply_cover

            apply_cover(
                book,
                config,
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
                config,
                review_id=value(form, "review"),
                approve=value(form, "candidate"),
                allow_small=value(form, "allow_small") == "yes",
            )
            self.notice = "Cover added to the EPUB and required checks passed."
        elif action == "ai-review":
            from emend.research.ai_review import review_plan

            review_plan(book, config, plan_id=value(form, "plan"))
            self.notice = "Meta advice saved. It cannot approve or apply changes."
        elif action == "reader-export":
            from emend.reader_export import export_reader

            if not self.reader_directory:
                raise ValueError(
                    "Start the workspace with --reader-directory for mounted-folder delivery."
                )
            result = export_reader(
                book,
                self.reader_directory,
                config,
                filename=value(form, "filename") or None,
                expected_artifact_sha256=digest,
            )
            self.notice = f"EPUB bytes verified: {result['receipt']['directory']}/{result['receipt']['filename']}. Use the operating system's safe eject before disconnecting."
        elif action == "publish":
            from emend.publish import publish

            results = publish(
                book, [value(form, "target")], config, expected_artifact_sha256=digest
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
        return location, False


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

    def send(
        self,
        status,
        body="",
        *,
        location=None,
        cookie=False,
        evidence=False,
        content_type="text/html; charset=utf-8",
        cache="no-store",
    ):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("Referrer-Policy", "no-referrer" if evidence else "same-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", EVIDENCE_CSP if evidence else WORKSPACE_CSP)
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
        self.send(status, views.error_page("Review request", message))

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
            if value(form or {}, "action", "") == "import":
                self.send(status, self.server.app.library(error=message))
                return
            book = value(form or {}, "book")
            if not book:
                raise ValueError("No book context")
            action = value(form, "action", "")
            page = self.server.app.book(
                book,
                error=message,
                step=ACTION_STEPS.get(action),
                opened="details" if action in DETAILS | COVERS else "",
            )
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
            # Keep the verified snapshot open and remove staging before any byte is sent,
            # so cleanup never races the client finishing the download.
            size = target.stat().st_size
            stream = target.open("rb")
        with stream:
            try:
                self.connection.settimeout(30)
                self.send_response(200)
                self.send_header("Content-Type", "application/epub+zip")
                self.send_header("Content-Length", str(size))
                self.send_header(
                    "Content-Disposition",
                    f"attachment; filename=emend-{row['id'][:12]}.epub; filename*=UTF-8''{quote(filename, safe='')}",
                )
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
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
        parts = url.path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "assets":
            # Package styles, script and licensed fonts only; nothing book-specific.
            try:
                data, kind = views.asset(parts[1])
            except KeyError:
                self.error(404, "Asset not found.")
                return
            self.send(200, data, content_type=kind, cache="private, max-age=86400")
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
            if url.path == "/status":
                self.send(
                    200,
                    json.dumps(self.server.app.status()),
                    content_type="application/json",
                )
                return
            if len(parts) == 3 and parts[0] == "books" and parts[2] == "download":
                try:
                    self.download_epub(parts[1])
                except (ValueError, OSError) as exc:
                    self.action_error(
                        400, str(exc)[:2000], {"book": [parts[1]], "action": ["download"]}
                    )
                return
            if len(parts) == 7 and parts[0] == "books" and parts[2] == "pages":
                _, book, _, kind, identifier, phase, name = parts
                if kind not in {
                    "visual-review",
                    "typography-trial",
                    "text-size-trial",
                    "image-conversion-trial",
                }:
                    raise ValueError("Unsupported page image.")
                book = self.server.app.store.resolve(book)["id"]
                data = views.evidence_page_image(
                    self.server.app.store, book, kind, identifier, phase, name
                )
                self.send(200, data, content_type="image/png")
                return
            if len(parts) == 2 and parts[0] == "books":
                self.send(
                    200,
                    self.server.app.book(
                        parts[1], step=value(query, "step"), opened=value(query, "open")
                    ),
                )
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
        app = self.server.app
        content_type = self.headers.get("Content-Type", "")
        multipart = content_type.lower().startswith("multipart/form-data;")
        upload_mib = min(app.config.limits.max_archive_mb, EPUB_UPLOAD_MAX_MIB)
        limit = upload_mib * 1024**2 + 65536 if multipart else 65536
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
                            raise ValueError(
                                "Choose an EPUB file with a simple filename."
                                if name == "epub"
                                else "Choose a JPEG or PNG file with a simple filename."
                            )
                        if len(data) > (upload_mib if name == "epub" else 20) * 1024**2:
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
            if not same_token(value(form, "csrf"), app.csrf):
                self.error(403, "Invalid or expired form token.")
                return
            if not app.operation.acquire(blocking=False):
                active = app.runner.active
                self.action_error(
                    409,
                    f"Emend is still working on {active.title}. Try again when it finishes."
                    if active
                    else "Another review action is running. Wait for it to finish.",
                    form,
                )
                return
            started = False
            try:
                if upload:
                    app.notice = ""
                    action = value(form, "action")
                    if (upload[0], action) not in {("epub", "import"), ("cover", "cover-local")}:
                        raise ValueError("Unsupported upload action.")
                    if action == "import":
                        location = app.start_import(upload[1], upload[2])
                        started = True
                    else:
                        from emend.covers import cover_review

                        with tempfile.TemporaryDirectory(
                            dir=app.config.root / ".staging"
                        ) as staging:
                            path = Path(staging) / upload[1]
                            path.write_bytes(upload[2])
                            row, _, digest = current_source(
                                app.store, value(form, "book"), app.config
                            )
                            book = row["id"]
                            if value(form, "artifact") != digest:
                                raise ValueError("This cover page is stale; reopen the book.")
                            cover_review(
                                book, app.config, image=path, source_note=value(form, "source_note")
                            )
                        location = book_location(book, "cover-local")
                else:
                    location, started = app.execute(form)
            finally:
                if not started:
                    app.operation.release()
            self.send(303, location=location)
        except OperationBusyError as exc:
            self.action_error(409, str(exc), form)
        except (ValueError, OSError, KeyError, TypeError, TimeoutError) as exc:
            self.action_error(400, str(exc)[:2000], form)
