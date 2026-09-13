# Metabase OSS + the MotherDuck DuckDB driver, for the local analytics demo.
#
# Why not the official metabase/metabase image: it is Alpine (musl), and the
# driver ships a native DuckDB library built against glibc/libstdc++, so it
# cannot load there (driver README; driver issues #36, #62, #97). A glibc JRE
# base avoids that. Metabase 0.63 documents Java 25 as required.
#
# Pinned for reproducible builds: the base image by digest, the three downloads
# by sha256 (curl itself comes from Ubuntu's signed apt repository). Nothing
# binary is committed. An upgrade edits the ARGs below and the version-carrying
# image tag in ../compose.yaml.
ARG BASE_IMAGE=eclipse-temurin:25-jre-noble@sha256:d120abd9d8d7dec94520ce974ece62d0e4eed8576eb00bbc84e6128307ab48ef
ARG METABASE_VERSION=0.63.16.9
ARG METABASE_SHA256=297394b9649174eac1328b152b7ceeeed945d230a2bb9dcbf2b03fb13a1e870a
ARG DUCKDB_DRIVER_VERSION=1.5.5.0
ARG DUCKDB_DRIVER_SHA256=47a0d41ca52ff82a6ce78b0c52b2bdb58b32094405e446734cefc44eb1ddbd07
# Must match the DuckDB version bundled in the driver (its JDBC 1.5.5.0).
ARG DUCKDB_VERSION=1.5.5
ARG ICU_SHA256_AMD64=4beae6de79895ded1641f9e2d411300a0923850f7a89ee690dc7d5902b123baa
ARG ICU_SHA256_ARM64=2756baf49929fe8cf14d54affb2a5830f9db5428b440a030e3b17fe4b0d615a1

FROM ${BASE_IMAGE} AS base
# The base image has neither curl nor wget: curl serves the downloads below and
# the compose healthcheck.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

FROM base AS fetch
ARG METABASE_VERSION
ARG METABASE_SHA256
ARG DUCKDB_DRIVER_VERSION
ARG DUCKDB_DRIVER_SHA256
ARG DUCKDB_VERSION
ARG ICU_SHA256_AMD64
ARG ICU_SHA256_ARM64
ARG TARGETARCH
WORKDIR /fetch

# One layer per download, so a later failure does not re-download the large jar.
RUN curl -fsSL -o metabase.jar \
        "https://downloads.metabase.com/v${METABASE_VERSION}/metabase.jar" \
 && echo "${METABASE_SHA256}  metabase.jar" | sha256sum -c -

RUN curl -fsSL -o duckdb.metabase-driver.jar \
        "https://github.com/motherduckdb/metabase_duckdb_driver/releases/download/${DUCKDB_DRIVER_VERSION}/duckdb.metabase-driver.jar" \
 && echo "${DUCKDB_DRIVER_SHA256}  duckdb.metabase-driver.jar" | sha256sum -c -

# The driver connects with TimeZone=UTC, which makes DuckDB autoload the icu
# extension. Without a pre-installed copy DuckDB tries to download it on first
# connect, which fails offline or without a writable ~/.duckdb (driver issues
# #60 and #107). DuckDB's platform names match BuildKit's TARGETARCH values.
RUN case "${TARGETARCH}" in \
        amd64) sha="${ICU_SHA256_AMD64}" ;; \
        arm64) sha="${ICU_SHA256_ARM64}" ;; \
        *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
 && curl -fsSL -o icu.duckdb_extension.gz \
        "https://extensions.duckdb.org/v${DUCKDB_VERSION}/linux_${TARGETARCH}/icu.duckdb_extension.gz" \
 && echo "${sha}  icu.duckdb_extension.gz" | sha256sum -c - \
 && mkdir -p "duckdb/extensions/v${DUCKDB_VERSION}/linux_${TARGETARCH}" \
 && gunzip -c icu.duckdb_extension.gz \
        > "duckdb/extensions/v${DUCKDB_VERSION}/linux_${TARGETARCH}/icu.duckdb_extension"

FROM base
# A real, writable HOME: DuckDB resolves its extension directory from it.
RUN useradd --system --create-home --home-dir /home/metabase --shell /usr/sbin/nologin metabase \
 && mkdir -p /app /plugins /metabase-data \
 && chown metabase:metabase /plugins /metabase-data
# The Metabase jar stays root-owned: the service only reads it. /plugins (where
# compose.yaml points MB_PLUGINS_DIR) must be writable: Metabase refuses a
# plugins directory it cannot write to, falls back to an empty temporary one,
# and the DuckDB driver never loads. /metabase-data pre-exists with the
# service's owner so a fresh named volume inherits it.
COPY --from=fetch /fetch/metabase.jar /app/metabase.jar
COPY --from=fetch --chown=metabase:metabase /fetch/duckdb.metabase-driver.jar /plugins/
COPY --from=fetch --chown=metabase:metabase /fetch/duckdb /home/metabase/.duckdb

USER metabase
WORKDIR /home/metabase
EXPOSE 3000
CMD ["java", "-jar", "/app/metabase.jar"]
