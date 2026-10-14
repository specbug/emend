"""Small original test books: no downloaded or copyrighted book corpus required."""

import io
import zipfile
from pathlib import Path

from PIL import Image


def book_files(version="3.0") -> dict[str, bytes]:
    image = io.BytesIO()
    Image.new("RGB", (160, 240), "#345a4a").save(image, "PNG")
    epub3 = version == "3.0"
    files = {
        "mimetype": b"application/epub+zip",
        "META-INF/container.xml": b"""<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>""",
        "EPUB/package.opf": f'''<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="{version}" unique-identifier="book-id"{' xml:lang="en"' if epub3 else ""}>
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="book-id">urn:uuid:54fa42c8-0442-4e92-947c-646dd26e1693</dc:identifier>
<dc:title>The Quiet Observatory</dc:title><dc:creator>Emend Test Corpus</dc:creator>
<dc:language>en</dc:language><dc:publisher>Emend</dc:publisher>
{'<meta property="dcterms:modified">2026-01-01T00:00:00Z</meta>' if epub3 else ""}
</metadata><manifest>
<item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
<item id="notes" href="notes.xhtml" media-type="application/xhtml+xml"/>
<item id="style" href="style.css" media-type="text/css"/>
<item id="image" href="observatory.png" media-type="image/png"/>
{'<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>' if epub3 else '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'}
</manifest><spine{' toc="ncx"' if not epub3 else ""}><itemref idref="chapter"/><itemref idref="notes"/></spine></package>'''.encode(),
        "EPUB/chapter.xhtml": b"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en"><head><title>At the Observatory</title><link rel="stylesheet" type="text/css" href="style.css"/></head>
<body><h1 id="start">At the Observatory</h1><p>At dusk, Mira opened the shutters and watched the first star appear.</p>
<p>The old telescope made a soft clicking sound as she turned it toward the horizon. Nothing in the room had moved since morning, except the light.</p>
<p>She recorded one observation in her notebook.<a id="ref1" href="notes.xhtml#note1">1</a></p>
<p><img src="observatory.png" alt="A simple green illustration of the observatory"/></p></body></html>""",
        "EPUB/notes.xhtml": b"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en"><head><title>Notes</title></head><body>
<h1>Notes</h1><p id="note1">1. The observation was the time of the first visible star. <a href="chapter.xhtml#ref1">Return</a></p></body></html>""",
        "EPUB/style.css": b"body { margin: 5%; } p { line-height: 1.5; } img { max-width: 100%; height: auto; }",
        "EPUB/observatory.png": image.getvalue(),
    }
    if epub3:
        files["EPUB/nav.xhtml"] = b"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en"><head><title>Contents</title></head><body><nav epub:type="toc" id="toc"><h1>Contents</h1><ol><li><a href="chapter.xhtml#start">At the Observatory</a></li><li><a href="notes.xhtml#note1">Notes</a></li></ol></nav></body></html>"""
    else:
        files["EPUB/toc.ncx"] = b"""<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head><meta name="dtb:uid" content="urn:uuid:54fa42c8-0442-4e92-947c-646dd26e1693"/><meta name="dtb:depth" content="1"/><meta name="dtb:totalPageCount" content="0"/><meta name="dtb:maxPageNumber" content="0"/></head><docTitle><text>The Quiet Observatory</text></docTitle><navMap><navPoint id="n1" playOrder="1"><navLabel><text>At the Observatory</text></navLabel><content src="chapter.xhtml#start"/></navPoint><navPoint id="n2" playOrder="2"><navLabel><text>Notes</text></navLabel><content src="notes.xhtml#note1"/></navPoint></navMap></ncx>"""
    return files


def write_epub(path: Path, files: dict[str, bytes] | None = None, version="3.0") -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in (files if files is not None else book_files(version)).items():
            archive.writestr(
                name,
                data,
                compress_type=zipfile.ZIP_STORED if name == "mimetype" else zipfile.ZIP_DEFLATED,
            )
    return path
