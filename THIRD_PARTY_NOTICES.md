# Bundled report fonts

Emend embeds these fonts in standalone HTML reports, allowing them to render offline without third-party requests. They do not modify fonts inside any EPUB.

- **DM Sans**, from [Google Fonts](https://github.com/google/fonts/tree/main/ofl/dmsans). Distributed under the SIL Open Font License 1.1. The full license and copyright notice are in `src/emend/assets/DMSans-OFL.txt`.
- **IBM Plex Mono**, from [Google Fonts](https://github.com/google/fonts/tree/main/ofl/ibmplexmono). Distributed under the SIL Open Font License 1.1. The full license and copyright notice are in `src/emend/assets/IBMPlexMono-OFL.txt`.

The font license notices accompany both the package and each generated HTML report.

The optional cloud setup script downloads EPUBCheck from its official W3C release and checks the distribution SHA-256. EPUBCheck is a separate tool with its own included licenses; it is not bundled in the Emend wheel.
