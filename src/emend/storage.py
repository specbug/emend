import fcntl
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


def now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, root: Path):
        self.root = root
        for directory in ["inbox", "originals", "workspace", "curated", "reports", ".staging"]:
            (root / directory).mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS books (
                    id TEXT PRIMARY KEY, source_name TEXT NOT NULL, state TEXT NOT NULL,
                    updated_at TEXT NOT NULL, report_path TEXT, artifact_path TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, book_id TEXT NOT NULL, state TEXT NOT NULL,
                    at TEXT NOT NULL, detail TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS publications (
                    book_id TEXT NOT NULL, target_key TEXT NOT NULL, status TEXT NOT NULL,
                    updated_at TEXT NOT NULL, receipt TEXT NOT NULL,
                    PRIMARY KEY(book_id, target_key)
                );
                CREATE TABLE IF NOT EXISTS research_plans (
                    id TEXT PRIMARY KEY, book_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    path TEXT NOT NULL, sha256 TEXT NOT NULL
                );
            """)

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.root / "state.db", timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def locked(self):
        with (self.root / ".pipeline.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError(
                    "Another Emend operation is running. Retry when it finishes."
                ) from exc
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def transition(
        self,
        book_id: str,
        source: str,
        state: str,
        detail: str,
        report_path: Path | None = None,
        artifact_path: Path | None = None,
    ):
        with self.db() as db:
            db.execute(
                """INSERT INTO books VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at,
                    report_path=excluded.report_path, artifact_path=excluded.artifact_path""",
                (
                    book_id,
                    source,
                    state,
                    now(),
                    str(report_path) if report_path else None,
                    str(artifact_path) if artifact_path else None,
                ),
            )
            db.execute(
                "INSERT INTO events(book_id, state, at, detail) VALUES (?, ?, ?, ?)",
                (book_id, state, now(), detail),
            )

    def resolve(self, prefix: str) -> dict:
        if not re.fullmatch(r"[a-f0-9]{8,64}", prefix):
            raise ValueError("Use a full book SHA-256 or a unique prefix of at least 8 characters.")
        with self.db() as db:
            rows = db.execute("SELECT * FROM books WHERE id LIKE ?", (prefix + "%",)).fetchall()
        if len(rows) != 1:
            raise ValueError("Book ID is unknown or ambiguous.")
        return dict(rows[0])

    def books(self) -> list[dict]:
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM books ORDER BY updated_at DESC")]

    def receipt(self, book_id: str, key: str) -> dict | None:
        with self.db() as db:
            row = db.execute(
                "SELECT * FROM publications WHERE book_id=? AND target_key=?", (book_id, key)
            ).fetchone()
        return dict(row) if row else None

    def save_receipt(self, book_id: str, key: str, status: str, receipt: dict):
        with self.db() as db:
            db.execute(
                "INSERT OR REPLACE INTO publications VALUES (?, ?, ?, ?, ?)",
                (book_id, key, status, now(), json.dumps(receipt)),
            )

    def event(self, book_id: str, state: str, detail: str):
        with self.db() as db:
            db.execute(
                "INSERT INTO events(book_id, state, at, detail) VALUES (?, ?, ?, ?)",
                (book_id, state, now(), detail),
            )

    def save_plan(self, identifier: str, book_id: str, path: Path, digest: str):
        with self.db() as db:
            db.execute(
                "INSERT INTO research_plans VALUES (?, ?, ?, ?, ?)",
                (identifier, book_id, now(), str(path), digest),
            )

    def plan(self, book_id: str, identifier: str | None = None) -> dict:
        with self.db() as db:
            if identifier:
                row = db.execute(
                    "SELECT * FROM research_plans WHERE book_id=? AND id=?", (book_id, identifier)
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM research_plans WHERE book_id=? ORDER BY created_at DESC LIMIT 1",
                    (book_id,),
                ).fetchone()
        if row is None:
            raise ValueError("No matching research plan. Run emend research first.")
        return dict(row)
