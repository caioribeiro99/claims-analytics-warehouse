"""Local analytics demo: Metabase over the warehouse, provisioned through its REST API.

    .venv/bin/python demo/demo.py up      start Metabase, connect the warehouse, load the examples
    .venv/bin/python demo/demo.py down    stop Metabase; keeps users, questions and dashboards
    .venv/bin/python demo/demo.py reset   remove the container, its data and the saved credentials

METABASE_PORT picks the loopback port (default 3000). Stdlib only, and `up` is
idempotent: running it again updates what it created instead of adding copies.
Sign-in credentials are generated once into .demo/metabase-admin.json (git-ignored);
MB_DEMO_EMAIL / MB_DEMO_PASSWORD choose them instead, on the first `up` after a reset only.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parent
ROOT = DEMO_DIR.parent
COMPOSE_FILE = DEMO_DIR / "compose.yaml"
WAREHOUSE = ROOT / "warehouse" / "claims_warehouse.duckdb"
ANALYSES_DIR = ROOT / "analyses"
CREDENTIALS = ROOT / ".demo" / "metabase-admin.json"

SERVICE = "metabase"
DEMO_EMAIL = "analyst@northstar.example"
DATABASE_NAME = "Northstar claims warehouse"
COLLECTION_NAME = "Northstar example analyses"
DASHBOARD_NAME = "Northstar overview"
DATABASE_DETAILS = {
    # A path inside the container: compose mounts ../warehouse read-only at /warehouse.
    "database_file": "/warehouse/claims_warehouse.duckdb",
    "read_only": True,
    "old_implicit_casting": True,
    # The driver puts DuckDB's temp_directory next to the database file, which
    # cannot be created on a read-only mount. Ordinary queries never need it, but
    # one that spills to disk would fail with an IO error, so spill inside the
    # container instead.
    "additional-options": "temp_directory=/tmp/duckdb",
}
EXPECTED_TABLES = {"fct_claims", "fct_lookups", "fct_reversals", "rejects", "build_info"}
CHART_DISPLAYS = {"bar", "line", "row"}
METADATA_LINE = re.compile(r"(title|chart|x|y|stacked):\s*(.*)")
GRID_WIDTH, CARD_HEIGHT = 24, 8  # Metabase dashboards are 24 grid columns wide


class DemoError(Exception):
    """A problem the user can act on; reported without a traceback."""


class ApiError(DemoError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


# --- docker ------------------------------------------------------------------


def compose(*args: str, capture: bool = False, check: bool = True) -> str:
    result = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), *args],
        text=True,
        capture_output=capture,
        # BuildKit's default provenance attestation differs on every build, so even a
        # fully cached `up --build` would get a new image ID and compose would recreate
        # (restart) Metabase on every run although nothing changed.
        env={**os.environ, "BUILDX_NO_DEFAULT_ATTESTATIONS": "1"},
    )
    if check and result.returncode != 0:
        detail = f":\n{result.stderr.strip()}" if capture else " (see the output above)"
        raise DemoError(f"`docker compose {' '.join(args)}` failed{detail}")
    return result.stdout.strip() if capture and result.returncode == 0 else ""


def require_docker() -> None:
    if shutil.which("docker") is None:
        raise DemoError("docker is not installed - see https://docs.docker.com/get-docker/")
    if subprocess.run(["docker", "compose", "version"], capture_output=True).returncode != 0:
        raise DemoError("Docker Compose v2 is missing (`docker compose version` fails)")
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        raise DemoError("the Docker daemon is not running - start Docker and try again")


def require_free_port(port: int) -> None:
    """Fail early, with a hint, when something other than this demo holds the port."""
    if compose("port", SERVICE, "3000", capture=True, check=False) == f"127.0.0.1:{port}":
        return
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            raise DemoError(
                f"127.0.0.1:{port} is already in use - choose another port, "
                "e.g. METABASE_PORT=3300 make demo"
            )


def docker_timestamp(value: str) -> float:
    # Docker prints nanoseconds (2026-01-31T09:15:02.123456789Z); datetime takes microseconds.
    whole, _, fraction = value.strip().rstrip("Z").partition(".")
    return datetime.fromisoformat(whole).replace(tzinfo=UTC).timestamp() + float(f"0.{fraction}")


def restart_if_warehouse_is_newer() -> None:
    # Metabase keeps a pooled DuckDB connection, and an open connection keeps
    # reading the file it opened even after `make build` renames a new one into
    # place. Only a fresh process sees the new build.
    container = compose("ps", "-q", SERVICE, capture=True)
    inspect = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.StartedAt}}", container],
        text=True,
        capture_output=True,
    )
    if not container or inspect.returncode != 0:
        raise DemoError(
            "Metabase exited right after starting - "
            "see `docker compose -f demo/compose.yaml logs metabase`"
        )
    if WAREHOUSE.stat().st_mtime > docker_timestamp(inspect.stdout):
        print("The warehouse was rebuilt after Metabase started: restarting Metabase ...")
        compose("restart", SERVICE)


# --- Metabase API ------------------------------------------------------------


class Metabase:
    """Just enough of the Metabase REST API."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.session: str | None = None

    def request(self, method: str, path: str, body: dict | None = None):
        request = urllib.request.Request(
            self.base_url + path,
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={"Content-Type": "application/json"},
        )
        if self.session:
            request.add_header("X-Metabase-Session", self.session)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise ApiError(exc.code, f"{method} {path} -> HTTP {exc.code}: {detail}") from None
        return json.loads(payload) if payload else None

    def get(self, path: str):
        return self.request("GET", path)

    def post(self, path: str, body: dict | None = None):
        return self.request("POST", path, body or {})

    def put(self, path: str, body: dict):
        return self.request("PUT", path, body)


def wait_until(condition, what: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if condition():
                return
        except (OSError, ValueError, ApiError):  # not listening yet, or still starting up
            pass
        time.sleep(2)
    raise DemoError(
        f"timed out waiting for {what} - see `docker compose -f demo/compose.yaml logs metabase`"
    )


def load_credentials() -> tuple[dict, bool]:
    """The saved sign-in, or new credentials (and True) when none are saved yet."""
    overrides = {
        key: value
        for key, value in (
            ("email", os.environ.get("MB_DEMO_EMAIL")),
            ("password", os.environ.get("MB_DEMO_PASSWORD")),
        )
        if value
    }
    if not CREDENTIALS.exists():
        credentials = {"email": DEMO_EMAIL, "password": secrets.token_urlsafe(18)}
        return {**credentials, **overrides}, True
    saved = json.loads(CREDENTIALS.read_text(encoding="utf-8"))
    # The saved password is the only one Metabase knows, so an override must not replace it.
    if any(saved.get(key) != value for key, value in overrides.items()):
        raise DemoError(
            "MB_DEMO_EMAIL / MB_DEMO_PASSWORD only apply to a fresh install - unset them, "
            "or run `make demo-reset` first (it deletes Metabase's data)"
        )
    return saved, False


def save_credentials(credentials: dict) -> None:
    CREDENTIALS.parent.mkdir(mode=0o700, exist_ok=True)
    # Created 0600 up front so the password is never readable by others, even briefly.
    fd = os.open(CREDENTIALS, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(credentials, file, indent=2)
        file.write("\n")


def sign_in(api: Metabase, credentials: dict) -> None:
    properties = api.get("/api/session/properties")
    if not properties["has-user-setup"]:
        try:
            session = api.post(
                "/api/setup",
                {
                    "token": properties["setup-token"],
                    "user": {**credentials, "first_name": "Northstar", "last_name": "Analyst"},
                    "prefs": {
                        "site_name": "Northstar Claims Analytics",
                        "site_locale": "en",
                        "allow_tracking": False,
                    },
                },
            )
        except ApiError as exc:
            if exc.status != 400:
                raise
            # Not the response itself: Metabase echoes a rejected password back in it.
            raise DemoError(
                "Metabase refused to create the admin user - if MB_DEMO_EMAIL or "
                "MB_DEMO_PASSWORD is set, use a valid email and a less common password"
            ) from None
    else:
        login = {"username": credentials["email"], "password": credentials["password"]}
        try:
            session = api.post("/api/session", login)
        except ApiError as exc:
            if exc.status in (400, 401):
                raise DemoError(
                    f"Metabase rejected the sign-in from {CREDENTIALS.relative_to(ROOT)} "
                    "(missing or out of date) - run `make demo-reset` to start over"
                ) from None
            raise
    api.session = session["id"]


def ensure_database(api: Metabase) -> int:
    existing = next(
        (db for db in api.get("/api/database")["data"] if db["name"] == DATABASE_NAME), None
    )
    if existing is None:
        database_id = api.post(
            "/api/database",
            {"name": DATABASE_NAME, "engine": "duckdb", "details": DATABASE_DETAILS},
        )["id"]
    else:
        database_id = existing["id"]
        details = api.get(f"/api/database/{database_id}")["details"]
        if any(details.get(key) != value for key, value in DATABASE_DETAILS.items()):
            api.put(f"/api/database/{database_id}", {"details": DATABASE_DETAILS})
        api.post(f"/api/database/{database_id}/sync_schema")

    def tables_visible() -> bool:
        tables = api.get(f"/api/database/{database_id}?include=tables")["tables"]
        return {table["name"] for table in tables} >= EXPECTED_TABLES

    wait_until(tables_visible, "the warehouse tables to sync", timeout=180)
    return database_id


def ensure_collection(api: Metabase) -> int:
    for collection in api.get("/api/collection"):
        if collection["name"] == COLLECTION_NAME and collection.get("personal_owner_id") is None:
            return collection["id"]
    return api.post(
        "/api/collection",
        {
            "name": COLLECTION_NAME,
            "description": "One saved question per analyses/*.sql file, loaded by `make demo`.",
        },
    )["id"]


def collection_items(api: Metabase, collection_id: int, model: str) -> dict[str, int]:
    items = api.get(f"/api/collection/{collection_id}/items?models={model}")["data"]
    return {item["name"]: item["id"] for item in items}


# --- content -----------------------------------------------------------------


def _csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def parse_analysis(path: Path) -> dict:
    """An analyses/*.sql file: leading `--` comments (metadata + description), then the SQL."""
    lines = path.read_text(encoding="utf-8").splitlines()
    header = list(itertools.takewhile(lambda line: line.startswith("--"), lines))
    metadata, description = {}, []
    for line in header:
        text = line[2:].removeprefix(" ").rstrip()
        match = METADATA_LINE.fullmatch(text)
        if match:
            metadata[match[1]] = match[2].strip()
        else:
            description.append(text)
    if "title" not in metadata:
        raise DemoError(f"{path.name} has no '-- title:' line")

    display = metadata.get("chart", "table")
    settings = {}
    if display in CHART_DISPLAYS:
        settings = {
            "graph.dimensions": _csv(metadata.get("x", "")),
            "graph.metrics": _csv(metadata.get("y", "")),
        }
        if metadata.get("stacked") == "true":
            settings["stackable.stack_type"] = "stacked"
    return {
        "name": metadata["title"],
        "description": "\n".join(description).strip() or None,
        "display": display,
        "visualization_settings": settings,
        # Metabase may wrap a native query (row limits, downloads); a trailing ';' breaks that.
        "sql": "\n".join(lines[len(header) :]).strip().removesuffix(";").rstrip(),
    }


def upsert_cards(api: Metabase, database_id: int, collection_id: int) -> list[dict]:
    existing = collection_items(api, collection_id, "card")
    cards = []
    for path in sorted(ANALYSES_DIR.glob("*.sql")):
        analysis = parse_analysis(path)
        payload = {
            "name": analysis["name"],
            "description": analysis["description"],
            "display": analysis["display"],
            "visualization_settings": analysis["visualization_settings"],
            "collection_id": collection_id,
            "dataset_query": {
                "database": database_id,
                "type": "native",
                "native": {"query": analysis["sql"], "template-tags": {}},
            },
        }
        if analysis["name"] in existing:
            cards.append(api.put(f"/api/card/{existing[analysis['name']]}", payload))
        else:
            cards.append(api.post("/api/card", payload))
    return cards


def failing_cards(api: Metabase, cards: list[dict]) -> list[str]:
    """Names of the saved questions that do not run against the current warehouse."""
    failing = []
    for card in cards:
        try:
            completed = api.post(f"/api/card/{card['id']}/query")["status"] == "completed"
        except ApiError:  # some query errors come back as HTTP 400, not as status "failed"
            completed = False
        if not completed:
            failing.append(card["name"])
    return failing


def upsert_dashboard(api: Metabase, collection_id: int, cards: list[dict]) -> int:
    dashboard_id = collection_items(api, collection_id, "dashboard").get(DASHBOARD_NAME)
    if dashboard_id is None:
        dashboard_id = api.post(
            "/api/dashboard",
            {
                "name": DASHBOARD_NAME,
                "collection_id": collection_id,
                "description": "The example charts. `make demo` restores this layout on every run.",
            },
        )["id"]

    # The PUT replaces the whole set of dashcards, so nothing accumulates. Reusing
    # the id of a dashcard that already shows a card updates it in place; new
    # dashcards take negative placeholder ids.
    current = {
        dc["card_id"]: dc["id"] for dc in api.get(f"/api/dashboard/{dashboard_id}")["dashcards"]
    }
    charts = [card for card in cards if card["display"] in CHART_DISPLAYS]
    half = GRID_WIDTH // 2
    dashcards = []
    for index, card in enumerate(charts):
        alone_on_last_row = index == len(charts) - 1 and index % 2 == 0
        dashcards.append(
            {
                "id": current.get(card["id"], -(index + 1)),
                "card_id": card["id"],
                "row": index // 2 * CARD_HEIGHT,
                "col": 0 if alone_on_last_row else index % 2 * half,
                "size_x": GRID_WIDTH if alone_on_last_row else half,
                "size_y": CARD_HEIGHT,
                "parameter_mappings": [],
                "visualization_settings": {},
                "series": [],
            }
        )
    api.put(f"/api/dashboard/{dashboard_id}", {"dashcards": dashcards})
    return dashboard_id


# --- commands ----------------------------------------------------------------


def up(port: int) -> None:
    require_docker()
    if not WAREHOUSE.is_file():
        raise DemoError(f"{WAREHOUSE.relative_to(ROOT)} not found - run `make build` first")
    require_free_port(port)

    print(f"Starting Metabase on 127.0.0.1:{port} (the first run builds the image) ...")
    compose("up", "-d", "--build", "--wait", SERVICE)
    restart_if_warehouse_is_newer()

    api = Metabase(f"http://127.0.0.1:{port}")
    wait_until(lambda: api.get("/api/health")["status"] == "ok", "Metabase to start", timeout=300)
    credentials, generated = load_credentials()
    sign_in(api, credentials)
    if generated:
        save_credentials(credentials)  # only once Metabase has accepted them
    print("Connecting the warehouse and loading the example questions ...")
    database_id = ensure_database(api)
    collection_id = ensure_collection(api)
    cards = upsert_cards(api, database_id, collection_id)
    dashboard_id = upsert_dashboard(api, collection_id, cards)
    failing = failing_cards(api, cards)
    print_guide(api.base_url, credentials, collection_id, dashboard_id, len(cards))
    # Raised after the guide: the demo is still usable, but must not look healthy.
    if failing:
        raise DemoError(
            f"{len(failing)} of {len(cards)} questions fail against the current warehouse "
            f"({'; '.join(failing)}) - it may predate analyses/: run `make demo`, "
            "which rebuilds it first"
        )


def print_guide(
    url: str, credentials: dict, collection_id: int, dashboard_id: int, cards: int
) -> None:
    print(f"""
Metabase is ready at {url}

  Sign in     {credentials["email"]}
  Password    {credentials["password"]}
              (random and local-only; saved in {CREDENTIALS.relative_to(ROOT)})

  Dashboard   "{DASHBOARD_NAME}"  {url}/dashboard/{dashboard_id}
  Questions   "{COLLECTION_NAME}" ({cards} questions, one per analyses/*.sql)
              {url}/collection/{collection_id}

Ad-hoc SQL: New -> SQL query -> pick "{DATABASE_NAME}", type, then Cmd/Ctrl+Enter.
Two queries to start from:

  SELECT network, count(*) AS claims,
         sum(net_retained_fee) FILTER (WHERE attribution_status = 'attributed') AS net_retained_fee
  FROM fct_claims GROUP BY network ORDER BY claims DESC

  SELECT date_trunc('month', submitted_at) AS month, sum(net_service_fee) AS net_service_fee
  FROM fct_claims GROUP BY 1 ORDER BY 1

To chart a result, click Visualization (bottom left), pick a chart type, then Save.
The warehouse is mounted read-only: queries can read everything and change nothing.

  make demo-down    stop Metabase and keep everything
  make demo-reset   delete Metabase's data and these credentials
  docs/data-model.html explains the tables, their grain and how they join.
""")


def down() -> None:
    require_docker()
    compose("stop", SERVICE)
    print("Metabase stopped; users, questions and dashboards are kept. `make demo` resumes.")


def reset() -> None:
    require_docker()
    compose("down", "--volumes")
    CREDENTIALS.unlink(missing_ok=True)
    print("Metabase container, its data volume and the saved credentials are removed.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("up", "down", "reset"))
    args = parser.parse_args(argv)
    # docker compose writes straight to the terminal; line buffering keeps this
    # script's messages in order with its output when piped (make demo | tee).
    sys.stdout.reconfigure(line_buffering=True)
    try:
        port = os.environ.get("METABASE_PORT", "3000")
        if not port.isdigit() or not 0 < int(port) < 65536:
            raise DemoError(f"METABASE_PORT must be a TCP port number, got {port!r}")
        # compose publishes the same port this script talks to
        os.environ["METABASE_PORT"] = port
        if args.command == "up":
            up(int(port))
        elif args.command == "down":
            down()
        else:
            reset()
    except DemoError as exc:
        sys.exit(f"demo: {exc}")


if __name__ == "__main__":
    main()
