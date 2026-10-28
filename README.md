# Emend

A local, preservation-first EPUB workflow for macOS (including Apple Silicon) and Linux. Emend keeps original books unchanged, produces auditable quality reports, and publishes only after required checks pass. It also backs up and restores **complete Calibre libraries**, including their database, covers, metadata, custom columns, and every stored book format.

Emend includes the reliable foundation, evidence-backed metadata curation, explicit edition/cover review, and optional offline visual sampling from [the roadmap](docs/roadmap.md). It runs locally, records evidence for proposed edits, and keeps publication explicit. Optional Meta Model API reviews provide evidence-linked advice; device certification is not implemented.

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

### Accessibility findings

With Node.js 22 or newer installed, install the standalone DAISY CLI:

```sh
npm install -g @daisy/ace-cli@1.4.6
command -v ace-cli
```

Set `tools.ace` to the absolute executable path printed above. An existing `ace` executable on `PATH` also works. The CLI installation includes its Puppeteer browser; this is separate from Emend's optional Playwright visual renderer. `scripts/setup-ace.sh` installs the pinned CLI in `EMEND_ACE_DIR` for integration testing.

On ingestion, `revalidate`, or explicit metadata/cover application, Ace findings appear in the quality report's HTML, text, and JSON: rule, impact, document, and CSS selectors when supplied by Ace. Reports record the Ace version and raw JSON fingerprint. The original Ace report remains beside the quality report under `ace/report.json`; document/report summaries are not counted again as separate violations. A required audit blocks publication on violations, unresolved outcomes, malformed evidence, or a failed process. An optional failed audit stays visible in the library overview without blocking publication.

Set `quality.require_accessibility_audit: true` to require the audit, then run `emend revalidate BOOK_ID` to refresh an existing book while retaining its curated edits. Existing reports remain readable and do not gain findings retroactively. A clean automated audit still needs manual screen-reader, reading-order, image-description, navigation/footnote, contrast, and resizing review. Emend does not add accessibility conformance metadata or rewrite the book.

Emend reads at most 32 MiB of Ace JSON, rejects malformed or excessively nested assertion trees, and retains at most 500 finding details with bounded text/selectors. Counts include the entire parsed audit; shortened summaries are labelled and full evidence remains in the raw report. These bounds apply to Emend's parser, not Ace's own output generation.

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

## Open the local review workspace

```sh
uv sync --frozen --extra visual
uv run --extra visual playwright install chromium
uv run --extra visual emend review
```

Open the session URL printed in the terminal. The workspace binds only to `127.0.0.1`; keep that URL private and stop the server with Ctrl+C. Use `--port 0` to select an available port. No separate web account or subscription is needed.

Choose or drop an EPUB (up to 128 MiB, or your configured archive limit if lower).
The import starts at once and runs by itself; the page follows its progress:

1. **Keep and check:** the original is stored byte for byte, inspected, and checked
   by the required validators.
2. **Repair:** when required checks fail, Emend prepares a repair plan and applies
   each supported repair (EPUB packaging, case-only contents links, empty EPUB 2
   root languages and block-only paragraph wrappers) through the same gated path as
   `apply-repairs`. Several supported problems are repaired together in one plan. Each candidate is independently compared with the current book
   and must pass the required validators; a rejected candidate leaves the previous
   version in place and the page shows the errors found in that candidate.
3. **Preview:** reader-sized paginated pages (scrolling for books paginated preview
   cannot handle) are rendered from the current file, followed by the font and
   footnote inventory.
   **On your X4 Pro** lists covers and images in formats CrossPoint cannot decode
   (it shows only JPEG, PNG and GIF).

Each book page shows six steps: **Import, Check, Repair, Preview, Refine, Send**.
It opens on the step that needs you; a checked book opens on its pages, with
**Download EPUB** in the header. When something stops the book, **What went wrong**
gives the validator's actual messages and file locations, what Emend already tried,
and ordered next steps (tool setup, or the manual Calibre edit for errors without a
supported repair). **Run everything again** re-checks the current file and retries
the supported repairs. Known books open without resetting their curated edits.

**Refine** is optional and comes afterwards. Choose a spacing (Compact, Comfortable,
Roomy or exact values) to render the baseline and adjusted pages from the same file,
then compare them side by side or one at a time. Nothing changes until **Apply this
spacing**, which applies to every chapter unless you untick it and re-renders the
pages. After applying, the comparison stays as baseline versus adjustment, and
**Return to the baseline** restores the previous checked version through recovery.
Edition lookups, suggested details and covers sit in the same step and still need an
explicit save. **Text size on your reader** resets publisher body-text sizes that shrink
or enlarge the size chosen on the device; see below. Typeface is a reader setting on
CrossPoint, which ignores fonts embedded in a book.

**Send** downloads the checked EPUB for CrossPoint's file-transfer page, or uses
configured Calibre/Drive/reader-folder destinations. Sending stays disabled while
required checks are unresolved; a refused action keeps you on the book with the
blockers visible. The workspace accepts EPUB files only; convert PDFs elsewhere first.

**Download EPUB** rechecks the same approval, policy, engine and identity gates as
publication, then streams a hash-verified bounded snapshot. It does not call Calibre,
change the book's approval, or record a completed device delivery. Calibre can stay
open for import, review, preview and download. Direct **Add to Calibre** still uses
`calibredb` and needs Calibre closed; a lock error explains the download/GUI-import
alternative. Complete-library backup is a separate collapsed option that requires
closing Calibre and other writers before taking the snapshot.

The cover check inspects the image declared inside the EPUB, rather than assuming
Calibre's separate library thumbnail is embedded. It reports absent/unsupported
cover declarations, format/extension mismatches and available JPEG/PNG dimensions.
These observations are advisory, not device certification. See
[CrossPoint covers](docs/crosspoint-covers.md) for library-grid/sleep troubleshooting.

The workspace binds to loopback with session/CSRF/origin/Host checks and isolated
saved reports. GET never starts lookups, edits or checks; page images are served only
after their recorded hashes match. One run works at a time in the background and
holds the workspace lock; other actions wait for it. Pages update through a small
same-origin script and still work without it. The CLI remains available for larger
imports, recovery and advanced inspection.

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

Structural errors or failed required validators produce `FAILED`. Missing required tools produce `REVIEW_REQUIRED`. Neither state can publish. Optional check failures remain visible but do not block publication. EPUBCheck is required by default; do not disable it if you need standards validation. Optional visual review produces advisory browser samples; no “Gold,” edition-match, or device-readiness certification is issued.

Warnings about publisher styles, missing alt text, duplicate chapter text, or replacement characters are observations; Emend never silently rewrites them. Multi-rendition, DRM-encrypted, scripted, responsive-source-set, and remotely embedded content require manual review in this version. Font obfuscation is retained.

Calibre’s **Check book** and EPUBCheck run different checks. Calibre can report
“No problems found” while the same EPUB still fails EPUBCheck. For unsupported
errors, inspect the reported source lines in **Edit book**, then save and import
an edited EPUB copy. See [EPUBCheck troubleshooting](docs/epubcheck-troubleshooting.md).

## Import from an inbox

```sh
uv run emend inbox                         # One batch from ~/Emend/inbox
uv run emend inbox --watch                 # Foreground polling; Ctrl+C stops
uv run emend inbox --directory ~/Downloads/EPUB-Inbox --watch --stable-seconds 3
uv run emend inbox --json
```

The inbox imports top-level `.epub` files after two unchanged observations, two seconds apart by default. It leaves source files in place and ignores hidden files, symlinks, subdirectories, and other extensions. A quiet interval is a heuristic: download as `book.epub.part`, then rename to `book.epub` when finished. Copies are bounded and checked again for changes before ingestion. Custom directories must already exist and cannot be inside managed data (except the inbox) or the configured Calibre library. Each scan allows at most 10,000 directory entries; archive-size limits still apply.

Exact copies of known originals **or retained curated revisions** are skipped under the same lock used for importing. Renaming a file or restarting the watcher does not reset curated edits or rerun validators. `known` means retained data exists, not that its current quality gates pass. Failed or incomplete imports remain available for explicit investigation: use `report` and `revalidate` when a complete report exists. An interrupted import without a complete report needs recovery from its original; ordinary `ingest` resets curated edits, so inspect retained data before choosing that path.

Batch mode uses one observation window and exits `1` if files are still changing, blocked, busy, or errored; retry later. It exits `0` for imported/known files or an empty inbox, and `2` for command/configuration errors. `--json` returns one batch object; `--watch --json` emits newline-delimited events, suppressing repeated unchanged statuses. Watching processes books sequentially and retries busy operations on subsequent scans. It runs only while the command is open; there is no background service. Ingestion never researches, edits, or publishes automatically.

## Find reports and pending review work

```sh
uv run emend library
uv run emend library --json
uv run emend library --limit 100 --offset 100
```

Open `~/Emend/library.html` for an offline overview of recent books, recorded quality state, metadata proposals, edition candidates, cover comparisons, visual samples, and saved Meta advice. Reports link to existing local files. Labels distinguish current evidence, earlier revisions/policies, applied changes, incomplete captures, and missing or altered evidence. The page suggests inspection commands; it never applies edits or publishes books.

The default shows 200 books, newest updated first. `--limit` accepts 1–1000 and `--offset` moves through larger libraries. HTML and JSON snapshots are regenerated on demand; `--json` prints only the snapshot. Registered review JSON is checked against its stored hash, while EPUB/image bytes are rechecked by the relevant apply, visual-review, and publishing workflows. This overview does not certify current file integrity or replace those gates. Keep it inside the Emend data directory so relative report links continue to work.

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

`reset-original` keeps history and external copies; if revalidation fails, publication is blocked until the required gates pass. Books without a valid ISBN use the edition-discovery workflow below. Failed providers remain visible and prevent safe proposals; research exits nonzero when a provider fails. Research itself only records cover URLs. The separate cover-review workflow downloads candidates and supports explicit replacement. Author/identifier edits and typography changes are deferred.

Optional configuration:

```yaml
metadata:
  sources: [openlibrary, google_books]
  cache_days: 7
  request_timeout_seconds: 15
```

Publishing an edited artifact creates a separately identified revision in Calibre and a hash-named EPUB in Drive. It does **not** overwrite an existing Calibre entry or migrate annotations between revisions. Each revision/destination has its own retryable receipt. Full-library backup continues to preserve all entries and their stored data.

Upgrading from 0.1: old reports require `revalidate BOOK_ID` with the new engine. SQLite research tables are added without deleting existing books or receipts. Back up the Emend data directory separately if you want to preserve its research/history; Calibre-library backups cover the Calibre library itself.

## Refresh validation without undoing edits

```sh
uv run emend revalidate BOOK_ID
uv run emend revalidate BOOK_ID --json
```

Use this after installing a validator, changing quality policy, or upgrading the validation engine. It checks the **current curated EPUB**, preserves its exact bytes and metadata/cover history, and records fresh inspection, EPUBCheck, and Ace evidence. If an initial import never produced a curated artifact, it checks the preserved original. It refuses missing or mismatched input hashes; it does not adopt an externally modified file as a new approved revision.

Revalidation commits a `REVIEW_REQUIRED` report before running inspection or external tools. Missing required tools leave the book in `REVIEW_REQUIRED`; failed required gates leave it `FAILED`. The current EPUB stays retained for review and retry, while publication and further curation remain blocked. Fix the reported problem and rerun the same command. If the process is interrupted or the final report/state cannot be saved, the pending report continues to block publication and identifies the retained book for a retry. Preflight failures leave the earlier record intact; publishing independently checks policy and input hashes.

Validators receive a bounded temporary copy. Emend checks that copy and the stored inputs for unexpected changes. Existing reports and publication receipts remain in history; successfully rechecking unchanged bytes does not create a new delivery revision. Visual/research evidence remains tied to its recorded bytes and its own policy/version checks. No publishing happens automatically, and copies already delivered to Calibre or Drive are not withdrawn by a failed audit.

In a revalidation report, `artifact_sha256` identifies the retained bytes **even when validation fails**. Approval requires `state: VALIDATED`, matching policy/engine, and passing required gates. Use `reset-original` or re-ingest an original only when you intend to discard that book's current curated edits.

## Review and apply an EPUB repair

A book rejected because its existing, correct `mimetype` entry is compressed or appears after other ZIP entries can now be repaired explicitly:

```sh
uv run emend repair-plan BOOK_ID
uv run emend repair-plan BOOK_ID --json
# Open the reported review.html and inspect the proposed packaging changes.
uv run emend apply-repairs BOOK_ID --plan PLAN_ID --approve container.mimetype
```

The plan records the current EPUB/report hashes, quality policy, and inspection findings. It shows current/proposed packaging details for container repairs, each old/new link and its existing target for navigation repairs, or each affected file/element for HTML repairs. Preparing it changes neither book bytes nor approval. Plans live under `~/Emend/repairs/BOOK_ID/PLAN_ID/`; a changed book, report, policy, or engine requires a new plan. `--approve` is mandatory, and `--plan` selects the exact saved proposal.

**Fix HTML markup** supports two narrowly scoped repairs in structurally readable books:

- For EPUB 2, fill existing empty root `lang`/`xml:lang` attributes from one
  unambiguous package language. Nonempty document languages, local overrides and
  missing attributes stay unchanged. EPUB 3 permits empty language values, so
  those are preserved.
- For EPUB 2/3, replace a paragraph wrapper directly inside `body`/`div` with a
  `div` when it contains only headings/lists and whitespace. Mixed prose, inline
  parents and other block types remain for manual review. IDs, classes, links and
  other attributes are retained; stylesheets are not changed. Element-specific
  CSS can render the repaired wrapper differently, so preview the affected pages.

The review workspace applies this repair automatically after import or **Run everything again**, or:

```sh
uv run emend repair-plan BOOK_ID
uv run emend apply-repairs BOOK_ID --plan PLAN_ID --approve xhtml.markup
```

The entire candidate must pass required checks before activation; unsupported
remaining errors reject the candidate and retain the previous book. Discovery
limits (1,000 XHTML files, 8 MiB per file, 50,000 elements per file, 500 edits)
refuse a partial proposal. Unrelated resources remain byte-identical; independent
XML comparison permits only reviewed attributes/wrappers and, for EPUB 3, the
modification timestamp. This is not a general HTML or language-detection engine.

The `container.mimetype` action moves the existing `mimetype` entry first, stores it uncompressed, and removes its ZIP extra fields. **Every resource payload stays byte-identical**, including OPF metadata, XHTML, CSS, images, fonts, unmanifested files, and directory records. Other entries retain their relative order, compression, timestamps, extra fields, and comments; the archive comment is retained. ZIP compression/header bytes may differ, so the repaired EPUB has a new hash. Original bytes and pre/post repair revisions are retained.

The container action supports the built-in `zip.mimetype_order` and `zip.mimetype_header` errors. The only other structural errors it allows are contents-link targets that a later navigation step in the same plan repairs. Missing/incorrect mimetype content, unsafe/duplicate entries, encryption problems, malformed XML, and other defects require manual review and receive no supported action. Local extra-field-only defects and prepended container bytes are detected and proposed explicitly. It does not repair typography, text, navigation, or missing resources; the separate navigation action below handles one specific link defect.

When a book has more than one supported problem, one plan lists every action in a fixed order: packaging, then contents links, then HTML markup. Preparing the plan simulates the steps on staging copies, each discovered on the previous step's output. A packaging repair can therefore expose link or markup problems that inspection or EPUBCheck could not reach before. A chain whose final copy would still be structurally unreadable is not offered. `--approve` must name every action in the plan. Applying replays exactly the planned steps and checks each one with its own preservation comparison. Only the final copy is validated and activated; if any step diverges or fails, the whole chain is rejected and the book is unchanged. The report records one `repair_changes` entry per step, linking the original hash to the activated hash.

Applying checks preservation independently and runs the configured validators before activating the candidate. Failure keeps the previous book, report, and approval state, while saving rejection evidence. Repair history survives subsequent metadata/cover edits and revalidation. Repeating a successful application is idempotent; it cannot clear later failed/pending gates—use `revalidate` for those. Publishing the repaired revision remains explicit. The apply command exits `1` for rejected quality gates and `2` for invalid requests or operational errors.

The `navigation.case_paths` action fixes case-only resource paths in the declared EPUB 2 NCX or EPUB 3 table of contents:

```sh
uv run emend repair-plan BOOK_ID
# Inspect each old/new link in review.html before approving this action.
uv run emend apply-repairs BOOK_ID --plan PLAN_ID --approve navigation.case_paths
```

For example, `CHAPTER.xhtml#start` can become `chapter.xhtml#start` when there is exactly one matching archive path, that path is an XHTML spine document, and `start` exists. Fragment spelling/encoding and link labels stay unchanged. Up to 200 fixes can be approved together; the plan lists every one. A candidate must fix all structural errors. This version does not combine container and navigation actions in one plan.

Ambiguous case collisions, nonexistent resources/fragments, fragment-case mistakes, query strings, broken body/footnote links, non-spine destinations, unsupported XML, and other damage get no navigation proposal. External links are left alone. Each link/document/target value is limited to 2,000 UTF-8 bytes. Emend neither guesses chapters from titles nor generates new IDs.

Navigation repair changes only the listed XML link attributes and EPUB 3's modification timestamp. Independent XML comparisons include labels, comments, processing instructions, namespaces, doctype, and unrelated attributes/text; every other resource stays byte-identical. Changed XML documents are serialized as UTF-8, so declaration/formatting bytes may differ. Reading text, OPF descriptive metadata, styles, images, and spine order are retained. These repairs do not certify reading order, footnote behavior, typography, or device compatibility.

Rollback is tested for application errors, including a database failure after artifact replacement. Machine/power-loss durability and forced interruption during replacement are not established; a byte/report mismatch blocks further use and requires recovery from the retained revision. These checks establish container preservation and standards validation, not visual quality or reader compatibility.

## Discover editions when an EPUB has no ISBN

The edition/cover workflow passes preservation, rollback, and official EPUBCheck tests, with live Open Library checks. See [validation evidence](docs/validation.md) and [remaining integration checks](docs/deferred-validation.md).

```sh
uv run emend discover BOOK_ID
uv run emend editions BOOK_ID
uv run emend editions BOOK_ID --json
uv run emend research BOOK_ID --discovery DISCOVERY_ID --edition RECORD_ID
uv run emend proposals BOOK_ID
```

`discover` searches Open Library and Google Books using the existing title and author. Supply `--title "…" --author "…"` when metadata is absent or multi-valued; these arguments change the query, not the EPUB. Use `--offline` for fresh cached responses or `--refresh` to fetch new evidence. If a valid ISBN already exists, use exact-ISBN `research` instead.

Results show title/author/language/publisher/date agreements and differences. Open Library work-level search results are expanded into concrete edition records; they are never used as edition metadata directly. Searches are bounded to the first five Open Library work results (up to two editions each) and ten Google Books records. Ordering is not a confidence score, and other editions may exist.

Select one saved record explicitly with `research --edition`. The resulting plan records `match_mode: reviewed_edition`, the selected record, discovery ID, and source evidence. **Every resulting metadata edit needs explicit approval**, even when fields agree. Candidate ISBNs stay evidence about the candidate; they are never inserted into the EPUB. Meta review sees the match mode and cannot turn a search selection into automatic approval. Stale or altered discoveries must be rerun.

## Compare and replace an existing cover

After research, prepare a self-contained comparison page from the recorded Open Library cover candidates:

```sh
uv run emend cover-review BOOK_ID --plan PLAN_ID
uv run emend covers BOOK_ID
uv run emend covers BOOK_ID --review REVIEW_ID --json
```

Open the reported `review.html` locally. It shows current artwork and candidates, source edition details, native dimensions, byte counts, and a perceptual-hash distance. That distance is a rough layout comparison; it does not prove that the artwork or edition matches. Images are sorted by format compatibility and pixel area, not automatically selected. Inspect the title, edition, crop, artwork, and small text yourself.

You can also supply a local image with a provenance note; this needs no metadata provider or AI key:

```sh
uv run emend cover-review BOOK_ID --image /path/to/cover.jpg --source-note "Publisher-supplied cover for this edition"
uv run emend apply-cover BOOK_ID --review REVIEW_ID --approve CANDIDATE_ID
```

`--approve` means you have reviewed the candidate's artwork and edition. This path supports **one already-declared JPEG or PNG cover**, and the replacement must have the same format. It leaves the resource path, manifest, cover-page XHTML, CSS, and all unrelated resource bytes unchanged. It does not invent cover declarations, alter SVG wrappers, transcode files, upscale thumbnails, or rewrite layout. JPEG/PNG candidates are bounded to 20 MiB and 40 megapixels; animated images, unusual colour modes, and EXIF-rotated candidates need manual preparation before applying.

Smaller images and aspect-ratio changes above 5% are blocked by default. After checking the consequences, `--allow-smaller` and `--allow-aspect-change` record explicit exceptions. There is no automatic visual or device-readiness certification. Existing cover-page dimensions may distort a candidate with a different aspect ratio, so that exception needs particular review.

Applying a cover advances the EPUB 3 modification timestamp, checks content/resource invariants, and reruns required validators. On failure it retains the previous approved artifact and saves rejection evidence. Original bytes and prior revisions remain available; quality reports record old/new image hashes, dimensions, provenance, and any exceptions. Repeating the same successful selection is idempotent. Replacing a cover creates a new artifact revision, so the normal explicit publishing and full-library backup rules still apply.

Remote review downloads up to eight recorded Open Library covers; failures remain visible. `covers` reads a saved review without downloading again. Downloaded images and local inputs are retained by hash, and altered review files/assets cannot be applied. Preview images are scaled only in the report; replacement bytes stay untouched.

## Review EPUB layout offline

Install the optional renderer once:

```sh
uv sync --frozen --extra visual
uv run --extra visual playwright install chromium
uv run --extra visual emend visual-review BOOK_ID
uv run emend visuals BOOK_ID
uv run emend visuals BOOK_ID --json
```

Open the reported `review.html` file for a self-contained screenshot contact sheet. The default samples up to five documents, prioritizing the beginning, middle, and end of the spine, then navigation and additional positions. Each document is captured at **600 × 800** and **1000 × 1400** CSS pixels, with up to three vertical offsets. Select fewer samples with `--profile compact --max-documents 3`; repeat `--profile` to choose both sizes. PNGs and hash-registered JSON live under `~/Emend/visual/BOOK_ID/REVIEW_ID/`.

For numbered page previews of horizontal, left-to-right, reflowable books:

```sh
uv run --extra visual emend visual-review BOOK_ID --layout paginated
uv run --extra visual emend visual-review BOOK_ID --layout paginated --page-margin 48 --profile compact
```

Paginated mode uses Chromium's CSS columns to sample the first, middle, and last page of each selected document. Page numbers restart per document; they are not book-wide locations. `--page-margin` accepts 16–120 CSS pixels (default 40), and the report records the margin, page count, and exact horizontal capture offsets. The default `--layout scroll` keeps the existing vertical-sampling workflow; older saved reviews remain readable.

Only the preview DOM gets reader geometry overrides: page width/height, margins, columns, and forced page-break adaptation. Animations are paused at their initial frame before measuring pages. Publisher text and font styling are retained, and no EPUB bytes are changed. Reports flag oversized images/tables, text crossing page margins, vertical spillover, and positioned elements. Fixed-layout declarations and right-to-left page progression are rejected before launching the browser; vertical/RTL document flow and column spanners produce an explicit incomplete review. These cases remain available in scrolling mode.

Pagination is capped at 5,000 pages and 20,000 elements per document, with text-edge inspection limited to 50,000 fragment rectangles. The existing screenshot and time budgets still apply. Left/right blank-page parity, interactive footnote placement, and full-book pagination are not emulated.

The renderer uses the EPUB's own XHTML, CSS, images, and fonts from a temporary verified copy. No EPUB changes or uploads occur. A browser context with networking and JavaScript disabled serves only manifest resources from the archive through intercepted requests. Nothing is extracted into a web server. If Chromium is already installed, set `tools.chromium` to its executable path instead of downloading Playwright's browser. `doctor` reports optional renderer availability; actual launch is checked by `visual-review`.

Review hints identify horizontal overflow, failed HTML images/font faces, and visible text elements below 10 CSS pixels. Resource reads are capped at 8 MiB each, screenshots at 48 MiB per review, and sampling stops after the configured tool-time budget (at most 180 seconds, plus any in-flight browser operation). Missing browsers fail explicitly; interrupted captures are saved as **incomplete**, and the command exits nonzero. Warnings in a completed capture are advisory and do not alter publication state. `visuals` verifies report/screenshot hashes and exits nonzero if the book has changed since capture.

Both layouts are generic Chromium previews. Compact/wide profiles do not emulate the X4 Pro or Apple Books. Obfuscated fonts, fixed layouts, vertical text, footnote interactions, and unsampled content need reader review. Browser/OS font differences can affect pixels; the report records Chromium's version and viewport sizes. Visual review remains separate from required EPUBCheck and accessibility gates.

## Compare typography without changing the EPUB

```sh
uv run --extra visual emend typography-trial BOOK_ID --line-height 1.6 --paragraph-gap 0.6
uv run --extra visual emend typography-trial BOOK_ID --line-height 1.7 --layout paginated --profile compact
uv run emend typography-trials BOOK_ID --json
```

Open the reported `trial.html` for current/trial screenshots from the **same verified snapshot**, with identical document/profile selections. At least one numeric setting is required: unitless body/p line height (1–2.4), and/or paragraph gap (0–2 em, with the paragraph's preceding block margin set to zero). There is no automatic style recommendation. Fonts, alignment, indentation, and other styles are retained. The generated rules use `!important`; more specific nested rules may still affect individual text. Paragraph rules also affect verse, captions, and notes marked as `p`, so inspect those explicitly.

The default samples up to three representative documents at compact/wide sizes; `--max-documents` accepts 1–9. Both scrolling and paginated previews retain their existing limitations. Each view samples its own document start/middle/end: different flow may change sampled text/page counts. These are not line-aligned comparisons or reader emulation. Each capture retains its own bounded time/screenshot budget, up to two renderer budgets overall.

Trials live under `~/Emend/typography/BOOK_ID/TRIAL_ID/` with numeric settings, the exact generated CSS, and hashed before/after screenshots. `typography-trials --trial TRIAL_ID` verifies saved evidence without launching the browser; it exits `1` for stale/incomplete evidence and `2` for invalid or altered inputs. A later EPUB edit requires a new trial. Creating a trial leaves book bytes, reports, approval, and publishing unchanged; it does not apply CSS to the EPUB. The separate `apply-typography` command below requires explicit review/approval.

## Apply a reviewed typography trial

```sh
uv run emend apply-typography BOOK_ID --trial TRIAL_ID --approve
# Optional, broader scope after reviewing special content:
uv run emend apply-typography BOOK_ID --trial TRIAL_ID --approve --all-spine
```

The default edits only the documents sampled in the named trial. `--all-spine` explicitly extends the same CSS to all XHTML spine documents, including unsampled chapters; review verse, tables, captions and notes first. A complete, intact trial for the current artifact and a validated current book are required. Typography choices are personal/editorial decisions; Emend does not certify them as improvements.

Application adds the exact trial CSS as a manifest resource, appends stylesheet links to approved XHTML heads, and updates the EPUB 3 modification timestamp. It independently checks the XML trees and resource inventory: reading text, labels, fonts, images, existing CSS, metadata and spine order must remain unchanged. Existing stylesheets remain available; the new numeric rules participate in the CSS cascade exactly as in the trial. A different trial can layer further changes; `reset-original` restores original bytes and clears active edits after explicit review.

Required validators run before activation. Rejected candidates preserve the previous artifact/report and retain evidence. Originals and pre/post revisions remain available; application errors roll back, including database/copy failure after replacement. Repeating the same successful trial/scope is idempotent. Typography history survives later metadata edits, cover changes, repairs and revalidation. Changed artifact hashes make earlier visual/metadata proposals stale. Publishing remains explicit. Forced-interruption/power-loss durability during activation remains a separate validation task.

## Reset body text to the reader's size

```sh
uv run emend text-size-trial BOOK_ID
uv run emend apply-text-size BOOK_ID --trial TRIAL_ID --approve
```

CrossPoint applies a book's relative `font-size` rules on top of the size chosen on
the device. It ignores `!important` and fonts embedded in the book; see
[CrossPoint typography](docs/crosspoint-typography.md). The trial finds `font-size`
values in plain `html`, `body` and `p` rules of UTF-8 stylesheets that scale that size.
It builds a candidate with only those values reset (`1em`, `1rem` or `100%`) and
renders the current book and the candidate from one verified snapshot. Class and id
rules, headings, inline styles, `@media` blocks, the `font` shorthand and absolute
`px`/`pt` sizes are listed and left as published. A root-size chain such as
`html { font-size: 62.5% }` with `rem` sizes elsewhere is left alone.

Applying requires a validated book, a complete current comparison and `--approve`.
It replays the same edits and independently checks that every byte outside the
planned values matches, both as bytes and as parsed rules. Other files must be
identical apart from the EPUB 3 modification timestamp, and the reading text, spine
and metadata are unchanged. Required validators run before activation; a rejected
candidate leaves the previous book in place. `text_size_changes` survive later
edits, and the workspace's **Return to the baseline** restores the earlier version
through recovery. Comparisons live under `~/Emend/text-size/BOOK_ID/TRIAL_ID/`.
Browser screenshots are not a device preview: check the result on the reader.

## Review fonts, notes and reading direction

```sh
uv run emend reading-review BOOK_ID
uv run emend reading-review BOOK_ID --show --json
```

This offline, advisory review records manifest font bytes/hashes, discovered font-face sources (including nested/inline styles), declared obfuscation, document language/direction, and explicitly marked note references. It looks for an existing target and a reciprocal backlink inside the note region; missing semantics/backlinks remain visible for manual inspection. Ordinary unmarked links are not guessed to be notes. Direction declarations are source hints, not a resolved CSS cascade.

The report changes no EPUB bytes, approval or publication state. It does not establish glyph coverage, real font loading, screen-reader conformance, footnote popup behavior, or device compatibility. Inspection is bounded to 1,000 XHTML documents, 200 fonts and 5,000 marked references; hitting a bound marks the evidence incomplete. Saved JSON is capped at 8 MiB and verified when reopened. `--show`/`--review ID` reads saved evidence without rescanning; stale/incomplete evidence exits `1` and altered/invalid input exits `2`.

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

The client has contract/failure tests using local response fixtures and passed an authenticated Muse Spark 1.3 smoke test on 2026-10-18 using synthetic edition metadata. Structured advice and citations validated; book bytes and approval state stayed unchanged, and a repeat review reused the cache without another API call. Source providers can also be quota-limited: Google Books currently returns HTTP 429 from the development environment. Missing corroboration remains visible and prevents safe proposals.

## Publish approved books to Calibre

Set `publish.calibre_library` to your existing local library folder. Close Calibre and other writers before using the CLI integration.

```sh
uv run emend publish BOOK_ID
uv run emend publish --ready
```

Publishing is explicit, separate from ingestion, and uses `calibredb`; Emend never writes Calibre's database directly. A `emend` identifier and per-destination receipts make retries idempotent. Existing books with the same title may coexist if their source bytes differ. Emend rechecks the original and artifact hashes and the quality policy before publishing, and verifies the stored EPUB after importing into Calibre. If an import plugin changes the file, delivery remains pending for review. If policy changes, run `emend revalidate BOOK_ID` first to retain curated edits.

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

## Export approved EPUBs to a mounted reader folder

```sh
uv run emend export-reader BOOK_ID --to "/Volumes/YOUR_READER/Books"
# Or enable the same explicit destination in the review workspace:
uv run emend review --reader-directory "/Volumes/YOUR_READER/Books"
```

Choose an existing folder on the intended reader/card. This is generic filesystem delivery; Emend does not detect hardware, guess firmware/network protocols, or certify XTEINK compatibility. Confirm the volume is mounted and use the operating system's safe eject before disconnecting.

Export shares publication's required-gate, policy, engine and source/artifact checks. It makes a bounded staging copy, flushes and hashes it, then uses the native macOS/Linux no-overwrite rename operation and verifies the visible result again. Unsupported filesystem operations fail explicitly. Directory identity checks detect a moved/remounted destination during delivery; a copy is not a full Calibre backup.

The default sanitized filename includes the approved artifact's hash, so a new revision gets a distinct file. `--filename NAME.epub` selects a plain filename. Existing exact bytes are verified and reused; different files/symlinks are never overwritten. Older reader revisions are not deleted automatically. Retry reconciles a completed copy even if its database receipt could not be saved. Receipts record point-in-time byte verification, not continuing remote integrity or power-loss durability. A disconnected volume may retain a hidden `.emend-*.part` file for manual cleanup.

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
  inbox/                    # Stable-file batch import / foreground watcher
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

The generated regression corpus contains EPUB 2/3 books, footnotes, illustrations, malformed containers, unsafe references, and damaged resources. Publisher boundary tests use local fake executables. Optional real Calibre tests create disposable libraries and exercise import, rich metadata/notes/annotations, backup, and restore. They do not access a personal library or Google Drive account. See [validation notes](docs/validation.md) for what has actually been exercised.

On Linux x86_64, install the checksum-verified official Calibre CLI and run those integration tests:

```sh
bash scripts/setup-calibre.sh
EMEND_TEST_CALIBRE_BIN=/workspace/.tools/calibre-9.17.0 uv run pytest -q tests/test_calibre_integration.py
```

Override `EMEND_CALIBRE_DIR` for another installation directory. On macOS, the tests can instead use an existing `/Applications/calibre.app/Contents/MacOS` directory. They isolate Calibre configuration and Qt runs offscreen. With rclone installed, the rich-library test also transfers its backup through a local backend before restoring it. Hosted Linux CI enables these checks; macOS CI explicitly skips them until given Calibre.

Exit codes: `0` success; `1` blocked quality gates or pending publication/upload; `2` invalid configuration, input, or operational error. No automatic publication occurs merely by inspecting or ingesting a book.

### Creating a missing cover

After `cover-review BOOK --image cover.png --source-note "Artwork provenance"`,
inspect the saved comparison, then explicitly approve its candidate:

```sh
emend create-cover BOOK --review REVIEW_ID --approve CANDIDATE_ID --config emend.yaml
```

This supports reflowable EPUB 2/3 with no declared cover or existing cover-page
hints. It adds the approved JPEG/PNG unchanged and a proportional cover page at
the front of the spine, preserving all existing resource bytes and their relative
reading order. Required validators run before activation; failures retain the
previous artifact. Sources below 600 pixels on their short edge require the
recorded `--allow-small` exception after checking readability. No artwork is
invented or matched automatically. Existing declared covers use `apply-cover`.
Both operations are available in the local review workspace.

### Recovering an interrupted or damaged current artifact

```sh
emend recovery-options BOOK --config emend.yaml
emend recover BOOK --revision original --approve --config emend.yaml
# Or choose a full retained SHA-256 from recovery-options instead of original.
```

Recovery accepts the preserved original or a retained revision backed by a saved
successful quality report. It verifies the selected bytes, revokes approval before
replacement, moves the previous current artifact into `quarantine/`, and reruns
current required validators. An interruption leaves publication blocked; retry
recovery, or use `revalidate` if the selected copy is already intact. The workspace
shows recovery controls for unavailable books and provides a recovery link for
intact ones. Restoring an earlier revision restores its edit history; newer edits
remain in retained reports/revisions. Quarantine files are evidence, never approved
recovery sources. Missing or damaged originals require the byte-identical source from your own backup.
Keep the Emend data directory backed up separately; Calibre-library backups do not
include Emend originals or review history.
Recovery does not restore the Calibre library: use the separate rich-library
backup/restore workflow for that.

### Release verification

[Mac validation and first real library/device review](docs/mac-validation.md)
provides the repeatable `scripts/verify-local.sh` command, optional isolated official
Calibre installation on Apple silicon/Intel, and the later private Calibre/Drive/
X4 Pro acceptance steps. CI runs actual Calibre on Linux and macOS. The complete
workflow tests use disposable libraries and synthetic EPUB 2/3 books.

Version 0.18 uses engine 3 to detect mimetype local extra fields and
prepended container bytes, and proposes their explicit packaging repair. Existing
approved books need `revalidate` before publishing with this engine; prepare new
reviews/trials before applying changes. Other structural defects remain manual.

## Convert images for CrossPoint

**Refine → Images and covers** offers an optional conversion when **On your X4
Pro** reports unsupported formats such as WebP. Choose **Compare image conversion**,
inspect baseline versus converted pages and the size/skip summary, then explicitly
**Apply image conversion**. This runs in the background, works without JavaScript,
keeps the comparison and offers **Return to the baseline**. Import never applies it.

```sh
uv run --extra visual emend image-conversion-trial BOOK_ID --json
uv run emend apply-image-conversion BOOK_ID --trial TRIAL_ID --approve
```

The trial needs the visual extra and Chromium, like other comparisons. Use
`--layout scroll` for books the generic paginated browser cannot sample. A trial
never changes approval. Applying checks its input/plan/screenshot hashes, replays
and independently verifies the changes, requires EPUBCheck plus your configured
accessibility gates, then activates or rolls back while retaining rejection evidence.
Conversion records survive later metadata, cover, spacing, text-size, revalidation
and recovery actions. Re-ingesting an original intentionally resets its curation.

Dimensions, ICC profiles, text, chapter order, IDs and publisher styles stay intact.
Lossless/alpha/palette/line-art images become non-interlaced PNG with exact decoded
pixels; lossy photos become baseline JPEG (quality 98, no subsampling, PSNR ≥ 40 dB).
References and converted manifest href/media types are updated together. Unsupported
animations/vectors/large or unreadable images are listed as skipped; unsafe reference
graphs refuse the whole conversion. No resizing, grayscale conversion or SVG
rasterisation occurs. See [CrossPoint image behavior and exact limits](docs/crosspoint-typography.md#images-and-covers).

The firmware observations are **from pinned source, not a device test**. X4 Pro and
iPad acceptance remains pending. Browser previews and validator success do not
establish hardware compatibility.
