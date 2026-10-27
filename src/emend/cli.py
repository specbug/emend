import json
import math
import os
from pathlib import Path
from time import sleep
from typing import Annotated

import typer
import yaml

from emend.backup import backup_library, restore_backup, upload_backup, verify_backup
from emend.config import Config, default_config_path, executable, load_config
from emend.inbox import Inbox
from emend.inspect import inspect_epub
from emend.pipeline import get_report
from emend.pipeline import ingest as ingest_book
from emend.publish import publish as publish_book
from emend.reports import report_text
from emend.revalidation import revalidate as revalidate_book
from emend.storage import Store
from emend.validation import epubcheck_command

app = typer.Typer(
    no_args_is_help=True,
    help="Preserve, inspect, validate, publish, and back up your EPUB library.",
)
ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config", "-c", help="Config file (default: ~/Emend/config.yaml or EMEND_CONFIG)."
    ),
]


def fail(exc: Exception):
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(2) from exc


@app.command()
def init(
    root: Annotated[
        Path, typer.Option(help="Emend data folder, outside your Calibre library.")
    ] = Path("~/Emend"),
    config: ConfigOption = None,
):
    """Create folders and a conservative configuration. Existing configs are preserved."""
    try:
        path = (config or default_config_path()).expanduser().resolve()
        settings = Config(root=root.expanduser().resolve())
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x") as out:
            out.write("# Emend: keep the active Calibre library local; upload versioned backups.\n")
            yaml.safe_dump(settings.model_dump(mode="json"), out, sort_keys=False)
        Store(settings.root)
        typer.echo(
            f"Created {path}\nData directory: {settings.root}\nNext: emend doctor --config {path}"
        )
    except (ValueError, OSError) as exc:
        fail(exc)


@app.command()
def doctor(config: ConfigOption = None):
    """Check local tooling and configuration without changing your library."""
    try:
        settings = load_config(config)
        from emend.visual import availability

        epubcheck = epubcheck_command(settings)
        results = {
            "root": str(settings.root),
            "epubcheck": epubcheck or "unavailable",
            "epubcheck_required": settings.quality.require_epubcheck,
            "calibredb": executable("calibredb", settings) or "unavailable",
            "ace": executable("ace", settings) or "unavailable",
            "rclone": executable("rclone", settings) or "unavailable",
            "calibre_library": str(settings.publish.calibre_library)
            if settings.publish.calibre_library
            else "not configured",
            "drive_export": settings.publish.drive_remote or "not configured",
            "drive_backup": settings.publish.backup_remote or "not configured",
            "meta_model": settings.ai_review.model,
            "meta_api_key": "configured"
            if os.environ.get("MODEL_API_KEY")
            else "not configured (optional)",
            "visual_qa": availability(settings),
        }
        typer.echo(json.dumps(results, indent=2))
        missing = (settings.quality.require_epubcheck and not epubcheck) or (
            settings.quality.require_accessibility_audit and not executable("ace", settings)
        )
        if missing:
            raise typer.Exit(1)
    except (ValueError, OSError) as exc:
        fail(exc)


@app.command("inspect")
def inspect_command(epub: Path):
    """Inspect an EPUB without storing it, changing it, or running external tools."""
    try:
        result = inspect_epub(epub.expanduser().resolve())
        typer.echo(result.model_dump_json(indent=2))
        if result.blocking:
            raise typer.Exit(1)
    except (ValueError, OSError) as exc:
        fail(exc)


@app.command()
def ingest(epub: Path, config: ConfigOption = None):
    """Preserve an EPUB and run quality gates. This never publishes automatically."""
    try:
        settings = load_config(config)
        result = ingest_book(epub, settings)
        typer.echo(report_text(result))
        row = Store(settings.root).resolve(result.book_id)
        typer.echo(f"\nReports: {Path(row['report_path']).parent}")
        if result.state != "VALIDATED":
            raise typer.Exit(1)
    except (ValueError, OSError) as exc:
        fail(exc)


@app.command("list")
def list_books(config: ConfigOption = None):
    """List imported books and their current states."""
    try:
        rows = Store(load_config(config).root).books()
        for row in rows:
            typer.echo(f"{row['id'][:12]}  {row['state']:<16} {row['source_name']}")
        if not rows:
            typer.echo("No books imported. Run emend ingest /path/to/book.epub.")
    except (ValueError, OSError) as exc:
        fail(exc)


@app.command()
def inbox(
    directory: Annotated[
        Path | None, typer.Option("--directory", help="Existing folder; defaults to Emend's inbox.")
    ] = None,
    watch: Annotated[
        bool, typer.Option("--watch", help="Keep polling in the foreground until Ctrl+C.")
    ] = False,
    stable_seconds: Annotated[
        float, typer.Option(min=0.1, max=300, help="Required unchanged observation interval.")
    ] = 2,
    interval: Annotated[
        float, typer.Option(min=0.1, max=60, help="Polling interval in watch mode.")
    ] = 1,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    config: ConfigOption = None,
):
    """Import stable, new EPUBs without resetting known books or publishing."""
    try:
        if not math.isfinite(interval):
            raise ValueError("Polling interval must be finite.")
        processor = Inbox(load_config(config), directory, stable_seconds=stable_seconds)

        def show(event):
            typer.echo(
                event.model_dump_json()
                if as_json
                else f"{event.status.upper():<8} {event.source!r}: {event.detail}"
                + (f" [{event.book_id[:12]}]" if event.book_id else "")
            )

        if watch:
            if not as_json:
                typer.echo(f"Watching {processor.directory}. Stop with Ctrl+C.")
            try:
                while True:
                    for event in processor.scan():
                        show(event)
                    sleep(interval)
            except KeyboardInterrupt:
                if not as_json:
                    typer.echo("Stopped watching.")
        else:
            events = processor.batch()
            if as_json:
                typer.echo(
                    json.dumps(
                        {
                            "directory": str(processor.directory),
                            "events": [event.model_dump() for event in events],
                        }
                    )
                )
            else:
                for event in events:
                    show(event)
                if not events:
                    typer.echo("No new EPUB activity in this inbox.")
            if any(event.status in {"waiting", "blocked", "busy", "error"} for event in events):
                raise typer.Exit(1)
    except (ValueError, OSError) as exc:
        fail(exc)


@app.command("repair-plan")
def repair_plan(
    book_id: str,
    config: ConfigOption = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Propose supported EPUB repairs; leave the EPUB and approval unchanged."""
    from emend.repairs import prepare_repairs

    try:
        plan, review = prepare_repairs(book_id, load_config(config))
        typer.echo(
            plan.model_dump_json(indent=2)
            if as_json
            else f"Plan: {plan.id}\nActions: {', '.join(plan.actions) or '(none supported)'}\n{plan.detail}\nReview: {review}"
        )
    except (ValueError, OSError) as exc:
        fail(exc)


@app.command("apply-repairs")
def apply_repairs_command(
    book_id: str,
    plan: Annotated[str, typer.Option("--plan")],
    approve: Annotated[
        list[str], typer.Option("--approve", help="Explicitly approve an action ID.")
    ],
    config: ConfigOption = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Apply explicitly approved repairs, validate, and retain the previous book on failure."""
    from emend.repairs import RepairRejectedError, apply_repairs

    try:
        result = apply_repairs(book_id, load_config(config), plan_id=plan, approve=approve)
        typer.echo(result.model_dump_json(indent=2) if as_json else report_text(result))
    except RepairRejectedError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except (ValueError, OSError) as exc:
        fail(exc)


@app.command()
def revalidate(
    book_id: str,
    config: ConfigOption = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Rerun quality gates on the current EPUB, retaining metadata and cover edits."""
    try:
        settings = load_config(config)
        result = revalidate_book(book_id, settings)
        typer.echo(result.model_dump_json(indent=2) if as_json else report_text(result))
        if not as_json:
            row = Store(settings.root).resolve(result.book_id)
            typer.echo(f"\nReports: {Path(row['report_path']).parent}")
        if result.state != "VALIDATED":
            raise typer.Exit(1)
    except (ValueError, OSError) as exc:
        fail(exc)


@app.command()
def report(
    book_id: str,
    config: ConfigOption = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Show a book's latest report; accepts a unique SHA-256 prefix."""
    try:
        result = get_report(Store(load_config(config).root), book_id)
        typer.echo(result.model_dump_json(indent=2) if as_json else report_text(result))
    except (ValueError, OSError) as exc:
        fail(exc)


@app.command()
def publish(
    book_id: Annotated[str | None, typer.Argument()] = None,
    ready: Annotated[
        bool, typer.Option("--ready", help="Publish all books with passed gates.")
    ] = False,
    target: Annotated[
        list[str] | None, typer.Option("--target", help="calibre or drive; repeat for both.")
    ] = None,
    config: ConfigOption = None,
):
    """Explicitly publish validated EPUBs; retain retryable receipts per destination."""
    try:
        if bool(book_id) == ready:
            raise ValueError("Supply either BOOK_ID or --ready.")
        settings = load_config(config)
        ids = (
            [
                row["id"]
                for row in Store(settings.root).books()
                if row["state"] in {"VALIDATED", "PUBLISHED"}
            ]
            if ready
            else [book_id]
        )
        failed = False
        for identifier in ids:
            try:
                result = publish_book(identifier, target or ["calibre"], settings)
                typer.echo(json.dumps({"book_id": identifier, "results": result}, indent=2))
                failed |= any(value["status"] == "pending" for value in result.values())
            except (ValueError, OSError) as exc:
                typer.echo(f"{identifier}: {exc}", err=True)
                failed = True
        if not ids:
            typer.echo("No validated books are ready to publish.")
        if failed:
            raise typer.Exit(1)
    except (ValueError, OSError) as exc:
        fail(exc)


@app.command()
def backup(
    calibre_closed: Annotated[
        bool,
        typer.Option(
            "--calibre-closed",
            help="Confirm Calibre, its server, and other library writers are closed.",
        ),
    ] = False,
    to_drive: Annotated[
        bool, typer.Option("--to-drive", help="Upload the verified snapshot using rclone.")
    ] = False,
    config: ConfigOption = None,
):
    """Back up the complete Calibre library, including its database and all formats."""
    try:
        result = backup_library(load_config(config), calibre_closed, upload=to_drive)
        typer.echo(json.dumps(result, indent=2))
        if result["cloud_status"] == "pending":
            raise typer.Exit(1)
    except (ValueError, OSError) as exc:
        fail(exc)


@app.command("verify-backup")
def verify_backup_command(archive: Path):
    """Verify every backup checksum and the archived Calibre database."""
    try:
        manifest = verify_backup(archive.expanduser().resolve())
        typer.echo(f"Verified {len(manifest['files'])} files and Calibre database integrity.")
    except (ValueError, OSError) as exc:
        fail(exc)


@app.command("upload-backup")
def upload_backup_command(archive: Path, config: ConfigOption = None):
    """Retry a backup upload without creating another snapshot."""
    try:
        typer.echo(
            json.dumps(upload_backup(archive.expanduser().resolve(), load_config(config)), indent=2)
        )
    except (ValueError, OSError) as exc:
        fail(exc)


@app.command()
def restore(
    archive: Path,
    to: Annotated[
        Path, typer.Option("--to", help="New local library folder; must not already exist.")
    ],
):
    """Restore a verified backup into a new folder, then open that folder in Calibre."""
    try:
        restored = restore_backup(archive.expanduser().resolve(), to)
        typer.echo(
            f"Restored {restored}\nIn Calibre: Switch/create library → Use the previously existing library at the new location."
        )
    except (ValueError, OSError) as exc:
        fail(exc)


@app.command()
def research(
    book_id: str,
    config: ConfigOption = None,
    isbn: Annotated[
        str | None, typer.Option(help="Choose an ISBN already present in the EPUB.")
    ] = None,
    offline: bool = False,
    refresh: bool = False,
    edition: str | None = None,
    discovery: str | None = None,
):
    """Collect exact-ISBN evidence and proposals; never edit or publish a book."""
    from emend.research.service import plan_text
    from emend.research.service import research as research_book

    try:
        result = research_book(
            book_id,
            load_config(config),
            isbn=isbn,
            offline=offline,
            refresh=refresh,
            edition_id=edition,
            discovery_id=discovery,
        )
        typer.echo(plan_text(result))
        if result.provider_errors:
            raise typer.Exit(1)
    except (ValueError, OSError) as exc:
        fail(exc)


@app.command()
def proposals(
    book_id: str,
    config: ConfigOption = None,
    plan: str | None = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Review a recorded research plan, including source evidence and cover suggestions."""
    from emend.research.service import load_plan, plan_text

    try:
        store = Store(load_config(config).root)
        result = load_plan(store, store.resolve(book_id)["id"], plan)
        typer.echo(result.model_dump_json(indent=2) if as_json else plan_text(result))
    except (ValueError, OSError) as exc:
        fail(exc)


@app.command()
def discover(
    book_id: str,
    config: ConfigOption = None,
    title: str | None = None,
    author: str | None = None,
    offline: bool = False,
    refresh: bool = False,
):
    """Find candidate editions for an EPUB without an ISBN; never select one automatically."""
    from emend.research.discovery import discover as discover_book
    from emend.research.discovery import discovery_text

    try:
        result = discover_book(
            book_id,
            load_config(config),
            title=title,
            author=author,
            offline=offline,
            refresh=refresh,
        )
        typer.echo(discovery_text(result))
        if result.provider_errors:
            raise typer.Exit(1)
    except (ValueError, OSError) as exc:
        fail(exc)


@app.command()
def editions(
    book_id: str,
    config: ConfigOption = None,
    discovery: str | None = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Read saved edition candidates and their differences from the EPUB."""
    from emend.research.discovery import discovery_text, load_discovery

    try:
        store = Store(load_config(config).root)
        result = load_discovery(store, store.resolve(book_id)["id"], discovery)
        typer.echo(result.model_dump_json(indent=2) if as_json else discovery_text(result))
    except (ValueError, OSError) as exc:
        fail(exc)


@app.command("ai-review")
def ai_review_command(
    book_id: str,
    config: ConfigOption = None,
    plan: str | None = None,
    show: Annotated[
        bool, typer.Option(help="Show saved advice without making a network request.")
    ] = False,
    refresh: Annotated[
        bool,
        typer.Option(
            help="Request new advice even when cached; may incur another Meta API charge."
        ),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Ask Meta to review collected metadata evidence. Sends metadata, never book content; advice only."""
    from emend.research.ai_review import review_plan, review_text, show_review

    try:
        if show and refresh:
            raise ValueError("--show and --refresh cannot be combined.")
        settings = load_config(config)
        result = (
            show_review(book_id, settings, plan)
            if show
            else review_plan(book_id, settings, plan_id=plan, refresh=refresh)
        )
        typer.echo(result.model_dump_json(indent=2) if as_json else review_text(result))
    except (ValueError, OSError) as exc:
        fail(exc)


@app.command("cover-review")
def cover_review_command(
    book_id: str,
    config: ConfigOption = None,
    plan: str | None = None,
    image: Path | None = None,
    source_note: str | None = None,
):
    """Prepare a local artwork comparison from research candidates or a supplied image."""
    from emend.covers import cover_review, cover_review_text

    try:
        settings = load_config(config)
        result = cover_review(book_id, settings, plan_id=plan, image=image, source_note=source_note)
        typer.echo(cover_review_text(result))
        row = Store(settings.root).document(result.book_id, "cover-review", result.id)
        typer.echo(f"\nComparison: {Path(row['path']).with_suffix('.html')}")
        if result.errors or not result.candidates:
            raise typer.Exit(1)
    except (ValueError, OSError) as exc:
        fail(exc)


@app.command("covers")
def covers_command(
    book_id: str,
    config: ConfigOption = None,
    review: str | None = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Read a saved cover review without downloading images."""
    from emend.covers import cover_review_text, load_cover_review

    try:
        store = Store(load_config(config).root)
        result = load_cover_review(store, store.resolve(book_id)["id"], review)
        typer.echo(result.model_dump_json(indent=2) if as_json else cover_review_text(result))
    except (ValueError, OSError) as exc:
        fail(exc)


@app.command("apply-cover")
def apply_cover_command(
    book_id: str,
    review: Annotated[str, typer.Option(help="Saved cover review ID.")],
    approve: Annotated[
        str, typer.Option(help="Candidate ID whose artwork and edition you approve.")
    ],
    config: ConfigOption = None,
    allow_smaller: bool = False,
    allow_aspect_change: bool = False,
):
    """Replace one declared cover, preserve unrelated resources, and rerun quality gates."""
    from emend.cover_apply import apply_cover

    try:
        result = apply_cover(
            book_id,
            load_config(config),
            review_id=review,
            approve=approve,
            allow_smaller=allow_smaller,
            allow_aspect_change=allow_aspect_change,
        )
        typer.echo(report_text(result))
    except (ValueError, OSError) as exc:
        fail(exc)


@app.command("apply")
def apply_command(
    book_id: str,
    config: ConfigOption = None,
    plan: str | None = None,
    safe: Annotated[
        bool, typer.Option(help="Select corroborated missing publisher/date fields.")
    ] = False,
    approve: Annotated[
        list[str] | None,
        typer.Option(help="Explicitly select a proposal ID; repeat for multiple fields."),
    ] = None,
):
    """Apply selected metadata proposals, preserve resources, and rerun quality gates."""
    from emend.curation import apply_proposals

    try:
        typer.echo(
            report_text(
                apply_proposals(
                    book_id, load_config(config), plan_id=plan, safe=safe, approve=approve
                )
            )
        )
    except (ValueError, OSError) as exc:
        fail(exc)


@app.command("reset-original")
def reset_original(book_id: str, config: ConfigOption = None):
    """Revalidate the preserved original as the active artifact. Keeps history and external copies."""
    try:
        settings = load_config(config)
        identifier = Store(settings.root).resolve(book_id)["id"]
        result = ingest_book(settings.root / "originals" / f"{identifier}.epub", settings)
        typer.echo(report_text(result))
        if result.state != "VALIDATED":
            raise typer.Exit(1)
    except (ValueError, OSError) as exc:
        fail(exc)


@app.command("visual-review")
def visual_review_command(
    book_id: str,
    config: ConfigOption = None,
    profile: Annotated[
        list[str] | None, typer.Option(help="compact or wide; repeat to select both.")
    ] = None,
    max_documents: Annotated[int, typer.Option(min=1, max=9)] = 5,
    layout: Annotated[str, typer.Option(help="scroll or paginated; preview only.")] = "scroll",
    page_margin: Annotated[
        int, typer.Option(min=16, max=120, help="Paginated preview margin in CSS pixels.")
    ] = 40,
):
    """Capture offline browser samples without changing book approval or publication."""
    from emend.visual import visual_review

    try:
        settings = load_config(config)
        result = visual_review(
            book_id,
            settings,
            profiles=profile,
            max_documents=max_documents,
            layout=layout,
            page_margin=page_margin,
        )
        path = Store(settings.root).document(result.book_id, "visual-review", result.id)["path"]
        typer.echo(
            f"Sampling {result.status}. Layout: {result.layout}. Review: {result.id}\nOpen: {Path(path).with_suffix('.html')}"
        )
        if result.status == "incomplete":
            raise typer.Exit(1)
    except (ValueError, OSError) as exc:
        fail(exc)


@app.command("visuals")
def visuals_command(
    book_id: str,
    config: ConfigOption = None,
    review: str | None = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Verify and read saved screenshots; flag reviews from an earlier artifact."""
    from emend.research.discovery import current_source
    from emend.visual import load_visual_review

    try:
        settings = load_config(config)
        store = Store(settings.root)
        row, _, digest = current_source(store, book_id, settings)
        result = load_visual_review(store, row["id"], review)
        current = result.artifact_sha256 == digest
        if as_json:
            typer.echo(
                json.dumps({"current_artifact": current, "review": result.model_dump()}, indent=2)
            )
        else:
            path = store.document(row["id"], "visual-review", result.id)["path"]
            typer.echo(
                f"Review {result.id}: {result.status} ({result.layout})\nArtifact: {'current' if current else 'STALE; rerun visual-review'}\nOpen: {Path(path).with_suffix('.html')}"
            )
        if not current or result.status == "incomplete":
            raise typer.Exit(1)
    except (ValueError, OSError) as exc:
        fail(exc)


@app.command("typography-trial")
def typography_trial_command(
    book_id: str,
    line_height: Annotated[
        float | None, typer.Option(min=1, max=2.4, help="Trial body/p line height, unitless.")
    ] = None,
    paragraph_gap: Annotated[
        float | None,
        typer.Option(min=0, max=2, help="Trial p spacing in em; sets the before margin to zero."),
    ] = None,
    profile: Annotated[
        list[str] | None, typer.Option(help="compact or wide; repeat for both.")
    ] = None,
    max_documents: Annotated[int, typer.Option(min=1, max=9)] = 3,
    layout: Annotated[str, typer.Option(help="scroll or paginated.")] = "scroll",
    page_margin: Annotated[int, typer.Option(min=16, max=120)] = 40,
    config: ConfigOption = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Compare chosen typography values in the browser; leave the EPUB unchanged."""
    from emend.typography import TypographySettings, typography_trial

    try:
        settings = load_config(config)
        values = TypographySettings(line_height=line_height, paragraph_gap=paragraph_gap)
        trial = typography_trial(
            book_id,
            settings,
            values,
            profiles=profile,
            max_documents=max_documents,
            layout=layout,
            page_margin=page_margin,
        )
        path = Store(settings.root).document(trial.book_id, "typography-trial", trial.id)["path"]
        typer.echo(
            trial.model_dump_json(indent=2)
            if as_json
            else f"Typography trial {trial.id}: {trial.status}\nOpen: {Path(path).with_suffix('.html')}\nEPUB and quality state unchanged."
        )
        if trial.status == "incomplete":
            raise typer.Exit(1)
    except (ValueError, OSError) as exc:
        fail(exc)


@app.command("apply-typography")
def apply_typography_command(
    book_id: str,
    trial: Annotated[str, typer.Option("--trial")],
    approve: Annotated[
        bool, typer.Option(help="Confirm you reviewed the complete trial and its CSS/scope.")
    ] = False,
    all_spine: Annotated[
        bool,
        typer.Option(
            help="Also apply to unsampled spine documents; inspect special content first."
        ),
    ] = False,
    config: ConfigOption = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Apply the exact CSS from a reviewed trial; preserve text and validate before activation."""
    from emend.typography_apply import TypographyRejectedError, apply_typography

    try:
        result = apply_typography(
            book_id, load_config(config), trial_id=trial, approve=approve, all_spine=all_spine
        )
        typer.echo(result.model_dump_json(indent=2) if as_json else report_text(result))
    except TypographyRejectedError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except (ValueError, OSError) as exc:
        fail(exc)


@app.command("typography-trials")
def typography_trials_command(
    book_id: str,
    trial: str | None = None,
    config: ConfigOption = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Verify a saved trial and flag comparisons from an earlier artifact."""
    from emend.research.discovery import current_source
    from emend.typography import load_trial

    try:
        settings = load_config(config)
        store = Store(settings.root)
        row, _, digest = current_source(store, book_id, settings)
        result = load_trial(store, row["id"], trial)
        current = result.artifact_sha256 == digest
        path = store.document(row["id"], "typography-trial", result.id)["path"]
        typer.echo(
            json.dumps({"current_artifact": current, "trial": result.model_dump()}, indent=2)
            if as_json
            else f"Typography trial {result.id}: {result.status}\nArtifact: {'current' if current else 'STALE; capture a new trial'}\nOpen: {Path(path).with_suffix('.html')}"
        )
        if not current or result.status != "complete":
            raise typer.Exit(1)
    except (ValueError, OSError) as exc:
        fail(exc)


@app.command("library")
def library_command(
    config: ConfigOption = None,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 200,
    offset: Annotated[int, typer.Option(min=0)] = 0,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Create an offline overview of books and their latest recorded reviews."""
    from emend.library import library_snapshot, write_library

    try:
        settings = load_config(config)
        snapshot = library_snapshot(settings, limit, offset)
        if as_json:
            typer.echo(snapshot.model_dump_json(indent=2))
        else:
            path = write_library(snapshot, settings)
            typer.echo(
                f"Showing {len(snapshot.books)} of {snapshot.total_books} books. Open: {path}"
            )
    except (ValueError, OSError) as exc:
        fail(exc)


if __name__ == "__main__":
    app()
