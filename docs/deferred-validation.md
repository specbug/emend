# Remaining integration and corpus validation

The user subsequently authorized testing while travelling. Edition discovery and cover review now pass 22 new tests and the complete 157-test suite, including official EPUBCheck on EPUB 2/3 replacements. Live Open Library search/download and desktop/mobile report rendering were exercised. See [validation evidence](validation.md).

Further validation needs real library/device access or a broader corpus:

- Broaden the automated Ace corpus beyond original EPUB 2/3 fixtures, and perform screen-reader, reading-order, alternative-text quality, footnote, contrast, and resizing reviews. An automated audit alone cannot establish accessibility conformance.
- Calibrate scrolling and paginated Chromium previews against actual readers, obfuscated fonts, fixed layouts, vertical writing, and interactive footnotes. Paginated mode currently supports only horizontal LTR reflowable content. The generic browser sizes are not device profiles.
- Open replaced covers in Calibre on the Mac and on the intended reader. Browser comparison does not certify ebook-reader rendering.
- Exercise more real-world cover declarations, image reuse, unusual colour profiles, palettes/transparency, translated editions, and multi-author collections. Missing/ambiguous/SVG cover declarations remain unsupported for replacement.
- Verify a successful live Google Books response when quota permits. HTTP 429 is reported as a provider failure, not corroboration.
- Linux Calibre 9.16.0 import, rich-library backup/restore, CLI reopening, and real rclone local transfer are now verified. The target Mac, a personal production library, and authenticated Google Drive delivery still need checks; the Linux fixture does not establish access to the user's account or devices.
- Evaluate Meta advice on a broader set of real editions and ambiguous/conflicting records. An authenticated synthetic-evidence smoke test passed on 2026-10-18; contract/failure coverage still uses deterministic fixtures.
- Revalidation now has process-kill recovery and injected report/database-write failure coverage. Broaden this to other workflows, real filesystem exhaustion, and machine/power loss.

These remaining checks do not make the completed preservation tests or official EPUBCheck runs substitutes for device review.
