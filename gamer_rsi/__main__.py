"""``python -m gamer_rsi`` 入口（Spec §4.6 命令面）。

组合根在 ``gamer_rsi/app.py``：它负责把各模块的命令实现注册进 ``infra.cli``
（NFR-09-5：``infra`` 自身不得 import 其他子包）。
"""

from __future__ import annotations

import sys

from .app import main

if __name__ == "__main__":
    sys.exit(main(reexec=True))
