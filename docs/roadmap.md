# Scope and next milestones

The first usable version follows the supplied Emend design's reliable-foundation milestone. The project, Python package, and CLI are all named `emend`.

## Delivered foundation

- Local Python CLI, strict YAML configuration, SQLite state history, and retryable publication receipts.
- Safe EPUB container inspection, OPF/spine/navigation checks, text/resource fingerprints, and exact original preservation.
- EPUBCheck integration with stored machine-readable evidence; optional Ace integration.
- Human-readable and machine-readable reports, with skipped/unavailable checks shown explicitly.
- Gated Calibre and optional rclone EPUB publishing.
- Complete Calibre library snapshots, per-file verification, Google Drive upload, and restoration into a new directory.

## Next: metadata and controlled repair

Retrieve edition records by exact ISBN, reconcile multiple sources, cache responses, and retain evidence for every proposal. Add typed transformation operations with preconditions, before/after resource and text invariants, and rollback. Keep prose rewriting out of automatic operations. Add cover candidate provenance and edition matching before replacement.

## Then: visual QA and accessibility depth

Build an epub.js/Playwright renderer with representative page sampling for large and small screens, geometric checks, and reproducible screenshots. Validate the Ace integration against real books and add optional multimodal review. Calibrate browser profiles against actual Apple Books and XTEINK screenshots before making device claims.

## Later: device delivery and automation

Verify the X4 Pro's installed firmware and supported transfer protocol before implementing CrossPoint delivery. Add a stable-file inbox watcher, stage caches, scheduling, per-device profiles, and a focused review UI after the inspection/repair corpus is established.

No AI provider, API credential, firmware installation, background watcher, renderer, automatic metadata research, or typography transformation is required by this first version. Advanced checks are not represented as passing placeholders.
