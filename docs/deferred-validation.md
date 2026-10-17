# Deferred validation for edition and cover review

The user asked to continue feature development and defer testing/iterations. No local test suite, live discovery/download checks, EPUBCheck application runs, or browser screenshots were run for this feature. Static lint and formatting were checked. Existing CI remains configured normally; a pass on older regression coverage cannot establish the new behavior below.

## Edition discovery and selected research

- Validate real Open Library search/edition responses, missing authors, translated editions, duplicate records, absent ISBNs, bounded results, and partial provider failures. Confirm Google Books quota behavior without making failed sources count as corroboration.
- Check no-ISBN detection, title/author overrides, multiple authors, query escaping, offline cache expiry, refresh, and unknown/empty results.
- Confirm selected records create review-only proposals, preserve identifiers, retain discovery evidence, and inform Meta of the selected-record match mode. Existing exact-ISBN behavior should remain intact.
- Reject modified discoveries, stale artifact hashes, changed policies, and record IDs from another book/discovery. Review CLI output and exit codes for mixed-success provider results.

## Cover preparation and presentation

- Exercise EPUB 2 cover metadata, EPUB 3 cover-image declarations, agreement/conflict between both, undeclared/absent/SVG covers, manifest inconsistencies, and images reused elsewhere.
- Review authentic JPEG/PNG inputs, damaged files, wrong declared MIME types, EXIF rotation, unusual colour modes, PNG transparency/palettes, animation, tiny images, decompression limits, and oversized downloads.
- Verify redirect and URL restrictions with actual Open Library cover delivery; do not broaden destinations based on untrusted image metadata.
- Check candidate provenance, tampered assets/reviews, duplicate downloads, empty/failed candidates, and saved-review reads without network traffic.
- Render the comparison page at narrow and wide widths. Confirm natural aspect ratios, readable source details, no horizontal overflow, accessible image labels, and correctly escaped untrusted text. Perceptual distance must stay an advisory hint, never an artwork-match claim.

## Cover application and history

- Use official EPUBCheck on EPUB 2/3 replacements. Ensure the only altered resource is the declared image, plus the EPUB 3 modification timestamp; reading text, spine, metadata, CSS, XHTML, and every unrelated resource hash must remain unchanged.
- Reject incompatible formats, stale reviews, mutated candidate bytes, unsupported orientation/modes, and unapproved size/aspect-ratio regressions. Confirm explicit exceptions are recorded.
- Exercise required-validator failure/unavailability, input mutation during validation, write failures, database failures, repeat application, and recovery after interruption. Verify the previous approved artifact survives rejected candidates.
- Verify cover history survives later metadata edits, reset-original restores the original cover, and edited revisions publish once per destination without replacing annotated Calibre entries.

## Regression and target-device checks

Run the existing full suite and add meaningful coverage for the above behaviors in the later validation pass. Recheck package installation/CLI behavior on macOS. Inspect a replaced cover in Calibre and on the intended readers before making rendering-quality claims. Meta live generation still requires a configured key; the user deferred that setup separately.
