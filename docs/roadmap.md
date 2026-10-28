# Release scope and future work

Emend's current milestone is a local, preservation-first EPUB review and curation
workflow, with explicit edits, inspectable evidence, gated delivery and complete
Calibre-library backups. Version 0.18 completes the planned implementation batch.
The remaining release acceptance is PR review/merge and the personal Mac, Drive
and XTEINK checks in [the validation runbook](mac-validation.md).

Version 0.19 adds a guided **Check & fix → Preview → Send** workspace, actionable
validator messages and named supported fixes, gated browser downloads, optional
spacing presets, contextual failures and embedded-cover observations for the
CrossPoint handoff. These changes expose the existing preservation rules; they
do not introduce unrestricted automatic repair or device-cache generation.

Version 0.20 adds reviewed repair of empty EPUB 2 root language attributes and
block-only paragraph wrappers in EPUB 2/3. Independent tree/resource comparison and
required validators still gate activation; arbitrary markup repair remains outside
this scope.

Version 0.21 redesigns the workspace around one automatic run per import: keep and
check the original, apply the supported repairs through their existing gates, and
render reader pages. Each book then shows **Import → Check → Repair → Preview →
Refine → Send**, opens on the step that needs attention, and explains failures with
the validator's messages, what was tried and the next steps. Spacing refinement
compares baseline and adjusted pages before an explicit apply and can return to the
baseline. No new repair kinds, editing operations or input formats were added.

Version 0.22 lets supported repairs combine: one plan chains packaging, contents-link
and markup repairs, each checked independently, with only the final copy validated and
activated. No new kinds of repair were added.

Version 0.23 adds **Text size on your reader**. CrossPoint applies a book's relative
`font-size` rules on top of the device size, ignores `!important` and embedded fonts,
and does not read line height. Emend therefore resets only the publisher's own
body-text `font-size` values, after a compared preview and explicit approval, with
every other byte verified unchanged. Embedding typefaces for other readers is a
separate follow-up.

## Implemented workflow

- **Preserve and inspect.** Originals retain their exact bytes and identity hashes.
  Bounded ZIP/XML/resource inspection records structure, metadata, spine, text and
  resource fingerprints. EPUBCheck and optional DAISY Ace produce retained quality
  evidence; required missing, incomplete or failed checks block publication.
- **Research and choose.** Exact-ISBN and title/author discovery retain edition
  evidence from Open Library and Google Books. Explicit selection creates scoped
  proposals. Optional official Meta Model API advice cites recorded evidence and
  never applies edits or promotes approval. Provider failures remain visible.
- **Apply scoped edits.** Metadata changes, declared JPEG/PNG cover replacement,
  missing-cover creation for supported reflowable EPUB 2/3, container packaging
  repair, case-only TOC path repair, scoped HTML markup repair, and reviewed numeric typography application
  have independent preservation checks, validation before activation, retained
  revisions and rollback. Missing-cover creation explicitly adds a first cover
  page while preserving old resource bytes and their relative reading order.
- **Review reading quality.** Offline scrolling and horizontal LTR paginated browser
  samples, current/trial typography comparisons, font/source/obfuscation inventory,
  marked-note targets/backlinks and source direction hints are advisory evidence.
  Book fonts and alignment stay authored; broad CSS or text rewriting is unsupported.
- **Use one local workspace.** Authenticated loopback-only import/review/actions,
  saved/stale/incomplete evidence, local library overview, stable-file inbox
  ingestion, revalidation, and explicit original/approved-revision recovery are
  available. Interrupted recovery/revalidation leaves approval revoked.
- **Deliver and restore.** Gated Calibre import, optional rclone EPUB delivery,
  no-overwrite byte-verified mounted-folder export, full rich Calibre snapshots,
  cloud upload, integrity verification and restore into a new local directory.
  Back up the separate Emend workspace independently to retain its originals and
  evidence. A Calibre backup is the whole Calibre library, not just exported EPUBs.

## Acceptance still requiring your setup

Run [automated Mac verification and private real-book review](mac-validation.md).
Confirm actual Calibre rendering, Google Drive upload/download/restore, retained
rich library objects, and X4 Pro reading behavior. Broaden the private corpus and
perform accessibility/screen-reader review. [Executed evidence](validation.md)
distinguishes actual tools, fixtures, skipped cases and live provider checks.
[Deferred validation](deferred-validation.md) records the limits.

## Optional extensions after this milestone

A broader structural/footnote/layout repair catalog, SVG/ambiguous cover repair,
format conversion, font/glyph validation, fixed/RTL/vertical pagination, calibrated
device profiles, multimodal advice, and scheduling/background services need their
own requirements and corpus evidence. CrossPoint/network delivery depends on the
actual installed firmware and a verified protocol. These extensions are outside
the completed release scope; no passing placeholders or device certification are
substituted for them.
