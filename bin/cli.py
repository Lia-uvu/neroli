#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from cli import run  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(run())
