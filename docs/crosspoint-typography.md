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

## Images and covers

CrossPoint picks an image decoder by file extension and decodes only JPEG, PNG and
GIF (`lib/FsHelpers`, `ImageDecoderFactory`). Other formats, such as WebP or SVG, are
left out of the page, or replaced by `[Image: alt text]` when the image has alt text.
A WebP cover produces no home-screen or sleep cover. The Preview step lists these
images under **On your X4 Pro**. Emend does not convert image formats.

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
