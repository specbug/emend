# Validation evidence

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

## Remaining integration checks

- The target Mac mini M4 was not accessible. Calibre's macOS executable detection and command arguments are implemented; the argument/JSON formats were cross-checked against upstream Calibre source. A real Calibre import and opening a restored library still need verification on the Mac.
- No personal Google Drive account was connected or uploaded to. rclone upload behavior is tested at the process boundary; the user must configure and verify the actual remote locally.
- Ace is optional and was tested with structured subprocess fixtures, not a real browser accessibility audit. Reports show it as unavailable when it is absent.
- The foundation and initial metadata-curation commits passed hosted macOS and Linux CI. Meta review checks are tracked on PR #2.
- On 2026-10-17, live Open Library parsing resolved ISBN 9780140328721 to edition OL7353617M (*Fantastic Mr. Fox*), author Roald Dahl, publisher/date/language, and cover provenance, retaining two evidence documents. Google Books was reachable but returned HTTP 429; a successful live Google response remains unverified.
- Meta documentation was read successfully on 2026-10-17: [Chat Completions](https://dev.meta.ai/docs/protocols/chat-completions), [structured output](https://dev.meta.ai/docs/structured-output), and [authentication](https://dev.meta.ai/docs/authentication). The configured client uses `https://api.meta.ai/v1/chat/completions` and `MODEL_API_KEY`. No key was available for authenticated generation; its secret requirement and `api.meta.ai` destination were saved for secure entry.
- Paginated reader emulation, general structural repair, and XTEINK transfer remain deferred. No device compatibility or publication-quality certification is claimed.

## Report presentation

The user supplied `Rishit Design Skill.skill` during implementation. Its relevant guidance informed the report: role-specific typography, an 8-pixel spacing rhythm, warm neutral OKLCH colors, hairline separators, bounded reading width, and explicit line heights. DM Sans and IBM Plex Mono are embedded with their licenses; the display serif uses a local font stack. Reports make no network requests and run no scripts. Publisher EPUB formatting remains unchanged. The report was rendered in Chromium at desktop and mobile widths and checked for horizontal overflow.
