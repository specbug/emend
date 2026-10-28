# Typography on CrossPoint / X4 Pro

These notes come from reading the pinned CrossPoint **2.0.0-rc** source, not from a test
on the user's hardware. Confirm them on the device before relying on them.

## What the firmware does with a book's styles

With **Settings → Reader → Embedded Style** on (the default), CrossPoint reads a
subset of each book's CSS:

- **Typeface:** set on the device, never by the book. `@font-face` rules are skipped
  and `font-family` is not read. The reader font is **Reader Font Family**: Noto Sans
  built in, or families installed on the SD card as `.cpfont` (or TTF/OTF/TTC on models
  with external RAM). Embedding a font in an EPUB has no effect here.
- **Text size:** the base size is **Reader Font Size** (12–18 pt for built-in fonts).
  The book's relative `font-size` values (`em`, `rem`, `%` and keywords such as
  `small`) *are* applied on top of it. `rem` and keywords such as `small` are relative
  to the reader's size; `em` and `%` to the surrounding text. A publisher rule such as
  `body { font-size: 0.8em }` therefore makes the book read smaller than others at the
  same device setting.
- **Line height** is not read. Line spacing comes from **Reader Line Spacing**
  (Tight, Normal, Wide). Paragraph margins, `text-indent`, alignment, bold/italic and
  some borders and padding are read.
- **Conflicts:** `!important` is stripped, rules inside `@media` are skipped, and
  rules resolve by selector specificity. A generic override stylesheet added after the
  publisher's CSS does not reliably beat a more specific publisher rule such as
  `p.text`.

## Image decoding on the reader

CrossPoint picks an image decoder by file extension and decodes only JPEG, PNG and
GIF (`lib/FsHelpers`, `ImageDecoderFactory`). Other formats, such as WebP or SVG, are
left out of the page, or replaced by `[Image: alt text]` when the image has alt text.
A WebP cover produces no home-screen or sleep cover. The Preview step lists these
images under **On your X4 Pro**. Emend now offers an explicit checked conversion under Refine; see Images and covers below.

Elements hidden only by `font-size: 0` and `height: 0` stay visible on CrossPoint: it
rejects a zero font size and applies `height` only to images. `display: none` hides them.

## What Emend does

**Refine → Text size on your reader** finds `font-size` values in plain `html`, `body`
and `p` rules that change the reader's size. After a compared preview and explicit
approval, it resets only those values (`1em`, `1rem` or `100%`). Editing the
publisher's own values is the only change the firmware is certain to honour. Class
and id rules (often footnotes or captions), headings, inline styles, `@media` blocks
and absolute `px`/`pt` sizes stay as published and are listed. A book whose sizes
depend on a changed root size (`html { font-size: 62.5% }` with `rem` elsewhere) is
left alone. Every other byte is verified unchanged before the candidate is validated
and activated.

Spacing trials still set line height for other readers and for the browser preview,
but on CrossPoint only their paragraph spacing has an effect.

## Sources

- [CSS parser: supported selectors and ignored at-rules](https://github.com/crosspoint-reader/crosspoint-reader/blob/2.0.0rc/lib/Epub/Epub/css/CssParser.h)
- [CSS properties read and `font-size` keyword scale](https://github.com/crosspoint-reader/crosspoint-reader/blob/2.0.0rc/lib/Epub/Epub/css/CssParser.cpp)
- [Style model](https://github.com/crosspoint-reader/crosspoint-reader/blob/2.0.0rc/lib/Epub/Epub/css/CssStyle.h)
- [Image decoders by extension](https://github.com/crosspoint-reader/crosspoint-reader/blob/2.0.0rc/lib/FsHelpers/FsHelpers.cpp)
- [Reader settings in the user guide](https://github.com/crosspoint-reader/crosspoint-reader/blob/2.0.0rc/USER_GUIDE.md)

## Images and covers

From pinned **2.0.0rc**, commit `91ed3ef`, **source, not a device test**:

- `lib/Epub/Epub/converters/ImageDecoderFactory.cpp` selects JPEG, PNG or GIF by
  extension; `lib/FsHelpers/FsHelpers.cpp` recognises `.jpg`, `.jpeg`, `.png`, `.gif`.
  WebP has no decoder, including for cover/thumbnail generation. Unsupported
  in-page images can be omitted or represented by their alternative text.
- `lib/JpegToBmpConverter/JpegToBmpConverter.cpp` decodes progressive JPEGs at 1/8
  scale and smooths them. Emend writes **baseline** JPEGs only.
- `lib/PngToBmpConverter/PngToBmpConverter.cpp` rejects interlaced PNG. Emend writes
  **non-interlaced** PNGs only.
- `lib/Epub/Epub/converters/ImageToFramebufferDecoder.h` caps in-page source images
  at **8,388,608 pixels**. JPEG/PNG BMP conversion caps dimensions at **2048 × 3072**;
  `src/activities/boot_sleep/SleepActivity.cpp` also caps PNG source pixels at
  **2048 × 1536**. Emend reports these limits as advisories and never resizes here.

**Refine → Images and covers → Compare image conversion** creates a checked
candidate and baseline/converted page comparison. **Apply image conversion** is
separate, explicit and runs EPUBCheck (required for this edit) and configured
accessibility gates before activation. Nothing is converted during import.

PNG is chosen for lossless/unknown-losslessness sources, alpha, palettes and
images with at most 256 decoded colours or low colour entropy (< 5 bits). Lossy photographic sources become
baseline JPEG at quality **98**, with no chroma subsampling and **PSNR ≥ 40 dB**
against decoded source RGB pixels (or exact equality). PNG RGBA pixels must match
exactly. All images retain dimensions and ICC profiles; colour images are never
converted to grayscale. A profile/mode requiring a separate colour conversion is
left for review. This keeps the same EPUB useful on the iPad too.

Manifest IDs, `meta name="cover"`, `cover-image` and fallback IDs stay intact.
Only converted item href extensions/media types and corresponding references
change. ZIP entry order, spine and `mimetype` remain intact. Supported references
include img/srcset, SVG image href/xlink:href, stylesheet URLs and style attributes,
with relative directories, percent-encoding and fragments resolved. Name collisions
use a deterministic `.emend-N` suffix. XML is serialized as UTF-8; unrelated
resource payloads remain byte-identical. The OPF modification date is left intact
in this operation; only converted manifest href/media-type values change there.

Animated/multiframe images, SVG vectors, undecodable images and over-limit images
are skipped and listed. Fixed layouts, scripts, malformed documents, custom bases,
unknown reference attributes, embedded style elements referencing converted images,
and CSS image references outside `url()` refuse the whole plan. A conservative
local srcset grammar is supported; ambiguous/data source sets remain manual. CSS
must be UTF-8 compatible so bytes outside changed URL tokens remain exact.

Limits: 1,000 manifest images, 20 MiB/40 MP per image, 256 MP across converted
sources, 8 MiB per reference document (CSS 2 MiB), 20,000 grouped references and
16 MiB of plan JSON. No decompression-bomb protection is disabled. The summary
reports output formats, skipped images, unchanged oversize dimensions and actual
book byte-size change. Pixel/validator/browser checks do not certify either device.
