#!/usr/bin/env bash
# Local PostgreSQL 16 + pgvector WITHOUT Docker.
#
# Fallback for machines where `docker compose up -d` is unavailable. It runs the PostgreSQL
# binaries bundled in the `pgserver` pip package (install: `uv pip install pgserver`).
# Note: that bundle ships `vector` but not the contrib extensions `uuid-ossp` / `pgcrypto`;
# the migration enables those only when available (the Docker image has them).
#
# Usage: scripts/local_postgres.sh start|stop|status
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
PGDATA="${PGDATA:-$ROOT/.pgdata}"
PGPORT="${PGPORT:-5432}"
PGBIN="$("$PY" -c 'import pathlib, pgserver; print(pathlib.Path(pgserver.__file__).parent / "pginstall" / "bin")')"

psql_admin() {
  PGPASSWORD=postgres "$PGBIN/psql" -h localhost -p "$PGPORT" -U postgres -v ON_ERROR_STOP=1 -qtA "$@"
}

case "${1:-}" in
  start)
    if [ ! -f "$PGDATA/PG_VERSION" ]; then
      pwfile="$(mktemp)"
      echo "postgres" > "$pwfile"
      "$PGBIN/initdb" -D "$PGDATA" -U postgres --pwfile="$pwfile" \
        --auth-local=trust --auth-host=scram-sha-256 --encoding=UTF8 >/dev/null
      rm -f "$pwfile"
    fi
    "$PGBIN/pg_ctl" -D "$PGDATA" -l "$PGDATA/server.log" -w \
      -o "-p $PGPORT -k $PGDATA -c listen_addresses=localhost" start
    if [ "$(psql_admin -d postgres -c "SELECT 1 FROM pg_database WHERE datname = 'ats_db'")" != "1" ]; then
      psql_admin -d postgres -c "CREATE DATABASE ats_db"
    fi
    echo "PostgreSQL ready on localhost:$PGPORT (db: ats_db, user: postgres)"
    ;;
  stop)
    "$PGBIN/pg_ctl" -D "$PGDATA" -m fast stop
    ;;
  status)
    "$PGBIN/pg_ctl" -D "$PGDATA" status
    ;;
  *)
    echo "usage: $0 start|stop|status" >&2
    exit 2
    ;;
esac
