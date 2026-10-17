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

`ai-review` uses [Meta Model API](https://dev.meta.ai/products/meta-model-api), with Muse Spark 1.3 Standard mode by default. It reviews only collected metadata evidence, cites existing proposal/record IDs, saves bounded structured advice with request provenance, and never promotes proposals to safe or applies them. Cached reviews avoid repeated calls; explicit refresh and retries remain user-controlled. The documented API contract and failure handling are tested; authenticated generation awaits `MODEL_API_KEY`.

## Implemented, awaiting validation: edition discovery and cover review

Version 0.3 adds title/author discovery for books without ISBNs, field-by-field edition comparisons, explicit selection into review-only research plans, and match-mode context for Meta advice. Open Library search work records are expanded into edition records before selection.

Cover review downloads bounded recorded candidates or accepts a local image, produces a local side-by-side page with edition provenance and dimension/perceptual-hash hints, and supports explicit replacement of one already-declared JPEG/PNG cover in the same format. Unrelated resources and reading text remain preservation invariants; required validators and rollback remain part of the application path. Automatic artwork certification, missing-cover creation, format conversion, and layout repair are deferred.

Runtime testing, new regression coverage, live search/download checks, and report-layout iterations were deferred at the user's request. See [deferred validation](deferred-validation.md) before treating these additions as verified.

## Then: visual QA and accessibility depth

Build an epub.js/Playwright renderer with representative page sampling for large and small screens, geometric checks, and reproducible screenshots. Validate the Ace integration against real books and add optional multimodal review. Calibrate browser profiles against actual Apple Books and XTEINK screenshots before making device claims.

## Later: device delivery and automation

Verify the X4 Pro's installed firmware and supported transfer protocol before implementing CrossPoint delivery. Add a stable-file inbox watcher, stage caches, scheduling, per-device profiles, and a focused review UI after the inspection/repair corpus is established.

Only the optional AI review command requires an AI credential. Firmware installation, background watching, rendering, and typography transformation are not required for the current workflow. Advanced checks are not represented as passing placeholders.
