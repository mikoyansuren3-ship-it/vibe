#!/usr/bin/env python3
"""Standalone launcher — forwards to the `wck` CLI without installing the package.

    python scripts/run.py run --matches 3 --dashboard
    python scripts/run.py doctor
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wc_kalshi.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
