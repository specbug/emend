# Remaining integration and corpus validation

The supported workflow has preservation/failure coverage and actual tool integration
checks. [Validation evidence](validation.md) records the executed runs and explicit
skips; [the Mac runbook](mac-validation.md) makes the local checks repeatable.

Further validation needs real library/device access or a broader corpus:

- Broaden the automated Ace corpus beyond original EPUB 2/3 fixtures, and perform screen-reader, reading-order, alternative-text quality, footnote, contrast, and resizing reviews. An automated audit alone cannot establish accessibility conformance.
- Calibrate scrolling and paginated Chromium previews against actual readers, obfuscated fonts, fixed layouts, vertical writing, and interactive footnotes. Paginated mode currently supports only horizontal LTR reflowable content. The generic browser sizes are not device profiles.
- Confirm on the X4 Pro that a text-size reset makes body text follow **Reader Font Size**, that the book's
  line height has no effect, and that embedded fonts are ignored. The CrossPoint behaviour in
  [CrossPoint typography](crosspoint-typography.md) comes from reading the pinned 2.0.0-rc source.
- Confirm on the X4 Pro that WebP covers and images are absent (or shown as alt text), and on the iPad
  that Apple Books shows them and presents Pandoc footnotes as expected.
- Open replaced covers in Calibre on the Mac and on the intended reader. Browser comparison does not certify ebook-reader rendering.
- Exercise more real-world cover declarations, image reuse, unusual colour profiles, palettes/transparency, translated editions, and multi-author collections. Missing/ambiguous/SVG cover declarations remain unsupported for replacement.
- Verify a successful live Google Books response when quota permits. HTTP 429 is reported as a provider failure, not corroboration.
- Linux Calibre 9.17.0 and local Apple silicon macOS Calibre 9.16 import, rich-library backup/restore, CLI reopening, and real rclone local transfer are verified. Personal production-library acceptance, Calibre GUI rendering, and authenticated Google Drive delivery still need checks; disposable fixtures do not establish account or reader-device behavior.
- Evaluate Meta advice on a broader set of real editions and ambiguous/conflicting records. An authenticated synthetic-evidence smoke test passed on 2026-10-18; contract/failure coverage still uses deterministic fixtures.
- Revalidation now has process-kill recovery and injected report/database-write failure coverage. Explicit revision recovery also has process-kill and injected activation/database failure coverage. Real filesystem exhaustion and machine/power loss still require external validation.

These remaining checks do not make the completed preservation tests or official EPUBCheck runs substitutes for device review.

The implementation batch is complete for the supported workflow. Use [the Mac validation runbook](mac-validation.md) for release acceptance. Optional broader repairs/rendering/device protocols are listed separately in [the roadmap](roadmap.md).
