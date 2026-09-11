#!/usr/bin/env bash
#
# Start the crawler API locally the way Railway starts it.
#
# Railway builds the Dockerfile and runs `./docker-entrypoint.sh serve`
# (railway.json), which execs `python server.py`: the 12-hourly cycle plus the
# POST /crawl endpoint the app's dashboard button calls. This script reaches the
# same process without Docker -- it prepares a venv, checks the two settings the
# server refuses to start without, and hands over to the same entrypoint, so the
# role names and defaults stay the ones documented in docker-entrypoint.sh.
#
#   ./start-api.sh                 serve: schedule + trigger endpoint (default)
#   ./start-api.sh cron            one full cycle -- crawl, score, assess -- then exit
#   ./start-api.sh crawl           crawl only, then exit
#   ./start-api.sh score --llm     re-score and re-assess
#   ./start-api.sh --docker        the true Railway shape: build the image and run it
#
# Flags (before the role): --docker, --reinstall, --skip-install, --port N.
#
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

VENV="venv"
STAMP="$VENV/.requirements.sha"
USE_DOCKER=0
REINSTALL=0
SKIP_INSTALL=0
PORT_OVERRIDE=""

die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
info() { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarn:\033[0m %s\n' "$*" >&2; }

while [ $# -gt 0 ]; do
    case "$1" in
        --docker)       USE_DOCKER=1; shift ;;
        --reinstall)    REINSTALL=1; shift ;;
        --skip-install) SKIP_INSTALL=1; shift ;;
        --port)         PORT_OVERRIDE="${2:-}"; [ -n "$PORT_OVERRIDE" ] || die "--port needs a number"; shift 2 ;;
        --port=*)       PORT_OVERRIDE="${1#*=}"; shift ;;
        -h|--help)      sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        --)             shift; break ;;
        *)              break ;;
    esac
done

ROLE="${1:-serve}"
[ $# -gt 0 ] && shift || true

# --- settings -------------------------------------------------------------
# server.py loads .env itself, but the checks below run before it starts, so
# read the file here too. Real environment variables win, exactly as dotenv
# does it: a value already exported is not overwritten.
if [ -f .env ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        printf '%s' "$line" | grep -qE '^[A-Za-z_][A-Za-z0-9_]*=' || continue
        key="${line%%=*}"
        value="${line#*=}"
        value="${value%$'\r'}"
        # Strip one layer of surrounding quotes, as dotenv does.
        case "$value" in
            \"*\") value="${value#\"}"; value="${value%\"}" ;;
            \'*\') value="${value#\'}"; value="${value%\'}" ;;
        esac
        [ -n "${!key:-}" ] || export "$key=$value"
    done < .env
else
    warn ".env not found -- copy .env.example and fill it in, or export the settings yourself"
fi

[ -n "$PORT_OVERRIDE" ] && export PORT="$PORT_OVERRIDE"
export PORT="${PORT:-8080}"

# The server exits rather than expose an unauthenticated trigger, and the batch
# roles cannot do anything without a database. Say so here, with the fix, rather
# than let a traceback three seconds later be the message.
if [ "$ROLE" = "serve" ] && [ -z "${CRAWLER_TRIGGER_TOKEN:-}" ]; then
    die "CRAWLER_TRIGGER_TOKEN is unset. Generate one with 'openssl rand -hex 32' and set it in .env
       and to the same value in the tender-intelligence app, or the dashboard's button is refused."
fi
[ -n "${DATABASE_URL:-}" ] || die "DATABASE_URL is unset -- point it at the database the tender-intelligence app migrated."

# A closed port here is the common local failure: compose's Postgres is not up.
# It is a warning, not an error, because a remote DATABASE_URL is perfectly
# valid and this check only understands host:port.
db_hostport=$(printf '%s' "$DATABASE_URL" | sed -nE 's#^[a-z+]+://([^@/]*@)?([^/?:]+)(:([0-9]+))?.*#\2 \4#p')
if [ -n "$db_hostport" ]; then
    read -r db_host db_port <<<"$db_hostport"
    db_port="${db_port:-5432}"
    if ! (exec 3<>"/dev/tcp/$db_host/$db_port") 2>/dev/null; then
        warn "nothing listening on $db_host:$db_port -- start it with 'docker compose up -d db' if that is the local one"
    fi
fi

# --- docker: the shape Railway actually deploys ---------------------------
if [ "$USE_DOCKER" = "1" ]; then
    command -v docker >/dev/null || die "docker is not installed"
    info "building the image Railway builds"
    docker build -t tender-intelligence-crawler .
    [ -f .env ] || die "--docker passes .env to the container, and there is none"
    info "running '$ROLE' on :$PORT"
    # The container reaches a database on the host through host.docker.internal,
    # which needs the mapping added explicitly on Linux.
    exec docker run --rm -it \
        --add-host host.docker.internal:host-gateway \
        --env-file .env \
        -e PORT="$PORT" \
        -e DATABASE_URL="$(printf '%s' "$DATABASE_URL" | sed -e 's/@localhost:/@host.docker.internal:/' -e 's/@127\.0\.0\.1:/@host.docker.internal:/')" \
        -p "$PORT:$PORT" \
        tender-intelligence-crawler "$ROLE" "$@"
fi

# --- venv -----------------------------------------------------------------
# The image is built on 3.11, so prefer it when the host has it and fall back to
# whatever python3 is on PATH.
if [ ! -d "$VENV" ]; then
    # Being on PATH is not enough to pick an interpreter here. A pyenv shim for a
    # version that is not the active one exits non-zero when run, and a Debian
    # system python is routinely shipped without `ensurepip`, which `-m venv`
    # needs. So each candidate is tried on both counts, and pyenv's own
    # installed versions are searched as a fallback -- they always carry it.
    py=""
    for candidate in \
        ${PYTHON:-} python3.11 python3.12 python3 \
        "$HOME"/.pyenv/versions/3.11.*/bin/python \
        "$HOME"/.pyenv/versions/3.12.*/bin/python
    do
        [ -n "$candidate" ] || continue
        if "$candidate" -c 'import ensurepip' >/dev/null 2>&1; then py="$candidate"; break; fi
    done
    [ -n "$py" ] || die "found no python3 that can create a venv.
       Install one -- 'apt install python3-venv' on Debian/Ubuntu -- or point PYTHON= at an interpreter that has ensurepip."
    info "creating $VENV with $("$py" --version 2>&1) ($py)"
    "$py" -m venv "$VENV" || { rm -rf "$VENV"; die "could not create the venv"; }
fi

# shellcheck disable=SC1091
. "$VENV/bin/activate"

# Reinstall only when requirements.txt has actually changed. Without the stamp
# every start pays for a full dependency resolution.
if [ "$SKIP_INSTALL" != "1" ]; then
    want=$(sha256sum requirements.txt | cut -d' ' -f1)
    if [ "$REINSTALL" = "1" ] || [ "$(cat "$STAMP" 2>/dev/null || true)" != "$want" ]; then
        info "installing dependencies"
        python -m pip install --quiet --upgrade pip
        python -m pip install --quiet -r requirements.txt || die "dependency install failed"
        printf '%s\n' "$want" > "$STAMP"
    fi
fi

# --- run ------------------------------------------------------------------
# Straight to the entrypoint the Railway start command uses, so the roles, the
# defaults and the log lines are identical to production's.
if [ "$ROLE" = "serve" ]; then
    info "http://localhost:$PORT/health  ·  POST /crawl with header X-Trigger-Token"
fi
exec ./docker-entrypoint.sh "$ROLE" "$@"
