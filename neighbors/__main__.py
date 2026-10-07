"""Enables `python -m neighbors ...` (equivalent to the `neighbors` command)."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
