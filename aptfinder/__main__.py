"""Enables `python -m aptfinder ...` (used by the one-click launchers)."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
