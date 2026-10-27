# Covers on CrossPoint / X4 Pro

CrossPoint **2.0.0-rc** generates its own cover and thumbnail BMP cache files from
the cover declared in the EPUB. Emend embeds/replaces approved JPEG/PNG images and
exports the EPUB; it does not create firmware cache files or a per-book BMP sidecar.
A cover shown in Calibre's library can be separate from the image embedded in an
EPUB. The new book page reports what is actually declared inside the EPUB.

## If the home grid or sleep screen has no cover

1. Look under **Preview** in Emend for **Cover found in EPUB**, its image type,
   dimensions and any warnings. If no image is declared, use **Book details and
   cover** to compare a sourced JPEG/PNG. If Calibre already has the correct
   library cover, its **Polish books → Update the cover in the book files** option
   can embed that artwork; select only the intended operation, save an EPUB copy,
   and import the copy into Emend for fresh checks. Emend can add a missing cover only when
   there are no conflicting existing cover-page hints or fixed-layout declarations.
   Existing ambiguous/SVG/wrapper declarations may need Calibre's Edit book first.
2. Open the newly transferred book on CrossPoint and allow it to load. For sleep,
   select **Settings → Display → Sleep Screen → Cover**, then sleep while the book
   is open. **Cover + Custom** intentionally falls back to a custom image when
   not reading; timeout Quick Resume settings can also replace the cover screen.
3. If the embedded cover is present but still missing on-device, transfer a copy
   under a new filename and open that copy. In this firmware, the EPUB cache key
   depends on its device filepath; a new name exercises a fresh cache without
   deleting the old book or its reading data. This is a diagnostic, not a guarantee.
4. Check the image format and extension. This version chooses the BMP converter
   using `.jpg`/`.jpeg`, `.png`, or `.gif` extensions. A JPEG stored under a different
   extension can be a valid-looking library resource but fail this device path.
   Emend flags a JPEG/PNG format/extension mismatch rather than silently renaming
   resources. The firmware also documents unsupported interlaced GIFs.

The reader can take several seconds to convert a large cover. A failed or
unsupported thumbnail conversion can leave an empty BMP marker, and existing
cache files can prevent regeneration. Do not delete the entire `.crosspoint`
folder as a first troubleshooting step: it can contain reading data. A fresh
filename is preferable when diagnosing cache behavior.

These checks are based on the pinned firmware source, not a test on the user's
hardware or a guarantee that every PNG/JPEG decodes on the device:

- [EPUB cover and thumbnail generation](https://github.com/crosspoint-reader/crosspoint-reader/blob/2.0.0rc/lib/Epub/Epub.cpp)
- [OPF cover selection](https://github.com/crosspoint-reader/crosspoint-reader/blob/2.0.0rc/lib/Epub/Epub/parsers/ContentOpfParser.cpp)
- [Sleep cover selection](https://github.com/crosspoint-reader/crosspoint-reader/blob/2.0.0rc/src/activities/boot_sleep/SleepActivity.cpp)
- [Firmware user guide](https://github.com/crosspoint-reader/crosspoint-reader/blob/2.0.0rc/USER_GUIDE.md)
