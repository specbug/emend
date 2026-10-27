# Scope and next milestones

The first usable version follows the supplied Emend design's reliable-foundation milestone. The project, Python package, and CLI are all named `emend`.

## Delivered foundation

- Local Python CLI, strict YAML configuration, SQLite state history, and retryable publication receipts.
- Safe EPUB container inspection, OPF/spine/navigation checks, text/resource fingerprints, and exact original preservation.
- EPUBCheck integration with stored machine-readable evidence; optional Ace integration.
- Human-readable and machine-readable reports, with skipped/unavailable checks shown explicitly.
- Gated Calibre and optional rclone EPUB publishing.
- Complete Calibre library snapshots, per-file verification, Google Drive upload, and restoration into a new directory.

## Delivered: evidence-backed metadata curation

Exact-ISBN Open Library/Google Books research, cached responses, retained evidence, conservative proposals, explicit title/publisher/date/language edits, independent package/resource preservation checks, validation before activation, revision snapshots, and rollback on failed application. Research candidates retain edition provenance; the new separate cover-review workflow handles comparison and explicit replacement. Open Library edition/author parsing has passed a live exact-ISBN check. Google Books currently returns HTTP 429 in the cloud environment; its successful-response parsing remains fixture-tested.

## Implemented: optional Meta-assisted review

`ai-review` uses [Meta Model API](https://dev.meta.ai/products/meta-model-api), with Muse Spark 1.3 Standard mode by default. It reviews only collected metadata evidence, cites existing proposal/record IDs, saves bounded structured advice with request provenance, and never promotes proposals to safe or applies them. Cached reviews avoid repeated calls; explicit refresh and retries remain user-controlled. The documented API contract and failure handling are tested. Authenticated generation passed a synthetic-evidence smoke test on 2026-10-18, including structured citation validation and cache reuse; broader real-edition evaluation remains future work.

## Delivered: edition discovery and cover review

Version 0.3 adds title/author discovery for books without ISBNs, field-by-field edition comparisons, explicit selection into review-only research plans, and match-mode context for Meta advice. Open Library search work records are expanded into edition records before selection.

Cover review downloads bounded recorded candidates or accepts a local image, produces a local side-by-side page with edition provenance and dimension/perceptual-hash hints, and supports explicit replacement of one already-declared JPEG/PNG cover in the same format. Unrelated resources and reading text remain preservation invariants; required validators and rollback remain part of the application path. Automatic artwork certification, missing-cover creation, format conversion, and layout repair are deferred.

The complete 157-test suite, including official EPUBCheck integration, passes locally. Live Open Library search and cover download, plus narrow/wide Chromium comparison layouts, were exercised. See [validation evidence](validation.md) and [remaining checks](deferred-validation.md).

## Implemented: offline visual document review

Version 0.4 adds optional Playwright/Chromium sampling of representative spine/navigation documents, compact/wide viewports, bounded vertical screenshots, geometry observations, and self-contained reports tied to an artifact hash. Scripts and network access are disabled, publisher resources remain unchanged, and no approval state is promoted. The default remains scrolling browser documents; version 0.5 adds opt-in page previews below.

## Implemented: local library overview

`library` builds a local HTML/JSON index of recorded book states and the latest review evidence, with bounded pages for larger libraries. It shows stale/incomplete/unavailable records, applied metadata/cover history, pending proposals, and inspection commands. It reads existing evidence without provider calls, browser installation, editing, or publishing.

## Implemented: paginated browser preview

Version 0.5 adds `visual-review --layout paginated` for horizontal LTR reflowable books. Chromium columns provide numbered first/middle/last-page samples with recorded reader margins. Preview-only geometry and break adaptation leave EPUB bytes and publisher text/font styling intact. Unsupported fixed/RTL/vertical layouts are explicit; oversized content, text-edge spillover, and positioned elements produce observations. Existing scrolling reviews remain supported.

## Implemented: explicit container repair

Version 0.9 adds fingerprinted repair proposals with before/after packaging details, a local HTML review, and explicit action approval. Its first action fixes the order/compression of an existing valid `mimetype` ZIP entry and clears that entry's extra fields. All resource payloads, reading content, metadata, and spine order remain unchanged; required validators run before activation. Failed candidates retain the previous book and report, and successful repairs retain revision/history records. Other structural defects and extra-field-only problems remain unsupported.

Version 0.10 adds reviewed case-only table-of-contents path repairs for EPUB 2 NCX and EPUB 3 navigation. It requires a unique existing XHTML spine target and an existing fragment, lists every proposed link change, and independently verifies the changed XML plus unchanged resources/text. Other navigation/body/footnote repairs remain unsupported.

Further work includes a broader structural-repair catalog and explicit typography proposals with visual comparisons. Those changes need their own preservation rules and corpus evidence; the container repair does not establish publication-quality layout.

## Implemented: typography comparisons

Version 0.11 adds explicit line-height/paragraph-spacing trials with current/trial screenshots from one verified EPUB snapshot. Numeric settings, CSS and screenshot evidence are retained; incomplete/stale/tampered results remain distinguishable. Trials preserve book bytes, fonts, alignment, and approval state. Applying reviewed styles to EPUBs and a consolidated review interface are the next implementation steps.

## Next: reader calibration and accessibility depth

Version 0.6 adds structured Ace rule findings to quality reports, raw-evidence fingerprints, conservative handling of incomplete/malformed results, and optional-audit warnings in the library overview. Hosted integration tests exercise DAISY Ace 1.4.6 on original EPUB 2/3 fixtures. Automated audits remain separate from manual conformance review.

Version 0.7 adds `revalidate` to refresh quality evidence for the current EPUB while preserving curated metadata, cover bytes, and history. Approval is revoked before checks start; failed or interrupted attempts retain the current bytes for review and retry, with publication blocked. An unchanged successful revalidation keeps the same revision identity and publication receipts.

Calibrate browser pagination against real reader screenshots, extend supported writing/layout modes, and verify footnote behavior. Broaden accessibility testing to a real-book corpus and screen readers, extend font/footnote/layout coverage, and add optional multimodal advice. Actual Apple Books and XTEINK checks remain necessary before making device claims.

## Later: device delivery and automation

Version 0.8 adds safe inbox ingestion: one-shot batches or foreground polling, stable-file observation and bounded snapshots, and atomic recognition of known originals and retained curated revisions. It preserves source files, retries busy operations, and keeps research/editing/publication explicit. It does not install a background service.

Verify the X4 Pro's installed firmware and supported transfer protocol before implementing CrossPoint delivery. Stage caches, scheduling, per-device profiles, and a focused review UI remain later work after the inspection/repair corpus is established.

Only the optional AI review command requires an AI credential. Firmware installation, background services, rendering, and typography transformation are not required for the current workflow. Advanced checks are not represented as passing placeholders.
