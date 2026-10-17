# Remaining integration and corpus validation

The user subsequently authorized testing while travelling. Edition discovery and cover review now pass 22 new tests and the complete 157-test suite, including official EPUBCheck on EPUB 2/3 replacements. Live Open Library search/download and desktop/mobile report rendering were exercised. See [validation evidence](validation.md).

Further validation needs real library/device access or a broader corpus:

- Calibrate the new Chromium document samples against actual reader pagination, obfuscated fonts, fixed layouts, vertical writing, and interactive footnotes. The generic browser sizes are not device profiles.
- Open replaced covers in Calibre on the Mac and on the intended reader. Browser comparison does not certify ebook-reader rendering.
- Exercise more real-world cover declarations, image reuse, unusual colour profiles, palettes/transparency, translated editions, and multi-author collections. Missing/ambiguous/SVG cover declarations remain unsupported for replacement.
- Verify a successful live Google Books response when quota permits. HTTP 429 is reported as a provider failure, not corroboration.
- Test a real Calibre library backup/restore and authenticated Google Drive delivery locally. The full-fidelity fixture and real rclone local-backend checks do not establish access to the user's account or library.
- Run authenticated Meta generation after the user adds the deferred key. Current contract and failure tests use fixtures.
- Extend interruption testing beyond validator rejection and database rollback to process termination and filesystem exhaustion.

These remaining checks do not make the completed preservation tests or official EPUBCheck runs substitutes for device review.
