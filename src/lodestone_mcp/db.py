import os
from pathlib import Path

# Prefer pysqlite3 (bundles a SQLite built with loadable-extension support).
# Fall back to stdlib if the user is on a Python that already supports it.
try:
    import pysqlite3 as sqlite3  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    import sqlite3  # type: ignore[no-redef]

import sqlite_vec

DEFAULT_DB_PATH = Path(
    os.environ.get("LODESTONE_DB", Path.home() / ".lodestone" / "memory.db")
)


def open_db(path: Path | None = None):
    db_path = path or DEFAULT_DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row

    _ensure_schema(conn)
    return conn


def _ensure_schema(conn) -> None:
    schema_path = Path(__file__).parent / "schema.sql"
    conn.executescript(schema_path.read_text())
    _migrate(conn)
    conn.commit()


def _migrate(conn) -> None:
    """Apply additive schema changes that CREATE TABLE IF NOT EXISTS can't.

    SQLite has no 'ADD COLUMN IF NOT EXISTS', so we introspect and ALTER
    only when missing. Each block is idempotent.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
    if "project_label" not in cols:
        conn.execute("ALTER TABLE memories ADD COLUMN project_label TEXT")

    # memory_links CHECK constraint can't be ALTERed in SQLite. Recreate the
    # table only if the current schema doesn't include the new link kind.
    schema_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_links'"
    ).fetchone()
    if schema_row and "co_recalled_with" not in (schema_row["sql"] or ""):
        conn.executescript("""
            ALTER TABLE memory_links RENAME TO memory_links_old;
            CREATE TABLE memory_links (
              from_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
              to_id   INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
              kind    TEXT NOT NULL CHECK (kind IN (
                        'supersedes','related','contradicts','caused_by',
                        'co_recalled_with'
                      )),
              PRIMARY KEY (from_id, to_id, kind)
            );
            INSERT INTO memory_links SELECT * FROM memory_links_old;
            DROP TABLE memory_links_old;
        """)
