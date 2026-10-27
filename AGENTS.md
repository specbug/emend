# Working on Emend

Emend is a local, preservation-first EPUB inspection and curation tool. The Python
package and CLI are named `emend`. The target personal setup is a Mac mini M4 with
Calibre, complete-library backups on Google Drive, and an XTEINK X4 Pro reader.
Keep reader compatibility claims tied to actual device evidence.

## Start here

- Read [README.md](README.md) for commands, configuration, and supported behavior.
- Read [docs/roadmap.md](docs/roadmap.md) for scope and next milestones.
- Read [docs/validation.md](docs/validation.md) and
  [docs/deferred-validation.md](docs/deferred-validation.md) before claiming an
  integration or device workflow is verified.
- Check the working tree and current PR/branch state before editing. Preserve
  existing work. Keep this guide focused on durable instructions; record feature
  status and test results in the linked docs instead.

## Product invariants

- Preserve original EPUB bytes and their identity hashes. Curated edits must be
  explicit, narrowly scoped, independently checked, validated before activation,
  and rolled back on failure. Preserve unrelated resources, reading text, and
  spine order. Re-ingesting an original resets its curated artifact; account for
  this when designing refresh workflows. Use `revalidate` to refresh quality
  checks on the current bytes without discarding edits.
- Metadata, cover, and Meta AI advice must retain their evidence and provenance.
  Advice does not apply edits or promote approval. Meta integration uses the
  official Meta Model API; keep model/provider details in configuration and docs.
- Keep required publication gates distinct from advisory review. Missing,
  malformed, stale, incomplete, or failed evidence must not become a passing
  required check. An automated accessibility pass is not conformance certification.
- Tie saved reviews to the artifact they inspected. Verify the relevant identities
  and hashes before applying edits or publishing; keep publication retryable.
- A Calibre backup must retain the whole rich library: database, custom columns,
  all formats, covers, notes, and supplementary files. Keep the active library
  local; back up closed-library snapshots to Drive and restore into a new directory.
- Treat EPUBs, metadata responses, and tool reports as untrusted data. Retain
  bounded reads, archive/path checks, escaped report content, and disabled book
  scripts/network access in visual previews. Never commit books, credentials, or
  generated library/review data as development fixtures.
- Treat publication-quality typography and layout as a product requirement.
  Keep personal reference books and their locations private; never commit links
  or identifying details. Validator success alone does not establish visual quality.
- Preserve publisher typography. Emend report styling follows the existing
  [report CSS](src/emend/assets/report.css): warm neutrals, consistent spacing,
  readable line lengths, responsive layouts, and bundled licensed fonts. Check
  report changes at desktop and phone widths without horizontal overflow.

## Code map

- `inspect.py`, `pipeline.py`, `storage.py`, `models.py`: inspection, preserved
  artifacts, lifecycle state, and saved evidence models.
- Missing-cover creation deliberately adds a first spine page; preserve all original resource bytes and their relative order, and carry `cover_additions` through later edits. Refuse existing cover-page hints and fixed-layout books.
- `repairs.py`, `navigation_repair.py`: explicit packaging/navigation repair plans,
  independent payload/XML preservation, validator gates, and retained revisions. Carry `repair_changes` through later
  edits; never apply repairs while preparing a plan. Navigation repair must change
  only the approved attributes and EPUB 3 modification timestamp, with independent
  tree comparison and unchanged reading text/unrelated resource bytes.
- `inbox.py`: stable-file batch/foreground ingestion. Always use the atomic
  `ingest_new` path for automation; ordinary `ingest` intentionally resets books.
- `research/`, `curation.py`, `covers.py`, `cover_apply.py`, `cover_create.py`: discovery, advice,
  explicit metadata/cover edits, and rollback.
- `validation.py`, `revalidation.py`, `accessibility.py`, `visual.py`,
  `pagination.py`: validators, refresh of current-artifact approval, Ace evidence
  parsing, and advisory browser review.
- `typography.py`, `typography_apply.py`: explicit numeric trials and approved
  CSS application. Keep `typography_changes` through later edits; verify stylesheet
  declarations independently and preserve all text/unrelated resources. The current/trial
  captures must share one verified snapshot and evidence must remain hash-checked.
- `reading_review.py`: bounded offline font/marked-note/direction evidence.
  Keep source observations distinct from glyph coverage, reader behavior and
  accessibility conformance; limits must produce incomplete evidence.
- `review_guidance.py`: bounded, read-only user guidance and embedded-cover observations. UI readiness is advisory; downloads and transfers must still use authoritative core gates.
- `review_ui.py`: loopback-only authenticated workspace. Preserve Host/Origin/CSRF
  checks, bounded uploads, isolated evidence, and core mutation gates. GET must
  never trigger edits, provider calls, publishing or other user actions.
- `reports.py`, `library.py`, `assets/`, `cli.py`: local reports, overview, and CLI.
- `recovery.py`: restore only the original or a previously approved retained revision. Revoke approval before replacing bytes, quarantine the previous current artifact, and rerun current gates. Carry `recovery_changes` through subsequent edits.
- `reader_export.py`: byte-verified mounted-folder delivery. Keep native atomic
  no-clobber behavior, directory-descriptor checks, retry reconciliation and
  shared publication gates; never replace unknown files or infer device support.
- `publish.py`, `backup.py`: delivery and full-library backup/restore.
- Paths above are relative to `src/emend/`. `tests/epub_factory.py` supplies small
  original EPUB fixtures; use those for reproducible regression cases.

## Development and validation

Use Python 3.12+ and `uv`; keep dependencies and `uv.lock` in sync. From the repo:

```sh
uv sync --frozen --extra visual
uv run --extra visual ruff check .
uv run --extra visual ruff format --check .
uv run --extra visual pytest -q
uv build
```

Start with tests relevant to the change. Run the full suite for changes spanning
preservation, validation, editing, or publication. For documentation-only changes,
check referenced paths and commands; do not add tests that merely restate prose.
Report skipped integrations separately from passing checks.

| Integration | Setup and test opt-in |
| --- | --- |
| EPUBCheck | Java 21 and `bash scripts/setup-cloud.sh`; set `EMEND_TEST_EPUBCHECK_JAR` to the installed JAR. The script verifies the official distribution checksum. |
| Browser review | `uv run --extra visual playwright install chromium`; set `EMEND_TEST_PLAYWRIGHT=1`, or use `EMEND_TEST_CHROMIUM` with an existing Chromium executable. |
| DAISY Ace | Node.js 22+ and `bash scripts/setup-ace.sh`; set `EMEND_TEST_ACE` to the installed `node_modules/.bin/ace-cli`. Its Puppeteer browser is separate from Playwright. |
| Calibre | On Linux x86_64 and macOS (Apple silicon/Intel), `bash scripts/setup-calibre.sh` installs a checksum-verified official build in an isolated tools directory. Set `EMEND_TEST_CALIBRE_BIN` to the directory containing `calibredb`, `ebook-meta`, `ebook-convert`, and `calibre-debug`. Mac tools are inside `calibre.app/Contents/MacOS`; other platforms can use an existing installation. Tests use disposable libraries. |

[Mac verification](docs/mac-validation.md) documents the repeatable disposable-library command and later personal/device acceptance.

[CI](.github/workflows/tests.yml) is the reference for the combined macOS/Linux
integration setup. Never weaken assertions or substitute a mocked success for a
blocked external check. Exercise real Calibre/Drive/device workflows only within
the user's authorized scope; deterministic fixtures do not establish account or
hardware access.

In managed cloud sessions, use the existing isolated checkout. Set
`UV_CACHE_DIR=/workspace/.cache/uv` if the default cache is unwritable. Setup
scripts default to `/workspace/.tools`; override `EMEND_TOOLS_DIR` and
`EMEND_ACE_DIR`/`EMEND_CALIBRE_DIR` with writable paths elsewhere. Inspect current runtime/network
policy, preserve injected authentication and proxy settings, and never print
secret values. Cloud Chromium may block Ace's `file:` navigation; use hosted CI
for the unchanged CLI integration and record the local limitation.

## Changes and PRs

- Prefer focused branches from the latest merged `main`. Explicitly fetch
  `main:refs/remotes/origin/main` when the checkout's fetch configuration only
  tracks `HEAD`. Stack PRs only when there is a real dependency and name the base.
- Continue authorized implementation, fixes, and routine verification without
  repeated confirmation. Ask when a missing decision genuinely blocks progress.
- Describe the concrete behavior change and meaningful validation in each PR.
  Keep drafts while work is ongoing; mark finished PRs ready for review. The user
  reviews and merges—do not merge automatically.
- Update user-facing docs when behavior or setup changes. Avoid stale test counts,
  transient PR IDs, secrets, or session-specific progress notes in this file.
