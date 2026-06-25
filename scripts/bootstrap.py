"""Local-dev wrapper around `python -m apex.bootstrap`.

In production the Helm `bootstrap` hook Job runs the module form directly from the
image. Locally:  uv run python scripts/bootstrap.py deploy/bootstrap/example.json
"""

import sys

from apex.bootstrap.__main__ import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
