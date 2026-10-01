"""GameRSI 训练链路（Python 侧）。

模块拆分见 ``docs/模块拆分Spec-GameRSI.md``（唯一事实来源）。M0 阶段只交付
MOD-09 infra 骨架：配置、事件、JSONL、守卫、git 封装、CLI。

快速开始::

    python -m gamer_rsi guards check --root gamer_rsi
    python -m gamer_rsi env selftest --seed 1
    python -m gamer_rsi loop run --generations 3 --k 30 --hitl --run-id <id> --dry-run
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
