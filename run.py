#!/usr/bin/env python3
import sys

# Fail fast with a clear message instead of a cryptic mid-run crash on old
# interpreters (e.g. a conda base env pinned to 3.7).
if sys.version_info < (3, 9):
    sys.exit(
        f"aptfinder needs Python 3.9+ (you have {sys.version.split()[0]}).\n"
        "Try: python3 --version, or create an env:  conda create -n apt python=3.11"
    )

from aptfinder.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
