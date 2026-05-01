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
    conn.commit()
