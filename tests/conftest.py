import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from emend.config import Config
from tests.epub_factory import write_epub


@pytest.fixture
def epub(tmp_path):
    return write_epub(tmp_path / "a book.epub")


@pytest.fixture
def config(tmp_path):
    settings = Config(root=tmp_path / "Emend")
    # A deterministic executable fixture tests our subprocess/report boundary.
    # Real EPUBCheck validation is tested separately when the official JAR is available.
    tool = tmp_path / "epubcheck"
    tool.write_text(
        f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\nPath(sys.argv[sys.argv.index('--json')+1]).write_text(json.dumps({{'messages': []}}))\n"
    )
    tool.chmod(0o755)
    settings.tools.epubcheck = tool
    settings.tools.ace = tmp_path / "missing-ace"
    return settings


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "Calibre Library"
    root.mkdir()
    db = sqlite3.connect(root / "metadata.db")
    db.executescript("""
        CREATE TABLE books(id INTEGER PRIMARY KEY, title TEXT, author_sort TEXT);
        CREATE TABLE custom_columns(id INTEGER PRIMARY KEY, label TEXT, name TEXT, datatype TEXT);
        CREATE TABLE custom_column_1(book INTEGER PRIMARY KEY, value TEXT);
        INSERT INTO books VALUES(1, 'The Quiet Observatory', 'Corpus, Emend');
        INSERT INTO custom_columns VALUES(1, 'reading_status', 'Reading status', 'text');
        INSERT INTO custom_column_1 VALUES(1, 'Read and annotated');
    """)
    db.close()
    book = root / "Emend Test Corpus" / "The Quiet Observatory (1)"
    book.mkdir(parents=True)
    write_epub(book / "The Quiet Observatory.epub")
    (book / "The Quiet Observatory.pdf").write_bytes(b"test-pdf-format")
    (book / "cover.jpg").write_bytes(b"test-cover")
    (book / "metadata.opf").write_text("<metadata><title>The Quiet Observatory</title></metadata>")
    (book / "data").mkdir()
    (book / "data" / "annotations.json").write_text(
        json.dumps({"chapter": 1, "note": "Keep this."})
    )
    (root / ".notes").mkdir()
    (root / ".notes" / "notes.db").write_bytes(b"test-note-database")
    return root


@pytest.fixture
def fake_publishers(tmp_path, config, library):
    bin_dir = tmp_path / "calibre-bin"
    bin_dir.mkdir()
    tool = bin_dir / "calibredb"
    tool.write_text(f"""#!{sys.executable}
import json,sys,shutil
from pathlib import Path
args=sys.argv[1:]
root=Path(args[args.index('--with-library')+1])
state=root/'fake-published.json'
rows=json.loads(state.read_text()) if state.exists() else []
if args[0]=='list':
    print(json.dumps(rows))
elif args[0]=='add':
    identifier=args[args.index('--identifier')+1].split(':',1)[1]
    epub=root/'Imported'/f'{{identifier}}.epub'
    epub.parent.mkdir(exist_ok=True)
    shutil.copyfile(args[-1],epub)
    rows.append({{"id":len(rows)+1,"identifiers":{{"emend":identifier}},"formats":[str(epub)]}})
    state.write_text(json.dumps(rows))
    print('Added book ids: '+str(len(rows)))
""")
    tool.chmod(0o755)
    config.tools.calibre_bin = bin_dir
    config.publish.calibre_library = library
    rclone = tmp_path / "rclone"
    rclone.write_text(f"""#!{sys.executable}
import sys,shutil
from pathlib import Path
args=sys.argv[1:]
destination=Path({str(tmp_path / "fake-drive")!r})/args[-1].split(':',1)[1]
destination.parent.mkdir(parents=True,exist_ok=True)
shutil.copyfile(args[-2],destination)
""")
    rclone.chmod(0o755)
    config.tools.rclone = rclone
    config.publish.drive_remote = "gdrive:curated"
    config.publish.backup_remote = "gdrive:backups"
    return config


from tests.test_edition_discovery import search_responses  # noqa: E402, F401


@pytest.fixture
def browser_config(config):
    pytest.importorskip("playwright.sync_api")
    browser = os.environ.get("EMEND_TEST_CHROMIUM")
    if browser:
        config.tools.chromium = Path(browser)
    elif not os.environ.get("EMEND_TEST_PLAYWRIGHT"):
        pytest.skip("Set EMEND_TEST_CHROMIUM or EMEND_TEST_PLAYWRIGHT=1 for browser integration")
    return config


from tests.test_calibre_integration import real_calibre  # noqa: E402, F401
from tests.test_review_ui import workspace  # noqa: E402, F401
from tests.test_typography import fake_capture  # noqa: E402, F401
