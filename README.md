# Emend

A local, preservation-first EPUB workflow for macOS (including Apple Silicon) and Linux. Emend keeps original books unchanged, produces auditable quality reports, and publishes only after required checks pass. It also backs up and restores **complete Calibre libraries**, including their database, covers, metadata, custom columns, and every stored book format.

This is milestone 1 of the design in [the roadmap](docs/roadmap.md). It is a working CLI foundation, not an AI editor or a device certification system.

## Install on your Mac

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/getting-started/installation/), and your existing Calibre installation. Emend detects `/Applications/calibre.app/Contents/MacOS` automatically. It does not modify your Calibre installation. If you use Homebrew, `brew install uv epubcheck rclone` installs the additional tools for the full workflow; uv can provision Python 3.12 when needed.

From this repository:

```sh
uv sync --frozen
uv run emend init
uv run emend doctor
```

Use `uv run emend …` from the repository, or activate `.venv/bin/activate` to use `emend` directly. `doctor` returns a nonzero status when a required validator is missing.

Install EPUBCheck (required by default):

```sh
brew install epubcheck
```

Alternatively, download an official [EPUBCheck release](https://github.com/w3c/epubcheck/releases), install its required Java runtime, and set `tools.epubcheck_jar` to the extracted `epubcheck.jar`. Preserve the distribution's dependency folder alongside the JAR. Optional accessibility checks use [Ace by DAISY](https://daisy.github.io/ace/).

Edit `~/Emend/config.yaml`:

```yaml
root: /Users/YOUR_NAME/Emend
quality:
  require_epubcheck: true
  require_accessibility_audit: false
publish:
  calibre_library: /Users/YOUR_NAME/Calibre Library
  drive_remote: null
  backup_remote: gdrive:Emend/backups
```

Keep the other generated settings, or omit them to use their defaults. Paths can use `~`; relative paths resolve beside the configuration file. Unknown settings are rejected to catch typos. An alternative config can be passed with `--config PATH` on each command or selected with `EMEND_CONFIG`.

## Inspect and import an EPUB

```sh
uv run emend inspect ~/Downloads/book.epub
uv run emend ingest ~/Downloads/book.epub
uv run emend list
uv run emend report BOOK_ID
uv run emend report BOOK_ID --json
```

IDs are SHA-256 hashes; commands accept unique prefixes of at least eight characters. `inspect` only reads the input. `ingest` stores an immutable-by-convention, read-only original, runs inspection and external validators, and creates JSON, text, and self-contained HTML reports. Open the reported `report.html` file locally to view it.

The built-in checks cover ZIP structure and CRCs, bounded archive sizes, container/OPF/manifest/spine consistency, required metadata, EPUB 2 NCX and EPUB 3 navigation, duplicate IDs, local links and fragments, CSS resource links, corrupt raster images, and selected typography/accessibility warnings. All source resources and XHTML text are fingerprinted. The approved artifact is **byte-identical to the input**: this milestone performs no transformations.

Structural errors or failed required validators produce `FAILED`. Missing required tools produce `REVIEW_REQUIRED`. Neither state can publish. Optional check failures remain visible but do not block publication. EPUBCheck is required by default; do not disable it if you need standards validation. Visual QA is explicitly marked unimplemented, and no “Gold,” edition-match, or device-readiness certification is issued.

Warnings about publisher styles, missing alt text, duplicate chapter text, or replacement characters are observations; Emend never silently rewrites them. Multi-rendition, DRM-encrypted, scripted, responsive-source-set, and remotely embedded content require manual review in this version. Font obfuscation is retained.

## Publish approved books to Calibre

Set `publish.calibre_library` to your existing local library folder. Close Calibre and other writers before using the CLI integration.

```sh
uv run emend publish BOOK_ID
uv run emend publish --ready
```

Publishing is explicit, separate from ingestion, and uses `calibredb`; Emend never writes Calibre's database directly. A `emend` identifier and per-destination receipts make retries idempotent. Existing books with the same title may coexist if their source bytes differ. Emend rechecks the original and artifact hashes and the quality policy before publishing, and verifies the stored EPUB after importing into Calibre. If an import plugin changes the file, delivery remains pending for review. If policy changes, re-ingest the preserved original first.

Receipts record successful deliveries; they do not continually inspect remote state. Deleting a delivered book outside Emend does not automatically invalidate its receipt.

## Full-fidelity Google Drive backup and recovery

**Keep the active Calibre library on the Mac. Upload dated snapshots to Drive.** Do not place the live library database in a synchronized Drive folder.

Install and configure [rclone](https://rclone.org/drive/) on your Mac:

```sh
brew install rclone
rclone config
```

Create a Google Drive remote named `gdrive` using rclone's browser sign-in. Credentials stay in rclone's own configuration; do not put them in Emend YAML or Git. Set `publish.backup_remote: gdrive:Emend/backups`.

Quit Calibre, its Content Server, and any other application that writes the library, then:

```sh
uv run emend backup --calibre-closed --to-drive
```

This creates a dated `.tar.gz` under `~/Emend/backups`, containing **every regular file in the library**, not just EPUB exports. That includes `metadata.db`, per-book OPF metadata, covers, all book formats, supplementary files, and library-specific databases such as stored notes. Rich book records are restored from the actual library database, including identifiers, tags, series, custom columns, and saved library data.

Each archive has a file-by-file SHA-256 manifest. Emend checks the copied database, verifies every archived file, and compares the source inventory again to detect changes during backup. The `--calibre-closed` flag is your confirmation that writers are stopped; Emend cannot lock other applications out of Calibre's library. An open WAL/journal or changed file blocks backup. Backups containing symlinks are rejected rather than silently losing linked data.

Uploads use `rclone copyto --checksum`. They never synchronize deletions or prune older snapshots. Each full snapshot consumes approximately the compressed size of your library. Uploads allow one hour by default; set `limits.publish_timeout_seconds` up to 86400 for particularly large libraries or slow connections. If upload fails, the verified local backup is retained. Retry that archive:

```sh
uv run emend upload-backup ~/Emend/backups/calibre-TIMESTAMP-ID.tar.gz
```

To recover after a reinstall:

1. Install Calibre, Emend, and rclone; reconnect your Google Drive remote.
2. Download a snapshot with the Drive UI or `rclone copy gdrive:Emend/backups ~/Downloads/Emend-backups`.
3. Verify and restore it into a **new, nonexistent** local directory:

   ```sh
   uv run emend verify-backup ~/Downloads/Emend-backups/calibre-TIMESTAMP-ID.tar.gz
   uv run emend restore ~/Downloads/Emend-backups/calibre-TIMESTAMP-ID.tar.gz --to ~/Restored-Calibre-Library
   ```

4. In Calibre, use **Switch/create library → Use the previously existing library at the new location**, and select the restored directory.

Restoration verifies every file and does not overwrite an existing library. A compatible Calibre version can then open the restored database with its original rich book records. Test a restore periodically, not only after data loss.

**Backup scope:** the complete selected Calibre library folder. Calibre's application-wide preferences/plugins (normally `~/Library/Preferences/calibre` on macOS), rclone credentials, Emend's own state, and data stored only on reading devices are outside this snapshot. Back those up separately with Time Machine or another backup system if needed. Reader annotations are included only when they have actually been saved inside the Calibre library. Google Drive storage is provided by your own account; Emend requires no subscription and operates locally.

## Optional individual EPUB exports to Drive

This is a separate feature from full library backup. Set `publish.drive_remote: gdrive:Emend/curated`, then:

```sh
uv run emend publish BOOK_ID --target drive
uv run emend publish BOOK_ID --target calibre --target drive
```

Exports use stable hash filenames and contain only the approved EPUB, without Calibre's database or custom columns. They are convenient for reading apps, but are **not** a full Calibre backup. Direct XTEINK transfer and device-specific conversion are deferred; test standard EPUB compatibility on your X4 Pro.

## Data and recovery

```text
~/Emend/
  config.yaml
  inbox/                    # Reserved for manual drop-off; no watcher yet
  originals/<SHA>.epub       # Preserved sources
  curated/<SHA>.epub         # Only artifacts whose required gates passed
  reports/<SHA>/<RUN>/       # JSON, HTML, text, and validator evidence
  backups/                  # Complete Calibre snapshots and upload receipts
  state.db                  # Books, state history, publishing receipts
```

Re-ingesting identical input reuses its original and book ID but runs fresh validation; each attempt keeps its own evidence. A failed retry removes its ready artifact and blocks publishing. Operations serialize using a local file lock. Interrupted jobs can be retried by ingesting their original again. Back up the entire Emend folder separately to preserve its reports and workflow history.

## Development

```sh
uv sync --frozen
uv run pytest
uv run ruff check .
```

The generated regression corpus contains EPUB 2/3 books, footnotes, illustrations, malformed containers, unsafe references, and damaged resources. Publisher tests use local fake executables; they do not access a personal Calibre library or Google Drive. See [validation notes](docs/validation.md) for what has actually been exercised.

Exit codes: `0` success; `1` blocked quality gates or pending publication/upload; `2` invalid configuration, input, or operational error. No automatic publication occurs merely by inspecting or ingesting a book.
