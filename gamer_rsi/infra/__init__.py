"""MOD-09 infra：工程横切层（配置 / 事件 / JSONL / 守卫 / git / CLI / 绘图）。

依赖方向单向（NFR-09-5）：本包不得 import 任何其他 ``gamer_rsi`` 子包，
由 ``guards.scan_layer_imports()`` 静态强制。
"""

from __future__ import annotations
