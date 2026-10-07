#!/bin/bash
# Double-click this file in Finder to launch aptfinder.
# First run sets up a private Python environment (a few minutes); after that it
# just starts the app and opens your browser. Keep the window open while using
# the app — closing it stops the server.

cd "$(dirname "$0")" || exit 1

# aptfinder needs Python 3.9+. Whatever `python3` points at may be too old even
# when a newer Python is installed alongside it, so scan the versioned names
# newest-first and use the first that's 3.9+.
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

PY="$(find_python)"
if [ -z "$PY" ]; then
  echo "aptfinder needs Python 3.9 or newer, but no compatible version was found."
  echo "Install one from https://www.python.org/downloads/ (or 'brew install python'), then try again."
  read -n 1 -s -r -p "Press any key to close."
  exit 1
fi
echo "Using $("$PY" --version 2>&1)"

# Rebuild the environment if it was created with a now-incompatible Python.
if [ -x ".venv/bin/python" ] \
   && ! ./.venv/bin/python -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 9) else 1)' 2>/dev/null; then
  echo "Existing environment uses an unsupported Python — rebuilding it..."
  rm -rf .venv
fi

if [ ! -x ".venv/bin/aptfinder" ]; then
  echo "First-time setup: creating a private Python environment and installing aptfinder..."
  "$PY" -m venv .venv || { echo "Could not create the environment."; read -n 1 -s -r; exit 1; }
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet . || { echo "Install failed."; read -n 1 -s -r; exit 1; }
  echo "Setup done."
fi

echo ""
echo "Starting aptfinder — your browser will open shortly."
echo "Leave this window open while you use the app; close it (or press Ctrl-C) to stop."
echo ""
exec ./.venv/bin/aptfinder web
