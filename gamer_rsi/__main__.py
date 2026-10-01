"""``python -m gamer_rsi`` 入口（Spec §4.6 命令面）。"""

from __future__ import annotations

import sys

from .infra.cli import main

if __name__ == "__main__":
    sys.exit(main(reexec=True))
