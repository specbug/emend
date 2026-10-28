"""Background runs for the local workspace.

A run chains the existing gated operations so an imported book is checked,
repaired where a supported repair exists, and previewed without further clicks.
Every step keeps its own preservation checks, validation and rollback.
"""

import threading
import time

from emend.pipeline import KnownBook, get_report, ingest_new
from emend.research.discovery import current_source
from emend.review_guidance import CHECK_NAMES, FIX_NAMES, blockers
from emend.storage import OperationBusyError, Store

PREVIEW_DOCUMENTS = 3
PREVIEW_PROFILES = ["compact"]


class Step:
    def __init__(self, key, label):
        self.key = key
        self.label = label
        self.state = "waiting"  # waiting, running, done, skipped, failed
        self.note = ""
        self.started = None
        self.finished = None

    @property
    def seconds(self):
        if self.started is None:
            return None
        return round((self.finished or time.monotonic()) - self.started)


class Run:
    """In-memory progress for one book. Persistent state stays in the store."""

    def __init__(self, book, title, kind, steps, changed):
        self.book = book
        self.title = title
        self.kind = kind
        self.steps = [Step(key, label) for key, label in steps]
        self.notice = ""
        self.finished = None
        self._changed = changed

    @property
    def active(self):
        return self.finished is None

    @property
    def failed(self):
        return [s for s in self.steps if s.state == "failed"]

    def step(self, key):
        return next(s for s in self.steps if s.key == key)

    def begin(self, key):
        step = self.step(key)
        step.state, step.started = "running", time.monotonic()
        self._changed()

    def end(self, key, state, note=""):
        step = self.step(key)
        if step.started is None:
            step.started = time.monotonic()
        step.state, step.note, step.finished = state, note[:1200], time.monotonic()
        self._changed()

    def skip_rest(self, note=""):
        for step in self.steps:
            if step.state == "waiting":
                step.state, step.note = "skipped", note
        self._changed()

    def close(self):
        for step in self.steps:
            if step.state == "running":
                step.state, step.finished = "failed", time.monotonic()
                step.note = step.note or "This step stopped before it finished."
        self.finished = time.monotonic()
        self._changed()


class Runner:
    """Owns the workspace operation lock while one run works in the background."""

    def __init__(self, operation):
        self.operation = operation
        self.runs = {}
        self.version = 0
        self.thread = None
        self._lock = threading.Lock()

    def changed(self):
        with self._lock:
            self.version += 1

    @property
    def active(self):
        return next((r for r in self.runs.values() if r.active), None)

    def get(self, book):
        return self.runs.get(book)

    def start(self, book, title, kind, steps, work):
        """The caller has acquired the operation lock; the run releases it."""
        run = Run(book, title, kind, steps, self.changed)
        self.runs[book] = run
        self.changed()

        def target():
            try:
                work(run)
            except Exception as exc:  # A run must always finish and report.
                current = next((s for s in run.steps if s.state == "running"), None)
                if current:
                    run.end(current.key, "failed", explain(exc))
                run.skip_rest("Not run because an earlier step stopped.")
            finally:
                run.close()
                self.operation.release()

        self.thread = threading.Thread(target=target, name="emend-run", daemon=True)
        self.thread.start()
        return run

    def wait(self, timeout=None):
        thread = self.thread
        if thread is not None:
            thread.join(timeout)


def explain(exc):
    if isinstance(exc, OperationBusyError):
        return "Another Emend command, such as the CLI or inbox, was running. Run this again when it finishes."
    message = str(exc).strip() or type(exc).__name__
    if not isinstance(exc, (ValueError, OSError)):
        message = f"Unexpected {type(exc).__name__}: {message}"
    return message[:1200]


def check_summary(report, config):
    failed = [
        CHECK_NAMES.get(g.name, g.name) for g in report.gates if g.required and g.status == "failed"
    ]
    missing = [
        CHECK_NAMES.get(g.name, g.name)
        for g in report.gates
        if g.required and g.status == "unavailable"
    ]
    if failed:
        return "Found problems in " + ", ".join(failed).lower() + "."
    if missing:
        return "Could not finish: " + ", ".join(missing) + " is not installed."
    if any(g.required and g.status != "passed" for g in report.gates):
        return "Some required checks did not run."
    return "Structure, preservation and EPUB validation passed."


STEPS_IMPORT = [
    ("keep", "Keep the original and check it"),
    ("repair", "Repair what can be repaired safely"),
    ("preview", "Render reader pages"),
    ("notes", "List fonts and footnotes"),
]
STEPS_RECHECK = [("keep", "Check the current file again"), *STEPS_IMPORT[1:]]


def import_book(run, config, path):
    run.begin("keep")
    result = ingest_new(path, config)
    if isinstance(result, KnownBook):
        run.end("keep", "done", "This book is already on your shelf. It opened unchanged.")
        run.skip_rest("Already done for this book.")
        run.notice = "This book was already on your shelf, so it opened without changes."
        return
    run.title = "; ".join(result.inspection.metadata.get("title", [])) or run.title
    run.end("keep", "done", "Original kept byte for byte. " + check_summary(result, config))
    finish(run, config, result.book_id)


def recheck_book(run, config, book):
    from emend.revalidation import revalidate

    run.begin("keep")
    report = revalidate(book, config)
    run.end("keep", "done", check_summary(report, config))
    finish(run, config, book)


def finish(run, config, book):
    repair(run, config, book)
    preview(run, config, book)
    notes(run, config, book)
    report = get_report(Store(config.root), book)
    run.notice = (
        "Ready. The checks passed; look through the pages, then download or send it."
        if report.state == "VALIDATED"
        else "Finished. Some problems need you; they are explained below."
    )


def repair(run, config, book):
    from emend.repairs import RepairRejectedError, apply_repairs, prepare_repairs

    run.begin("repair")
    store = Store(config.root)
    applied = []
    # Each plan holds one action; a repaired book may expose the next supported one.
    for _ in range(len(FIX_NAMES) + 1):
        row, _, digest = current_source(store, book, config)
        report = get_report(store, row["id"])
        if not blockers(row, report, digest, config):
            break
        if any(g.required and g.status == "unavailable" for g in report.gates):
            run.end(
                "repair",
                "skipped",
                "A required checker is missing, so a repaired copy could not be verified.",
            )
            return
        plan, _ = prepare_repairs(row["id"], config)
        if not plan.actions:
            break
        name = " then ".join(f"“{FIX_NAMES.get(a, a)}”" for a in plan.actions)
        try:
            apply_repairs(row["id"], config, plan_id=plan.id, approve=plan.actions)
        except RepairRejectedError:
            done = f"Applied {', '.join(applied)}. " if applied else ""
            run.end(
                "repair",
                "failed",
                f"{done}Tried {name}, but the repaired copy still failed its checks, "
                "so Emend kept the previous version.",
            )
            return
        applied.append(name)
    row, _, digest = current_source(store, book, config)
    remaining = blockers(row, get_report(store, row["id"]), digest, config)
    if applied:
        run.end(
            "repair",
            "done" if not remaining else "failed",
            f"Applied {', '.join(applied)} and checked the result."
            + (" Other problems remain." if remaining else ""),
        )
    elif remaining:
        run.end("repair", "failed", "No supported repair applies to these problems.")
    else:
        run.end("repair", "done", "Nothing needed repair.")


def preview(run, config, book):
    from emend.visual import visual_review

    run.begin("preview")
    report = get_report(Store(config.root), book)
    if report.inspection.blocking:
        run.end("preview", "skipped", "Pages can be rendered once the EPUB structure is readable.")
        return
    try:
        try:
            result = visual_review(
                book,
                config,
                profiles=PREVIEW_PROFILES,
                max_documents=PREVIEW_DOCUMENTS,
                layout="paginated",
            )
        except ValueError as exc:
            if "layout scroll" not in str(exc):
                raise
            result = visual_review(
                book,
                config,
                profiles=PREVIEW_PROFILES,
                max_documents=PREVIEW_DOCUMENTS,
                layout="scroll",
            )
    except (ValueError, OSError) as exc:
        run.end("preview", "failed", explain(exc))
        return
    pages = sum(len(s.screenshots) for s in result.samples)
    if result.status == "complete":
        run.end(
            "preview",
            "done",
            f"Rendered {pages} {'page' if pages == 1 else 'pages'} from "
            f"{len(result.selected_documents)} of {result.total_spine_documents} documents.",
        )
    else:
        run.end("preview", "failed", "Some pages could not be rendered; see the preview notes.")


def notes(run, config, book):
    from emend.reading_review import reading_review

    run.begin("notes")
    report = get_report(Store(config.root), book)
    if report.inspection.blocking:
        run.end("notes", "skipped", "Waiting for a readable EPUB structure.")
        return
    try:
        review = reading_review(book, config)
    except (ValueError, OSError) as exc:
        run.end("notes", "failed", explain(exc))
        return
    fonts, marked = len(review.fonts), len(review.notes)
    run.end(
        "notes",
        "done" if review.complete else "failed",
        f"{fonts} embedded {'font' if fonts == 1 else 'fonts'} and "
        f"{marked} marked {'note' if marked == 1 else 'notes'}."
        + ("" if review.complete else " Some files exceeded the review limits."),
    )


def preview_only(run, config, book, layout):
    from emend.visual import visual_review

    run.begin("preview")
    result = visual_review(
        book, config, profiles=PREVIEW_PROFILES, max_documents=PREVIEW_DOCUMENTS, layout=layout
    )
    pages = sum(len(s.screenshots) for s in result.samples)
    run.end(
        "preview",
        "done" if result.status == "complete" else "failed",
        f"Rendered {pages} {'page' if pages == 1 else 'pages'}."
        if result.status == "complete"
        else "Some pages could not be rendered; see the preview notes.",
    )
    run.notice = "Pages rendered." if result.status == "complete" else ""


def spacing_trial(run, config, book, settings, layout):
    from emend.typography import typography_trial

    run.begin("trial")
    trial = typography_trial(
        book,
        config,
        settings,
        profiles=PREVIEW_PROFILES,
        max_documents=PREVIEW_DOCUMENTS,
        layout=layout,
    )
    if trial.status == "complete":
        run.end("trial", "done", "Baseline and adjusted pages rendered from the same file.")
        run.notice = "Comparison ready. Nothing in the book has changed yet."
    else:
        run.end("trial", "failed", "Some pages could not be rendered, so it cannot be applied.")


def apply_spacing(run, config, book, trial_id, all_spine):
    from emend.typography_apply import apply_typography

    run.begin("apply")
    apply_typography(book, config, trial_id=trial_id, approve=True, all_spine=all_spine)
    run.end("apply", "done", "Applied, preserved the text and passed the required checks.")
    preview(run, config, book)
    run.notice = "Spacing applied. The comparison with the baseline is kept below."


def restore(run, config, book, revision, expected_report):
    from emend.recovery import recover

    run.begin("restore")
    recover(book, config, revision=revision, approve=True, expected_report_path=expected_report)
    report = get_report(Store(config.root), book)
    run.end("restore", "done", check_summary(report, config))
    preview(run, config, book)
    run.notice = "Restored the earlier version and checked it again."


def size_trial(run, config, book, layout):
    from emend.text_size import text_size_trial

    run.begin("trial")
    try:
        trial = text_size_trial(book, config, max_documents=PREVIEW_DOCUMENTS, layout=layout)
    except ValueError as exc:
        if layout != "paginated" or "layout scroll" not in str(exc):
            raise
        trial = text_size_trial(book, config, max_documents=PREVIEW_DOCUMENTS, layout="scroll")
    if trial.status == "complete":
        run.end("trial", "done", "Rendered the book as published and at your reader's size.")
        run.notice = "Comparison ready. Nothing in the book has changed yet."
    else:
        run.end("trial", "failed", "Some pages could not be rendered, so it cannot be applied.")


def apply_size(run, config, book, trial_id):
    from emend.text_size import apply_text_size

    run.begin("apply")
    report = apply_text_size(book, config, trial_id=trial_id, approve=True)
    change = report.text_size_changes[-1]
    run.end(
        "apply",
        "done",
        f"Reset {plural(change.edits, 'size rule')}, kept every other byte and passed the required checks.",
    )
    preview(run, config, book)
    run.notice = "Body text now follows your reader's size. The comparison is kept below."


def plural(count, word):
    return f"{count} {word if count == 1 else word + 's'}"
