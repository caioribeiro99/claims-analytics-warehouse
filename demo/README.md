# Local analytics demo

Two optional ways to explore the warehouse built by `make build`, without a cloud
account or a server:

| Command | What runs | Needs |
| --- | --- | --- |
| `make demo` | Metabase in Docker, connected read-only to the warehouse, with every example analysis loaded as a saved question and the charts on a dashboard | Docker (Compose v2) |
| `make ui` | The DuckDB local UI, a notebook-style SQL IDE, over a read-only attach that prevents accidental writes | Internet (see below) |

```sh
make demo                     # builds the warehouse first, then prints the URL and a sign-in
METABASE_PORT=3300 make demo  # if port 3000 is taken
make demo-down                # stop Metabase, keep everything
make demo-reset               # delete Metabase's data and the generated credentials
```

## What `make demo` does

`demo/demo.py up` (standard library only) runs
`docker compose -f demo/compose.yaml up -d --build --wait` and then provisions
Metabase through its REST API:

1. Creates the admin user on first run, with a random password, and saves it
   to `.demo/metabase-admin.json` once Metabase has accepted it. Later runs
   reuse it.
2. Connects the DuckDB database **Northstar claims warehouse** and waits for
   the schema sync.
3. Upserts one saved native question per `analyses/*.sql` into the collection
   **Northstar example analyses**. The header comments of each file drive it:

   ```sql
   -- title: Claims by network                   -> question name
   -- chart: bar                                 -> table | bar | line | row
   -- x: network                                 -> chart dimension
   -- y: net_claims                              -> chart metric(s), comma separated
   -- stacked: true                              -> stacked bars (optional)
   -- Any other leading comment line             -> question description
   ```

4. Lays the chart questions out on the dashboard **Northstar overview**.
5. Runs every question once. If any fails, for example because the warehouse
   was built before an `analyses/` change, it names them and exits non-zero.
   `make demo` rebuilds the warehouse first, so this only shows up when
   `demo.py up` is run directly.

Every step is idempotent. Running `make demo` again updates questions in place
(matched by title), restores the dashboard layout, and never duplicates
anything. A question you save yourself is left alone. Renaming an analysis
title creates a new question, and the old one stays until you archive it.

When the script finishes it prints the URL, the sign-in, where the content is,
and how to write ad-hoc SQL: **New -> SQL query -> Northstar claims warehouse**.
To turn a result into a chart, use the **Visualization** button.
`docs/data-model.html` explains the tables, their grain and how they join.

## Why a custom image

- **glibc, not musl.** The official `metabase/metabase` image is Alpine (musl).
  The DuckDB driver ships a native library built against glibc/libstdc++, so it
  cannot load there (driver README; driver issues #36, #62, #97). The image here
  is `eclipse-temurin:25-jre-noble` (Ubuntu 24.04), because Metabase 0.63
  documents Java 25 as required.
- **Works offline after the build.** The driver connects with `TimeZone=UTC`,
  which makes DuckDB autoload its `icu` extension. Without a pre-installed copy
  and a writable `~/.duckdb`, that first connection tries to download the
  extension and fails offline (driver issues #60, #107). The image pre-installs
  `icu` for the build architecture, under a non-root user with a real home.
- **Spill space.** The driver puts DuckDB's `temp_directory` next to the
  database file, which cannot be created on a read-only mount. Ordinary queries
  never touch it. A query too large for memory would fail with an IO error, so
  the connection points `temp_directory` at `/tmp/duckdb` inside the container.

## Pinned components

The three artifact downloads happen at image build time and are verified with
`sha256sum -c`. The base image is pinned by digest, and `curl` comes from
Ubuntu's signed apt repository. Nothing binary is committed. The pins are
`ARG`s at the top of `demo/metabase/Dockerfile`. An upgrade edits those `ARG`s,
the image tag in `demo/compose.yaml` (it carries both versions) and the table
below.

| Component | Version | License | Source |
| --- | --- | --- | --- |
| Metabase OSS | v0.63.16.9 | AGPL-3.0 (downloaded at build time, not redistributed) | `https://downloads.metabase.com/v0.63.16.9/metabase.jar` |
| MotherDuck Metabase DuckDB driver | 1.5.5.0 (bundles DuckDB 1.5.5) | Apache-2.0 | `https://github.com/motherduckdb/metabase_duckdb_driver/releases` |
| DuckDB `icu` extension | v1.5.5, `linux_amd64` or `linux_arm64` | MIT | `https://extensions.duckdb.org/v1.5.5/<platform>/icu.duckdb_extension.gz` |
| Eclipse Temurin JRE base image | `25-jre-noble`, pinned by digest | GPL-2.0 with Classpath Exception | Docker Hub `eclipse-temurin` |

The DuckDB UI extension used by `make ui` is not pinned. DuckDB downloads it
into `~/.duckdb/extensions` on first use, and the UI loads its web assets from
the DuckDB website.

## Security posture

- **Loopback only.** The port is published as `127.0.0.1:${METABASE_PORT}`,
  never on `0.0.0.0`, and the DuckDB UI listens on localhost.
- **Generated credentials, kept out of git.** The password comes from
  `secrets.token_urlsafe`. It is stored in `.demo/metabase-admin.json` (mode
  0600, directory 0700, git-ignored) and removed by `make demo-reset`.
  `MB_DEMO_EMAIL` / `MB_DEMO_PASSWORD` choose them on a fresh install only.
  Later runs refuse a different value instead of losing the saved password.
- **Read-only twice.** The warehouse directory is bind-mounted `read_only`,
  and the DuckDB connection is opened with `read_only=true`. DuckDB refuses
  `CREATE`/`INSERT`/`UPDATE`. The mount also refuses file writes such as
  `COPY ... TO '/warehouse/...'`, which DuckDB's read-only mode would otherwise
  allow.
- **`make ui` is not a sandbox.** Its read-only attach prevents accidental
  writes, but the UI can still `DETACH` the file and `ATTACH` it again
  read-write. Only the Metabase demo has the read-only mount behind it.
- **Quiet and minimal.** Metabase runs as a non-root user, with every Linux
  capability dropped and `no-new-privileges`. Anonymous tracking, update checks
  and sample content are off. There is no restart policy, so the demo never
  comes back on its own when Docker starts.
- **Local only.** The application database is H2 inside a Docker volume. That
  is fine for one person on one machine, and not a template for a shared
  deployment.

## Refreshing after `make build`

Run `make demo` again. It always regenerates the data and rebuilds the warehouse
first, so it always restarts Metabase on the new file. Two details make that work:

- Compose mounts the `warehouse/` **directory**, not the file. The build writes
  a new file and atomically renames it into place, and a single-file bind mount
  would keep serving the old inode.
- Metabase keeps a pooled DuckDB connection, and an open connection keeps
  reading the file it opened. When the warehouse file is newer than the
  container's start time, `demo.py up` restarts Metabase, re-syncs the schema,
  and re-applies the questions. Only `.venv/bin/python demo/demo.py up` run
  against an unchanged warehouse leaves Metabase running: `demo.py` builds
  without BuildKit's default provenance attestation, which would otherwise give
  every cached rebuild a new image ID and make compose recreate the container.

## Troubleshooting

- **Port already in use.** `demo.py` checks before starting. Pick another port:
  `METABASE_PORT=3300 make demo`.
- **The first build is slow.** It downloads roughly 860 MB: the Metabase jar
  (663 MB), the driver (86 MB), the JRE base image (about 100 MB) and the `icu`
  extension (7 MB). The image takes about 1.9 GB on disk. Later runs start in
  well under a minute.
- **Apple Silicon and amd64** are both supported: BuildKit's `TARGETARCH`
  selects the matching `icu` build and checksum.
- **Docker not running / Compose missing / no warehouse.** `demo.py` stops with
  a message saying which one it is. For a missing warehouse, run `make build`.
- **Metabase did not become healthy.** Read its log with
  `docker compose -f demo/compose.yaml logs metabase`.
- **"Metabase rejected the sign-in".** The saved credentials are missing or no
  longer match Metabase's data, for example because `.demo/` was deleted by
  hand. Run `make demo-reset`, then `make demo`.
- **"questions fail against the current warehouse".** The warehouse is older
  than `analyses/`. Run `make demo`, which rebuilds it first.
- **`make ui` fails to start.** It needs internet: the UI extension downloads
  on first use, and the UI loads its assets from ui.duckdb.org every time. If it says the port is in use,
  another `make ui` is still running: stop that one first.

## Removing everything

```sh
make demo-reset                                                   # container, network, data volume, credentials
docker image rm "$(docker compose -f demo/compose.yaml config --images)"  # the image compose built
```

`make demo-down` only stops the container. Your users, questions and dashboards
stay until `make demo-reset`.

## Files

| File | Purpose |
| --- | --- |
| `metabase/Dockerfile` | Metabase + DuckDB driver + `icu` on a glibc JRE, pinned and checksum-verified |
| `compose.yaml` | Project `claims-analytics-demo`: one loopback-only service, a data volume, a read-only warehouse mount |
| `demo.py` | `up` / `down` / `reset`, and API provisioning |
| `duckdb_ui.py` | The DuckDB local UI over a read-only attach (`make ui`) |
