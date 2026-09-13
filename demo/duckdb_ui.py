"""Optional SQL IDE: the DuckDB local UI over a read-only attach of the warehouse.

    .venv/bin/python demo/duckdb_ui.py --db warehouse/claims_warehouse.duckdb   (make ui)

Needs internet: the `ui` extension is downloaded into ~/.duckdb on first use,
and the UI loads its web assets from the DuckDB website. The server itself
listens only on localhost and runs inside this process, so Ctrl-C stops it.

The warehouse is attached READ_ONLY into an in-memory database to prevent
accidental writes. It is not a sandbox: the UI can still DETACH the file and
ATTACH it again read-write. `make build` can replace the file while the UI
runs, but the attach keeps reading the build it opened; restart to see a new one.
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
from pathlib import Path

import duckdb

SCHEMAS = ("main", "staging")


def open_warehouse(db: Path) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()  # in-memory
    # ATTACH accepts no prepared parameters, so the path goes in as a quoted literal.
    path = str(db).replace("'", "''")
    con.execute(f"ATTACH '{path}' AS warehouse (READ_ONLY)")
    # The UI opens its own connections, so a `USE warehouse` here would not
    # reach them. Views in the default in-memory catalog do, and they make
    # unqualified names (fct_claims, staging.claims) work as in the other tools.
    objects = con.execute(
        """SELECT schema_name, table_name FROM duckdb_tables()
           WHERE database_name = 'warehouse' AND list_contains($schemas, schema_name)
           UNION ALL
           SELECT schema_name, view_name FROM duckdb_views()
           WHERE database_name = 'warehouse' AND list_contains($schemas, schema_name)
             AND NOT internal
           ORDER BY ALL""",
        {"schemas": list(SCHEMAS)},
    ).fetchall()
    for schema, name in objects:
        con.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        con.execute(
            f'CREATE VIEW "{schema}"."{name}" AS SELECT * FROM warehouse."{schema}"."{name}"'
        )
    return con


def port_in_use(port: int) -> bool:
    try:
        socket.create_connection(("localhost", port), timeout=1).close()
    except OSError:
        return False
    return True


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=Path("warehouse/claims_warehouse.duckdb"))
    parser.add_argument(
        "--no-browser", action="store_true", help="start the server without opening a browser"
    )
    args = parser.parse_args(argv)
    if not args.db.exists():
        sys.exit(f"{args.db} not found - run `make build` first")

    con = open_warehouse(args.db)
    try:
        # Reading the setting loads the ui extension, downloading it on first use.
        port = con.execute("SELECT current_setting('ui_local_port')").fetchone()[0]
        # start_ui() also "succeeds" when another process holds the port, and this
        # one would then print a URL served by that process, maybe over an older build.
        if port_in_use(port):
            sys.exit(f"localhost:{port} is already in use (another `make ui`?) - stop that first")
        con.execute("CALL start_ui_server()" if args.no_browser else "CALL start_ui()")
    except duckdb.Error as exc:
        sys.exit(
            f"could not start the DuckDB UI (it needs internet: the extension downloads on first use and the UI loads its assets from ui.duckdb.org): {exc}"
        )
    # flush: the process then blocks, and piped output would otherwise hide the URL
    print(
        f"DuckDB UI: http://localhost:{port}  (warehouse attached read-only; Ctrl-C to stop)",
        flush=True,
    )
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nstopping the DuckDB UI")
    finally:
        con.execute("CALL stop_ui_server()")
        con.close()


if __name__ == "__main__":
    main()
