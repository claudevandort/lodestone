import argparse
import sys

from . import db
from .project import derive_project_id


def purge() -> None:
    parser = argparse.ArgumentParser(
        prog="lodestone-purge",
        description="Delete memories from the lodestone DB. Requires a scope flag.",
    )
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--all", action="store_true", help="wipe ALL projects")
    grp.add_argument("--current", action="store_true",
                     help="wipe only the current project (derived from cwd)")
    grp.add_argument("--project", type=str, metavar="ID",
                     help="wipe one project by id")
    parser.add_argument("--yes", action="store_true",
                        help="skip the confirmation prompt")
    args = parser.parse_args()

    conn = db.open_db()

    if args.all:
        target = "ALL projects"
        where, params = "", ()
    elif args.current:
        pid, label = derive_project_id()
        target = f"current project ({label})"
        where, params = "WHERE project_id = ?", (pid,)
    else:
        target = f"project {args.project}"
        where, params = "WHERE project_id = ?", (args.project,)

    count = conn.execute(
        f"SELECT COUNT(*) AS n FROM memories {where}", params
    ).fetchone()["n"]

    if count == 0:
        print(f"No memories to purge for {target}.")
        return

    if not args.yes:
        resp = input(f"Purge {count} memories from {target}? [y/N] ")
        if resp.strip().lower() not in ("y", "yes"):
            print("Aborted.")
            sys.exit(1)

    # Vec virtual table has no FK or trigger; clean it explicitly first.
    ids = [r["id"] for r in conn.execute(f"SELECT id FROM memories {where}", params)]
    if ids:
        conn.executemany(
            "DELETE FROM memory_vec WHERE memory_id = ?", [(i,) for i in ids]
        )
    # memories DELETE → cascades to memory_tags & memory_links (FK),
    # fires mem_ad trigger to clean memory_fts.
    conn.execute(f"DELETE FROM memories {where}", params)
    conn.commit()

    print(f"Purged {count} memories from {target}.")
