#!/usr/bin/env bash
# Linux/macOS: run  ./run.sh  from a terminal to launch the app.
set -e
cd "$(dirname "$0")"

# aptfinder needs Python 3.9+. The interpreter on your PATH (e.g. an old system
# python3) might be too old even when a newer one IS installed alongside it — so
# instead of trusting whatever `python3` points at, we scan the common versioned
# names newest-first and pick the first one that's 3.9+.
find_python() {
  local c
  for c in python3.13 python3.12 python3.11 python3.10 python3.9 python3 python; do
    if command -v "$c" >/dev/null 2>&1 \
       && "$c" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 9) else 1)' 2>/dev/null; then
      command -v "$c"; return 0
    fi
  done
  return 1
}

PY="$(find_python)" || {
  echo "aptfinder needs Python 3.9 or newer, but no compatible version was found on your PATH."
  echo "Install one from https://www.python.org/downloads/ (or 'brew install python'), then try again."
  exit 1
}
echo "Using $("$PY" --version 2>&1) at $PY"

# If a venv already exists but was built with a now-incompatible interpreter,
# rebuild it rather than failing at runtime.
if [ -x ".venv/bin/python" ] \
   && ! ./.venv/bin/python -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 9) else 1)' 2>/dev/null; then
  echo "Existing environment uses an unsupported Python — rebuilding it..."
  rm -rf .venv
fi

if [ ! -x ".venv/bin/aptfinder" ]; then
  echo "Setting up (first run only)..."
  "$PY" -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet .
fi

exec ./.venv/bin/aptfinder web "$@"
