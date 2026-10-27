# Validation evidence

## Local Apple silicon verification — 2026-10-27

The documented `scripts/verify-local.sh --require-ace` run passes **519 tests
with no skips** on macOS 26.6.2 arm64, using Python 3.12.16, Java 21,
EPUBCheck 5.3.0, the existing Calibre 9.16 installation, rclone 1.75.2,
Playwright 1.64.0 / Chromium 156, and Ace 1.4.6 / Puppeteer Chrome 148.
Lint, formatting, source distribution and wheel builds pass.

The initial run passed 518 cases and exposed a timezone-dependent assertion in
the real Calibre EPUB 2 integration. Calibre interpreted the date-only March 4
metadata as local midnight and serialized it as March 3 at 18:30 UTC on the
India-timezone host. The disposable Calibre fixture now explicitly sets `TZ=UTC`;
the exact date assertion is unchanged. All three Calibre integration cases pass
with an India-timezone parent process, followed by the complete passing run.
No EPUB editing or publication behavior was changed.

Actual tools exercise EPUB 2/3 repair, metadata and cover edits, typography
application, visual/reading evidence, revision recovery, native macOS no-clobber
export, Calibre import/retry, rich-library backup and restore, and rclone local
transport. All five official Ace cases pass, including clean audits, missing-alt
findings and revalidation. After repairing a stalled Puppeteer browser install
from its complete downloaded archive, these five cases also pass with the normal
Puppeteer cache and no executable override. The full run used the same Chrome
archive extracted separately.

The built wheel was installed into a separate environment using frozen cached
dependencies, then exercised from outside the repository through the CLI. A
synthetic accessible EPUB passed required EPUBCheck and Ace audits, revalidation,
reading review and paginated browser review. Repeated folder export and Calibre
publication were idempotent. A complete backup verified and restored into a new
library, which Calibre reopened; the restored EPUB and preserved original matched
the input SHA-256. Default workspace initialization and `doctor` also passed.

The browser tests verify import, cover application and recovery, session/security
boundaries and desktop/phone widths. Review and recovery screenshots at 1280 px
and 390 px were inspected: readable layout, wrapping hashes and no horizontal
overflow. These are synthetic fixtures and disposable libraries; personal books,
authenticated Drive, live providers/Meta, manual accessibility and X4 Pro hardware
remain separate acceptance checks.

## Earlier Linux evidence

Validated in the Linux cloud development environment with Python 3.12, Java 21, and the official EPUBCheck 5.3.0 distribution.

## Edition discovery and cover review (0.3)

Edition discovery and cover review/replacement (0.3) now have 22 additional tests. The full suite passes **157 tests**, including official EPUBCheck 5.3.0 checks on both EPUB 2 and EPUB 3 cover replacements. Tests exercise explicit edition selection, review-only proposals, identifier preservation, missing ISBNs, provider failure, stale/tampered records, cover resource preservation, size/aspect exceptions, invalid images, validator failure, database rollback, unrelated-resource mutation, and metadata/cover history.

On 2026-10-17, live Open Library title/author search expanded results into five edition records with 15 retained evidence documents. Results included translations and collections; they remain unconfirmed candidates. An actual recorded cover URL downloaded a verified 326 × 500 JPEG through the restricted downloader. The comparison report was rendered in Chromium at 1280 px and 390 px widths: images loaded at natural proportions and neither viewport had horizontal overflow. Lint, formatting, and package build also pass. See [remaining validation](deferred-validation.md) for limits.

## Offline visual review (0.4)

The visual extension adds 21 tests, including real Chromium rendering of EPUB 2/3 resources, controlled overflow/tiny text, long-document sampling, disabled scripts/external images, navigation rejection, missing browser, bounded screenshot failure, artifact/report preservation, stale/tampered screenshots, and CLI exit status. The full suite passes **178 tests** with official EPUBCheck and a real Chromium executable enabled.

A standalone fixture produced six samples across two viewports. Its contact sheet was rendered at 1280 px and 390 px without horizontal overflow and inspected visually. Lint, format, and package build pass. CI installs Playwright's Chromium and enables browser integration on Linux and macOS; local checks here used `/usr/bin/chromium`.

To exercise both external integrations locally:

```sh
uv sync --frozen --extra visual
uv run --extra visual playwright install chromium
EMEND_TEST_PLAYWRIGHT=1 EMEND_TEST_EPUBCHECK_JAR=/path/to/epubcheck.jar uv run --extra visual pytest -q
```

For an existing browser, use `EMEND_TEST_CHROMIUM=/path/to/chromium` instead of `EMEND_TEST_PLAYWRIGHT`. Browser tests skip explicitly without the optional dependency and one of these opt-ins. The renderer is scrolling XHTML sampling; it does not certify pagination, device rendering, accessibility, or publication quality.

## Library overview

The combined branch passes **187 tests**. Nine overview tests cover empty and populated libraries, non-mutating reads, proposal/application history, missing or changed evidence, policy changes, incomplete visual captures, Meta advice tied to intact plans, escaped metadata, local-link containment, and paging. A mixed failed/validated fixture library was rendered in Chromium at 1280 px and 390 px with no horizontal overflow; every generated report link resolved to a local file. Linux/macOS CI for the visual-review base is green; the overview is checked on its own stacked PR.

The overview checks registered JSON hashes but does not hash all EPUBs, covers, or screenshots. It is a snapshot of recorded state; the relevant workflow performs full checks before editing or publishing.

## Previously executed checks

- The merged metadata curation/Meta-review work passed 135 tests locally, including real EPUBCheck validation of edited EPUB 2 and EPUB 3 artifacts. Research fixtures cover ISBN checksums, exact-edition filtering, conservative reconciliation, source provenance, cache age/integrity, bounded responses, and redirect restrictions. Apply tests cover stale/tampered plans, explicit review, resource/package preservation, validator rejection, database rollback, original reset, and revision-aware publication.
- Meta review tests cover the documented structured-output request, scoped authentication, source-only prompts, cached advice, explicit refresh, stale/tampered plans and reviews, unknown/conflicting citations, malformed/refused/truncated outputs, redirect rejection, timeouts, quota/auth errors, request/response limits, adversarial source text, CLI behavior, and unchanged book approval state. These tests use local transport fixtures, not live model generation.
- Regression tests cover EPUB 2/3 structure, footnotes and images, broken URLs/fragments, missing resources, duplicate IDs, ZIP traversal, XML entities, malformed input, typography observations, and content fingerprints.
- Pipeline tests cover exact byte preservation, repeat ingestion, missing/failed/timed-out validators, revocation after failed revalidation, source/artifact tampering, policy changes, escaped HTML reports, and CLI exit codes.
- Calibre/rclone integration-boundary tests use fake local executables to exercise command arguments, idempotency, interrupted delivery reconciliation, per-destination retries, changed destinations, and verification of Calibre's stored EPUB.
- Complete-library backup tests cover every stored format, covers, supplementary files, notes, and a SQLite database with a custom column. The full restore matches the source file inventory, hashes, modification times, and permissions, and retains the custom-column value.
- A separate backup/restore check used a database created from Calibre's current upstream `metadata_sqlite.sql` schema, including its annotation FTS tables. Its database passed `quick_check` and restored identically. This does not replace opening a real restored library in Calibre on the Mac.
- Backup tests also cover archive tampering, unsafe paths, active database journals, symlinks, changing source files, upload failures, retry, and refusal to overwrite an existing library.
- The real installed rclone executable also copied a complete backup through a named local backend; restoring that transferred archive reproduced the entire fixture library. This checks the CLI transport but does not establish Google Drive authentication or access.
- The real EPUBCheck integration accepts both generated EPUB 2 and EPUB 3 books, and rejects deliberately introduced OPF schema errors that the built-in checks do not catch.
- A real CLI import produced a validated, byte-identical curated EPUB and JSON/text/HTML reports.
- Lint, formatting, package build, and repeated frozen dependency setup were exercised locally.

Run the complete suite, including real EPUBCheck:

```sh
bash scripts/setup-cloud.sh
EMEND_TEST_EPUBCHECK_JAR=/workspace/.tools/epubcheck-5.3.0/epubcheck.jar uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

For another machine, use its actual EPUBCheck JAR path. Without `EMEND_TEST_EPUBCHECK_JAR`, the official-validator integration cases are explicitly skipped. The remaining validator subprocess tests use fixtures and are not substitutes for the official integration.

## Paginated browser preview (0.5)

The full suite passes **209 tests**, with official EPUBCheck and real Chromium enabled. The 22 new pagination cases cover numbered EPUB 2/3 captures, unchanged source/artifact/report state, first/middle/final page selection, final-text reachability, retained authored typography, forced breaks, custom margins, oversized content and text-edge warnings, animation freezing, unsupported layouts, bounded page counts, CLI output, and older scrolling-review compatibility.

An original synthetic chapter produced 15 compact pages and 5 wide pages, with first/middle/final screenshots labelled at their exact offsets. The final page was visually inspected, and contact sheets were rendered at 1280 px and 390 px with no horizontal overflow. Lint, formatting, and source/wheel builds pass. The installed wheel includes the tested pagination script and renders a two-page example using real Chromium.

The test corpus establishes the controlled browser behavior, not matching page counts in Calibre, Apple Books, or XTEINK. This first paginated mode supports horizontal LTR reflowable content only. Page counts are per document; left/right blank-page parity and interactive footnote placement remain outside its scope.

## Current EPUB revalidation (0.7)

The revalidation extension adds 23 cases. The complete local suite passes **262 tests**, with five full Ace CLI cases explicitly skipped for hosted CI. It checks unchanged curated EPUB 2/3 bytes and metadata/cover history, required versus optional audits, current-artifact research and visual evidence after a failed audit, policy/engine refresh, unchanged delivery receipts, recovery of an initial import without an artifact, structural failures, tampered/missing inputs, validator mutation of temporary copies, bounded snapshots, CLI output/status, and operation locking.

Interruption coverage forcibly kills a CLI process group after its validator starts, verifies the persisted `REVIEW_REQUIRED` state and retained curated identity, then successfully retries. Injected inspection, final-report write, and final-database write failures also leave a retryable pending report. This establishes process-interruption behavior, not machine/power-loss durability.

Official EPUBCheck cases revalidate metadata/cover edits in EPUB 2 and EPUB 3. Hosted macOS/Linux CI additionally runs the official DAISY Ace CLI on clean and missing-alt EPUBs through the new revalidation command. The managed cloud browser's Ace local-file limitation still applies. See the CI result for the exact commit under review.

A standalone curated fixture passed official EPUBCheck again without changing its artifact hash, metadata, or cover history. Its report was rendered at 1280 px and 390 px with no horizontal overflow. Lint, formatting, and wheel/sdist builds pass. Revalidation reports retain `artifact_sha256` as identity even on failure; state, policy, engine, and required gates determine approval.

## Authenticated Meta smoke test — 2026-10-18

The configured `MODEL_API_KEY` was reported ready and used through the inherited HTTPS proxy with the existing production client. One request to `muse-spark-1.3` reviewed an ambiguous publisher proposal for an original synthetic EPUB and fixture edition records. No personal book text or files were uploaded, and no credential value was printed or saved.

The live response passed strict structured-output parsing, proposal coverage, and source-citation checks. It produced one recommendation and reported 1,269 prompt tokens plus 1,629 completion tokens (2,898 total). The EPUB artifact hash, quality report, and book approval state remained unchanged. A repeated review reused the registered result with the network-call function replaced by a failing guard; reading saved advice also returned the identical result.

This establishes the authenticated API path and cache behavior, not the factual reliability of advice across real editions. The automated failure-mode tests remain fixture-based. No source-code adjustment was needed for the live API response.

## Actionable accessibility findings (0.6)

The suite now contains **244 tests**. The local run passes **241**, including official EPUBCheck and real Chromium, with the three full Ace CLI cases explicitly skipped. Parser/process tests cover DAISY's nested EARL schema, report fingerprints, inherited document paths and CSS selectors, summary deduplication, unresolved and contradictory outcomes, malformed reports, bounded evidence, nonzero exits, stale output refusal, optional/required publication policy, escaped HTML, older report compatibility, and library visibility.

Hosted macOS/Linux CI installs the official `@daisy/ace-cli@1.4.6` with its Puppeteer browser. Integration cases exercise a clean original EPUB 3 with accessibility metadata, and deliberate missing-alt defects in EPUB 2 and EPUB 3. The defect cases verify the `image-alt` rule, document/selector locations, preserved bytes, blocked required gates, and removal of that finding when the fixture's alt text is restored. The clean fixture also runs through official EPUBCheck. These are original synthetic books, not a representative publisher corpus or screen-reader evaluation.

The managed cloud Chromium rejects Ace's required `file:` navigation with `ERR_BLOCKED_BY_ADMINISTRATOR`; this is not treated as a passing audit. Local diagnosis used Ace's published axe rules on the same XHTML served by an in-memory browser route. It caught a missing `doc-toc` role in the clean fixture; the corrected documents pass those rules and official EPUBCheck. Full command-line integration runs in the hosted workflow with the unmodified DAISY runner.

The quality report was rendered and visually checked at 1280 px and 390 px with no horizontal overflow. Lint, formatting, wheel/sdist builds, and the optional CLI installation script pass. Emend's report runs no scripts or network requests; the separate raw DAISY report is retained as tool evidence.

## Real Calibre CLI integration — 2026-10-20

Installed the official Linux x86_64 Calibre 9.16.0 distribution from its GitHub release without root or Homebrew. The archive SHA-256 matches the release asset digest (`03cd4fda43de23d65405a3c90b924bc4a5c84753e388589d4ca343d9ba561a1d`). `calibredb`, `ebook-meta`, `ebook-convert`, and `calibre-debug` run successfully with isolated writable configuration and Qt's offscreen platform.

Three repeatable integration cases use actual Calibre databases initialized by its CLI:

- EPUB 2/3 publishing: original and explicitly curated metadata/cover revisions coexist with distinct Emend identifiers. Calibre stores the exact approved EPUB bytes, edited publisher/date/language, and the expected cover dimensions. Receipted retries and reconciliation without a receipt avoid duplicates. An altered destination EPUB is rejected by hash verification.
- Full rich-library round-trip: Calibre creates a custom reading-status column, rich HTML comments, Unicode values, tags, series/index, and rating. Its converter produces a real TXT companion to the EPUB. Its Python API creates an author note with a resource attachment, a viewer highlight with notes, and a supplementary data file. Calibre also generates per-book metadata OPFs. The notes live in `.calnotes` in this release.
- Emend backs up this closed library, verifies every archived file, transfers the archive with the real rclone local backend, and restores it into a new folder. The restored file inventory matches all hashes, modes, sizes, and modification times before reopening. Calibre then reads every rich field, annotation, note/resource hash, and format back identically, lists the library successfully, and reports no filesystem issues through `check_library --csv`.

The fixture helper uses Calibre's own API only to set up/read test data; Emend continues to use the CLI for publishing and never writes Calibre metadata directly. No application fix was needed. The full local suite passes **265 tests**, with five Ace CLI cases explicitly skipped for hosted CI. These Linux checks use original synthetic EPUBs and a disposable real library; the target Mac, GUI presentation, a large personal library, and authenticated Google Drive remain unverified.

Reproduce on Linux x86_64:

```sh
bash scripts/setup-calibre.sh
EMEND_TEST_CALIBRE_BIN=/workspace/.tools/calibre-9.16.0 uv run pytest -q tests/test_calibre_integration.py
```

Set `EMEND_CALIBRE_DIR` to install elsewhere; set a writable `UV_CACHE_DIR` where needed. On another platform, point `EMEND_TEST_CALIBRE_BIN` at an existing Calibre binary directory. The tests isolate Calibre configuration and use disposable libraries. rclone transfer is additionally exercised when rclone is installed; hosted Linux CI installs it explicitly. The macOS job skips these three cases until given a real installation.

## Remaining integration checks

- The target Mac mini M4 was not accessible. Real Calibre CLI import and rich-library restoration are now verified on Linux as above; macOS executable detection, platform-specific behavior, and GUI opening still need checks on the Mac.
- No personal Google Drive account was connected or uploaded to. rclone upload behavior is tested at the process boundary; the user must configure and verify the actual remote locally.
- Ace is optional. Structured parser/process fixtures and hosted official-CLI checks are described above; broader real-book and manual screen-reader validation remains outstanding. Reports show the tool as unavailable when it is absent.
- The foundation and initial metadata-curation commits passed hosted macOS and Linux CI. Meta review checks are tracked on PR #2.
- On 2026-10-17, live Open Library parsing resolved ISBN 9780140328721 to edition OL7353617M (*Fantastic Mr. Fox*), author Roald Dahl, publisher/date/language, and cover provenance, retaining two evidence documents. Google Books was reachable but returned HTTP 429; a successful live Google response remains unverified.
- Meta documentation was read successfully on 2026-10-17: [Chat Completions](https://dev.meta.ai/docs/protocols/chat-completions), [structured output](https://dev.meta.ai/docs/structured-output), and [authentication](https://dev.meta.ai/docs/authentication). The configured client uses `https://api.meta.ai/v1/chat/completions` and `MODEL_API_KEY`. The key was initially unavailable; the authenticated smoke test above subsequently passed on 2026-10-18.
- Device-specific reader emulation, general structural repair, and XTEINK transfer remain deferred. No device compatibility or publication-quality certification is claimed.

## Report presentation

The user supplied `Rishit Design Skill.skill` during implementation. Its relevant guidance informed the report: role-specific typography, an 8-pixel spacing rhythm, warm neutral OKLCH colors, hairline separators, bounded reading width, and explicit line heights. DM Sans and IBM Plex Mono are embedded with their licenses; the display serif uses a local font stack. Reports make no network requests and run no scripts. Publisher EPUB formatting remains unchanged. The report was rendered in Chromium at desktop and mobile widths and checked for horizontal overflow.

## Safe EPUB inbox (0.8)

The suite contains **304 cases**. Local validation passes **299**, including real EPUBCheck, Chromium, Calibre, and a foreground watcher subprocess. Five full Ace CLI cases remain explicitly skipped locally because of the managed browser restriction and run in hosted CI.

The 34 new cases cover stable observations, changing/replaced/disappearing files, `.part` rename completion, bounded copies and entry counts, ignored symlinks/FIFOs, lock contention/retry, independent per-file errors, restart/rename deduplication, and CLI JSON/status behavior. Tests exercise both injected and actual mid-read changes, atomic duplicate checks across concurrent curation, preserved metadata/history, retained edited-revision recognition, orphaned artifacts, failed/pending imports, and unchanged manual-ingest behavior. One subprocess runs the real foreground watcher, imports a fixture, stops cleanly on SIGINT, then recognizes a renamed duplicate without changing its report. The official EPUBCheck case validates a new inbox import.

Stability is inferred from filesystem observations; it does not establish that another program has closed the file. Prefer a temporary extension followed by rename after completion. Watching remains a synchronous foreground command, not a durable scheduler or automatic publication service. No personal EPUBs were used in these regression cases.

## Explicit container repair (0.9)

The local full suite passes **335 tests**, including real EPUBCheck, Chromium, and Calibre, with five full Ace CLI cases explicitly skipped for hosted CI. The 36 new repair cases cover EPUB 2/3 order/compression changes, all resource bytes including unmanifested data/directory records, archive/entry comments and metadata, exact approval, unchanged proposal state, unsupported damage, stale/tampered evidence, validator rejection/unavailability, unexpected resource/directory changes, mutated candidates/inputs, refusal to reread unsafe candidates, report-write failure, revision retention, idempotency, CLI output, metadata-history propagation, and subsequent revalidation. Rollback cases inject database failure and a copy failure immediately after replacing a retained current artifact.

A standalone synthetic EPUB with compressed mimetype and ZIP extra fields passed official EPUBCheck after repair; every resource hash matched the original. Its review page was rendered at 1280 px and 390 px and inspected at the narrow width. No personal books were used. These checks do not establish device rendering, typography quality, process-kill recovery during activation, or power-loss durability.

## Reviewed navigation path repair (0.10)

The full local suite passes **366 tests**, including real EPUBCheck, Chromium, and Calibre; five full Ace CLI checks remain explicitly skipped locally for hosted CI. The 31 new cases cover EPUB 2 NCX / EPUB 3 TOC corrections, exact target/fragment evidence, unchanged reading text/resources, multiple links and explicit approval, ambiguous/missing targets, unsupported URLs/body links, real 201-link rejection, nested and percent-encoded paths, public doctype/comments/processing instructions, unsupported XML canonicalization, and CLI JSON. Injected unapproved label, link, stylesheet, descriptive metadata, prolog, archive-entry, and invalid-timestamp changes are rejected by independent preservation checks.

Official EPUBCheck accepts repaired EPUB 2 and EPUB 3 fixtures. A separate synthetic EPUB passed after repair with unchanged text fingerprints; its review page was rendered at 1280 px and 390 px, checked for horizontal overflow, and inspected visually at the narrow width. No personal EPUBs or links were used as fixtures. Lint, formatting, and wheel/sdist builds pass. The shared application-error rollback tests remain enabled; forced interruption during activation, power-loss durability, and real-reader navigation/footnote behavior remain outside this evidence.

## Typography comparison trials (0.11)

The local full suite passes **390 tests**, including real EPUBCheck, Calibre and Chromium, with five full Ace CLI checks skipped for hosted CI. The 24 new cases cover explicit/bounded numeric settings, same-snapshot comparison, unchanged EPUB/report/state, CSS/screenshot/JSON integrity, missing/incomplete captures, browser failure, changed snapshots/browser versions, unsupported input, stale-trial CLI behavior, and real EPUB 2/3 scrolling/paginated comparisons. Browser tests verify changed screenshots and increased document height/page counts for larger spacing without modifying EPUBs.

A standalone fixture passed official EPUBCheck and produced a complete trial. The report was rendered at 1280 px and 390 px without horizontal overflow and inspected at the narrow width. XHTML styles are inserted directly by trusted automation because Playwright's stylesheet load helper did not finish in this context; book scripts stay disabled. This evidence establishes browser-only comparisons, not automatic typography recommendations or device fidelity.

## Approved typography application (0.12)

The full local suite passes **410 tests**, with five full Ace CLI checks skipped locally. The 20 new cases exercise explicit approval and document/all-spine scope, EPUB 2/3 resource/XML/text preservation, unchanged font/image/existing CSS bytes, idempotency, history through metadata edits/revalidation, validator failure, stale/incomplete evidence, and rollback after database/copy failure. Independent checks reject unapproved text, CSS, links, manifest metadata, archive entries, and stylesheet changes. Official EPUBCheck accepts applied EPUB 2 and EPUB 3 styles.

A real Chromium check applies the reviewed CSS to the EPUB and captures it through the normal renderer. Its document height and first screenshot hash exactly match the trial's adjusted view. This establishes the controlled fixture's cascade/geometry; it does not certify every publisher layout, unsampled content, device rendering, or interruption durability during activation.

## Offline reading evidence (0.13)

The full local suite passes **424 tests**, with five local Ace CLI skips. The 14 new cases cover font hashes/source declarations, nested and inline CSS, local/declared-obfuscated fonts, marked versus unmarked notes, missing semantics/backlinks/reference IDs, bounded incomplete evidence, language/direction hints, escaped markup, tampered records, and CLI saved/incomplete behavior. CSS comments are not treated as declarations. No book/report/state mutation occurs.

A synthetic corpus with an embedded licensed font and reciprocal semantic note passed official EPUBCheck and produced complete reading evidence. Its HTML was rendered at desktop/phone widths without overflow and inspected at the narrow width. This is a source inventory and relationship audit, not glyph coverage, a resolved style cascade, screen-reader evaluation, or device conformance.

## Local review workspace (0.14)

The complete local suite passes **441 tests**, with five local Ace skips. The 17 new cases cover session authentication, Host/Origin/CSRF rejection, bounded forms, stale identities, concurrent actions, escaped metadata, isolated/hash-checked evidence, explicit metadata/typography application, publication identity checks under the operation lock, and real-browser EPUB import, reading evidence, and local cover comparison/application. The same numeric settings and gates remain authoritative in CLI and UI.

A disposable real-browser run used official EPUBCheck and Calibre 9.17.0: the synthetic embedded-font/note EPUB was imported, reviewed, published to a fresh library, backed up through the workspace, restored to a new directory, and opened/listed by the real Calibre CLI. Desktop/phone views had no horizontal overflow and were inspected visually. No personal library or Drive account was used. The UI uses same-origin referrer policy so browser form origins remain verifiable, while isolated evidence uses no-referrer and sandboxed content. Rejected bodies receive a bounded orderly close to avoid resetting error responses.

Typography application checks compare computed body/paragraph styles and document geometry across captures. Screenshot hashes are retained for saved-file integrity; independent browser rasterization is not required to produce identical PNG bytes.

## Mounted-folder reader export (0.15)

The local full suite passes **463 tests**, with five local Ace CLI skips. The 22 new cases exercise native atomic no-overwrite delivery and idempotency, existing different files/symlinks, unsafe filenames, managed/library/missing destinations, competing writers, changed directories, unavailable rename/fsync, receipt failure/retry reconciliation, policy/revision guards, Unicode filenames, directory-sync limitations, and CLI/workspace use of an explicit destination. Actual exported bytes match the approved EPUB; no source or unrelated destination data is altered.

Linux uses `renameat2(RENAME_NOREPLACE)` and macOS uses `renameatx_np(RENAME_EXCL)`; Darwin's flag/signature were checked against Apple's published XNU header. Hosted macOS/Linux tests exercise their native implementation. A real reader filesystem, mount/unplug behavior, safe eject, filename support, and firmware rendering remain hardware validation, not inferred from a temporary-folder test.

## Missing-cover creation (0.16)

The combined local suite passes **478 tests**, with five full Ace cases explicitly
skipped because of the managed browser restriction. Fifteen creation cases cover
EPUB 2/3, exact original resource bytes and relative spine order, registered
candidate identity/provenance, small-source exceptions, existing declarations and
cover hints, fixed-layout refusal, injected resource changes, validator/database/
activation failures, rollback, and the workspace action. Both EPUB versions pass
official EPUBCheck 5.3.0 after creation. The final manifest-driven cover-hint scan
also passes the focused creation suite. Real Calibre, EPUBCheck and Chromium
integrations were enabled for the full run. Artwork/edition approval and actual
reader cover-page rendering remain human checks.

## Explicit revision recovery (0.17)

The combined full suite passes **496 tests**, with five local Ace skips and real
EPUBCheck, Chromium and Calibre enabled. Recovery initially added 18 cases,
including real process-kill recovery, current-byte quarantine, original/approved
revision selection, selected edit-history restoration, interrupted initial import,
stale UI refusal, tampered originals/revisions, required-validator failure, and
interruptions before/during/after replacement. Five additional focused cases
cover CLI failure exit status, symlink-original refusal, and database failures at
all three transitions. The final focused recovery/pipeline run passes 41 cases
with only its official-checker case skipped when that opt-in is absent. The
separate checker-enabled recovery run passed. Reports also expose recovery history.
Filesystem exhaustion, sudden power loss, and the target Mac remain external
validation; injected faults and process termination do not certify those cases.

## Complete release verification (0.18)

The repeatable `scripts/verify-local.sh` run passes **511 tests**, with five full
Ace cases explicitly skipped in this managed cloud browser. Lint, formatting,
source distribution and wheel builds pass. Real Calibre 9.17.0, EPUBCheck 5.3.0,
Chromium and rclone were enabled. The official Linux Calibre installer also ran
successfully using its checksum/size-verified cached distribution.

Two complete workflow cases run EPUB 2 and EPUB 3 through packaging repair,
fixture-backed metadata research and actual edits, missing-cover creation,
actual browser typography comparison/application, reading/visual evidence,
recovery from injected corrupt activation, verified mounted-folder export/retry,
real Calibre import, rich notes/resources/annotations/custom columns and extra
files, a complete backup transferred by real rclone to a disposable local remote,
and restoration reopened through Calibre's API. No personal library, book, Drive
account or live AI/provider request is involved. Existing integration cases also
verify multiple ebook formats and rich metadata retention.

A real-browser creation/recovery flow passes at 1280 px and 390 px with no
horizontal overflow; screenshots were inspected locally. Seven packaging cases
cover local-header extra fields (including absent central extras), prepended bytes,
EPUB 2/3 acceptance after repair, and a valid central-directory-only extra field.
Official EPUBCheck accepts that last case; Emend does not falsely flag it as a
local-header defect. Engine 3 requires revalidation of earlier approvals.

CI now installs actual Calibre on both Linux and macOS, runs full Ace and browser
integrations, and builds distributions. Its current checks establish the hosted
platform result; the final PR remains draft until those checks finish. The Mac
installer uses the official checksum/size-verified universal DMG in an isolated
tools directory. Personal Mac/Drive/X4 Pro and publication-quality acceptance
remain the checks in [the runbook](mac-validation.md).

Final review extended missing-cover refusal to legacy fixed-layout metadata and
per-spine fixed-layout declarations, sharing the existing pagination classifier.
The focused checker/browser-enabled cover and pagination suites pass **40 tests**,
including the three additional refusal cases and legacy EPUB 2 coverage.

## Guided book review and CrossPoint cover observations (0.19)

The initial full local integration verification passes **543 tests**, with five full Ace
cases explicitly skipped for the managed browser restriction. Actual EPUBCheck,
Chromium, Calibre 9.17 and rclone were enabled. Lint, formatting, source distribution
and wheel builds pass. After final layout/copy refinements, the focused UI,
workflow and guidance suites pass **49 tests**, with only their two complete
real-tool workflow cases skipped when the external-tool opt-ins are absent; those
cases run in the full integration suite above.

The 27 guidance cases include exact blocked-book messages/locations, named repair
availability, empty/unsupported repair scans, stale report detection, missing
checker setup and refresh, contextual publication/download errors, Calibre lock
feedback, named spacing preview/application, disabled metadata edits on failed
books, and bounded/confined validator evidence. Browser downloads recheck core
approval/engine/policy and source identities, use a bounded verified snapshot,
stream the exact approved bytes, require the local session, and leave approval
and publication state unchanged. Policy/engine/source/snapshot faults refuse the
download; this is not a device-delivery receipt.

Real Chromium cases exercise both a repairable book (find fix → apply named fix →
approved download) and unsupported validator failures (exact problems/manual next
steps, blocked sending, preserved book context). Current screens were inspected
at 1280 and 390 px, including the available-fix and ready-to-send states; no
horizontal overflow was detected. Existing security and mutation-approval cases
remain unchanged apart from following the new labels and optional disclosures.

Cover observations distinguish an embedded declared JPEG/PNG from a separate
Calibre library thumbnail, report image dimensions and format/extension mismatch,
and avoid offering missing-cover creation for known incompatible declarations or
layouts. Original synthetic fixtures cover these observations without changing
approval. [CrossPoint cover guidance](crosspoint-covers.md) is based on pinned
2.0.0rc source: that firmware creates its own BMP caches. No X4 Pro hardware,
private book or device cache was accessed or modified by these tests.

Completion notices are now scoped to their book and consumed once. Generic and
unauthenticated error pages never display a private backup/transfer notice; two
regression cases cover that boundary and cross-book message reuse.
