"""Fixture setup/readback through Calibre's own Python API via calibre-debug.

Emend itself never writes Calibre databases directly. This helper creates notes
and annotations that have no dedicated calibredb setter, then reads them back.
"""

import hashlib
import io
import json
import sys

from calibre.library import db


def main():
    action, library, book = sys.argv[1:]
    book = int(book)
    backend = db(library)
    api = backend.new_api
    try:
        author = api.field_ids_for("authors", book)[0]
        if action == "enrich":
            resource = api.add_notes_resource(b"original note attachment", "reference.txt")
            api.set_notes_for(
                "authors",
                author,
                "<p>Author research — keep this rich note.</p>",
                "Author research — keep this rich note.",
                resource_hashes=(resource,),
            )
            annotation = {
                "type": "highlight",
                "uuid": "emend-original-fixture-highlight",
                "timestamp": "2026-01-01T00:00:00Z",
                "highlighted_text": "Mira opened the shutters",
                "notes": "A retained viewer annotation — café.",
                "start_cfi": "epubcfi(/6/2!/4/2/1:0)",
                "end_cfi": "epubcfi(/6/2!/4/2/1:24)",
                "style": {"kind": "color", "which": "yellow"},
            }
            api.set_annotations_for_book(book, "EPUB", [(annotation, 1767225600.0)])
            api.add_extra_files(
                book, {"data/reading-notes.txt": io.BytesIO(b"Supplementary research notes.")}
            )
        elif action != "inspect":
            raise ValueError("Expected enrich or inspect")
        notes = api.notes_data_for("authors", author)
        resources = sorted(api.notes_resources_used_by("authors", author))
        result = {
            "custom_status": api.field_for("#reading_status", book),
            "comments": api.field_for("comments", book),
            "tags": sorted(api.field_for("tags", book)),
            "series": api.field_for("series", book),
            "series_index": api.field_for("series_index", book),
            "rating": api.field_for("rating", book),
            "formats": sorted(api.formats(book)),
            "note_html": notes["doc"],
            "resources": {
                key: hashlib.sha256(api.get_notes_resource(key)["data"]).hexdigest()
                for key in resources
            },
            "annotations": list(api.all_annotations_for_book(book)),
            "extra_files": sorted(item.relpath for item in api.list_extra_files(book)),
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    finally:
        backend.close()


if __name__ == "__main__":
    main()
