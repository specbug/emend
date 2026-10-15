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

Exact-ISBN Open Library/Google Books research, cached responses, retained evidence, conservative proposals, explicit title/publisher/date/language edits, independent package/resource preservation checks, validation before activation, revision snapshots, and rollback on failed application. Cover candidates retain edition provenance but are not downloaded or replaced. Public provider access still needs live verification in the restricted cloud environment; local fixture coverage does not establish live API compatibility.

## Next: AI-assisted review and cover research

The requested integration is [Meta Model API](https://dev.meta.ai/products/meta-model-api). Its documentation is currently blocked by cloud network policy; verify the current API contract and credential destination once access is available. No substitute provider or credential is assumed. Recommendations should cite collected evidence and remain subject to explicit review, typed edits, and deterministic preservation gates. Add broader edition matching for books without ISBNs, cover quality/edition checks, and reviewed cover replacement. Keep prose rewriting out of automatic operations.

## Then: visual QA and accessibility depth

Build an epub.js/Playwright renderer with representative page sampling for large and small screens, geometric checks, and reproducible screenshots. Validate the Ace integration against real books and add optional multimodal review. Calibrate browser profiles against actual Apple Books and XTEINK screenshots before making device claims.

## Later: device delivery and automation

Verify the X4 Pro's installed firmware and supported transfer protocol before implementing CrossPoint delivery. Add a stable-file inbox watcher, stage caches, scheduling, per-device profiles, and a focused review UI after the inspection/repair corpus is established.

No AI credential, firmware installation, background watcher, renderer, or typography transformation is required for the current workflow. Advanced checks are not represented as passing placeholders.
