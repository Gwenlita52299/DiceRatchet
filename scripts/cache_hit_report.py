#!/usr/bin/env python3
"""CLI 包装：``python scripts/cache_hit_report.py [run_id]``。

核心逻辑在 ``dice_ratchet/inference/cache_report.py``（可单测、可被其他脚本 import）。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from dice_ratchet.inference.cache_report import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
