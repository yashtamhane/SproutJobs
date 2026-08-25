#!/bin/sh
# One-command setup and run for macOS/Linux.
#   ./run.sh                 first-time setup, full run, then serve + open the dashboard
#   ./run.sh --no-open       same, but serve without launching a browser
#   ./run.sh --no-serve      collect and export only; do not start the dashboard
#   ./run.sh --deep          also sweep the Internet Archive index (slow, wide)
#   ./run.sh --skip-discovery --max-companies 20     extra flags go to `jobfinder run`
#
# The dashboard is a small local server (127.0.0.1:8765) reading your own
# SQLite file, so every filter covers the complete job set. Ctrl+C stops it.
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VENV_DIR="$PROJECT_DIR/.venv"
OPEN_DASHBOARD=1

# Pick an interpreter new enough for tomllib and datetime.UTC.  Many systems
# still ship 3.9 as plain `python3`, so try the versioned names too.
supports_project() {
    command -v "$1" >/dev/null 2>&1 &&
        "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null
}

find_python() {
    if [ -n "${PYTHON_BIN:-}" ]; then
        if supports_project "$PYTHON_BIN"; then
            echo "$PYTHON_BIN"
            return 0
        fi
        return 1
    fi
    for candidate in python3.14 python3.13 python3.12 python3.11 python3 python; do
        if supports_project "$candidate"; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

# Accept --no-open/--no-serve anywhere in the argument list; forward the rest.
RUN_ARGS=""
START_DASHBOARD=1
for arg in "$@"; do
    case "$arg" in
        --no-open) OPEN_DASHBOARD=0 ;;
        --no-serve) START_DASHBOARD=0 ;;
        *) RUN_ARGS="$RUN_ARGS $arg" ;;
    esac
done

if [ ! -x "$VENV_DIR/bin/python" ]; then
    if ! PYTHON="$(find_python)"; then
        echo "SproutJobs needs Python 3.11 or newer." >&2
        echo "Install it (macOS: 'brew install python@3.12'), or point at an existing" >&2
        echo "copy with: PYTHON_BIN=/path/to/python3.12 ./run.sh" >&2
        exit 1
    fi
    echo "Creating virtual environment in .venv using $("$PYTHON" --version 2>&1) ..."
    "$PYTHON" -m venv "$VENV_DIR"
fi

# Reinstall only when the dependency list actually changed.
STAMP="$VENV_DIR/.requirements-installed"
if [ ! -f "$STAMP" ] || [ "$PROJECT_DIR/requirements.txt" -nt "$STAMP" ]; then
    echo "Installing dependencies ..."
    "$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
    "$VENV_DIR/bin/python" -m pip install --quiet -r "$PROJECT_DIR/requirements.txt"
    touch "$STAMP"
fi

cd "$PROJECT_DIR"
# Collect, score and regenerate every derived artefact (CSV, static export, and
# the canonical opportunity set the dashboard queries).
# shellcheck disable=SC2086
"$VENV_DIR/bin/python" -m jobfinder run $RUN_ARGS

if [ "$START_DASHBOARD" -eq 1 ]; then
    if [ "$OPEN_DASHBOARD" -eq 1 ] && [ "${JOBFINDER_NO_OPEN:-0}" != "1" ]; then
        exec "$VENV_DIR/bin/python" -m jobfinder serve --open
    fi
    exec "$VENV_DIR/bin/python" -m jobfinder serve
fi
