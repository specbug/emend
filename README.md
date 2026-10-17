# Emend

A local, preservation-first EPUB workflow for macOS (including Apple Silicon) and Linux. Emend keeps original books unchanged, produces auditable quality reports, and publishes only after required checks pass. It also backs up and restores **complete Calibre libraries**, including their database, covers, metadata, custom columns, and every stored book format.

Emend includes the reliable foundation and exact-ISBN metadata curation from [the roadmap](docs/roadmap.md). It runs locally, records evidence for proposed edits, and keeps publication explicit. Optional Meta Model API reviews provide evidence-linked advice; device certification is not implemented.

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

The built-in checks cover ZIP structure and CRCs, bounded archive sizes, container/OPF/manifest/spine consistency, required metadata, EPUB 2 NCX and EPUB 3 navigation, duplicate IDs, local links and fragments, CSS resource links, corrupt raster images, and selected typography/accessibility warnings. All source resources and XHTML text are fingerprinted. The initial approved artifact is **byte-identical to the input**. Metadata curation is a separate, explicit operation described below.

Structural errors or failed required validators produce `FAILED`. Missing required tools produce `REVIEW_REQUIRED`. Neither state can publish. Optional check failures remain visible but do not block publication. EPUBCheck is required by default; do not disable it if you need standards validation. Visual QA is explicitly marked unimplemented, and no “Gold,” edition-match, or device-readiness certification is issued.

Warnings about publisher styles, missing alt text, duplicate chapter text, or replacement characters are observations; Emend never silently rewrites them. Multi-rendition, DRM-encrypted, scripted, responsive-source-set, and remotely embedded content require manual review in this version. Font obfuscation is retained.

## Research and curate metadata

For an imported book with a valid ISBN:

```sh
uv run emend research BOOK_ID
uv run emend proposals BOOK_ID
uv run emend proposals BOOK_ID --json
uv run emend apply BOOK_ID --safe
uv run emend report BOOK_ID
```

Research queries Open Library and Google Books by exact ISBN, caches responses for seven days, and saves a separate, hash-registered plan with source URLs, timestamps, response fingerprints, and before/after proposals. Neither research nor apply publishes anything. Public lookup requests send the selected ISBN; Open Library author lookups send author record IDs. No EPUB content is uploaded. These public APIs need no key in this implementation, but their quotas and availability still apply.

`--safe` selects only **missing publisher/date fields** corroborated by both providers, with matching ISBN, title, ordered authors, and no supplied language conflicts. Existing values and title/language changes always require review. Review source evidence, then explicitly select a proposal:

```sh
uv run emend apply BOOK_ID --approve PROPOSAL_ID
# Repeat --approve for different fields; choose one value per field.
uv run emend apply BOOK_ID --plan PLAN_ID --approve PROPOSAL_ID
```

Edits change only the selected plain OPF metadata nodes and the required EPUB 3 modification timestamp. Every other resource must retain its exact bytes; reading text and spine order must match. An independent OPF comparison rejects unselected metadata, attribute, or structural changes. Required validators run again before the candidate becomes active. Failed candidates leave the previous approved artifact/report in place and retain failure evidence under `reports/BOOK_ID/`. Ambiguous multi-valued or structured metadata requires manual editing.

Plans are tied to the current artifact and research policy. Edited or stale plans are rejected; rerun research after applying a different selection. Repeating the same successful plan and selection returns the existing result. Prior artifacts remain in `versions/BOOK_ID/`, originals remain unchanged, and reports retain the plan ID, before/after values, evidence URLs, and parent revision.

```sh
uv run emend research BOOK_ID --offline   # Use fresh cached evidence only
uv run emend research BOOK_ID --refresh   # Refresh provider evidence
uv run emend research BOOK_ID --isbn ISBN # Choose among ISBNs already in the EPUB
uv run emend reset-original BOOK_ID       # Revalidate the original as the active artifact
```

`reset-original` keeps history and external copies; if revalidation fails, publication is blocked until the required gates pass. Books without a valid ISBN currently need manual research. Failed providers remain visible and prevent safe proposals; research exits nonzero when a provider fails. Cover URLs are provenance-linked suggestions only: images are neither downloaded nor replaced. Author/identifier edits, title-only matching, and typography changes are deferred.

Optional configuration:

```yaml
metadata:
  sources: [openlibrary, google_books]
  cache_days: 7
  request_timeout_seconds: 15
```

Publishing an edited artifact creates a separately identified revision in Calibre and a hash-named EPUB in Drive. It does **not** overwrite an existing Calibre entry or migrate annotations between revisions. Each revision/destination has its own retryable receipt. Full-library backup continues to preserve all entries and their stored data.

Upgrading from 0.1: old reports require revalidation with the new engine (`reset-original` or re-ingest the original). SQLite research tables are added without deleting existing books or receipts. Back up the Emend data directory separately if you want to preserve its research/history; Calibre-library backups cover the Calibre library itself.

## Ask Meta to review uncertain metadata

After `research`, use [Meta Model API](https://dev.meta.ai/products/meta-model-api) for advice on proposals marked `review`:

```sh
uv run emend ai-review BOOK_ID
uv run emend ai-review BOOK_ID --show         # Read saved advice without a network call
uv run emend ai-review BOOK_ID --show --json  # Inspect the saved request, citations, and usage
uv run emend ai-review BOOK_ID --refresh      # Explicitly request new advice
```

Create an API key in the [Meta developer dashboard](https://dev.meta.ai/api/auth/login) and supply it through the `MODEL_API_KEY` environment variable. Keep it out of YAML and the repository. In this cloud environment, enter the key securely in environment settings under the saved `MODEL_API_KEY` requirement, scoped to `api.meta.ai`; save and publish the changes. On your Mac, export the variable in the shell that runs Emend, preferably through your password manager. `emend doctor` reports presence without printing the key.

The default is **`muse-spark-1.3`**, Meta's Standard mode (not Contributor mode). Meta bills API usage separately; this is optional and is not needed for local inspection, manual curation, publication, or Calibre backups. Model availability and pricing can change; see Meta's [models](https://dev.meta.ai/docs/models) and [pricing](https://dev.meta.ai/docs/pricing) documentation.

Only the selected title/author/language/identifier/publisher/date metadata, normalized edition records, proposal values, source URLs/fingerprints, and unavailable-provider names are sent. EPUB files, chapter text, images, descriptions, local paths, and credentials are excluded from the prompt. The model has no tools or web access in this workflow. Requests use Meta's documented HTTPS endpoint, preserve proxy/TLS verification, reject redirects, and never fall back to another service or model.

Advice can recommend an **existing** proposal, retain the current value, or mark it uncertain. Emend checks proposal coverage, cited record IDs, retained evidence, and conflicting recommendations. These checks establish where the advice came from; they do not establish that a model's factual judgment is correct. AI never changes a proposal's `safe` classification, approves an edit, or publishes a book. Inspect the evidence, then select a proposal explicitly:

```sh
uv run emend proposals BOOK_ID --plan PLAN_ID
uv run emend apply BOOK_ID --plan PLAN_ID --approve PROPOSAL_ID
```

Completed reviews are reused for the same plan and request. `--refresh` can incur another API charge. Failed or timed-out calls are not automatically retried; a timed-out request may already have been billed. Truncated/refused responses, invented proposal IDs, unsupported citations, and malformed JSON are rejected while the previous book state remains intact. Research, advice, and approved artifacts remain separate records. Saved review JSON lives under `research/BOOK_ID/ai-reviews/` and records the exact request, model, prompt version, response hash, token usage when supplied, and the structured answer; authentication headers and internal reasoning are not saved.

```yaml
ai_review:
  model: muse-spark-1.3
  reasoning_effort: medium
  max_completion_tokens: 4096
  request_timeout_seconds: 90
```

The client has been tested against the documented contract using local response fixtures. Authenticated live generation still needs a configured Meta API key. Source providers can also be quota-limited: Google Books currently returns HTTP 429 from the development environment. Missing corroboration remains visible and prevents safe proposals.

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
