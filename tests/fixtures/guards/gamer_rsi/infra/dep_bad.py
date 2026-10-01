"""NFR-09-5 fixture：infra 反向 import 其他子包 → E_IO_GUARD_LAYER。"""

from gamer_rsi.env.engine import GameEngine  # noqa: F401 —— 违反 NFR-09-5 单向依赖


def boot():
    return GameEngine
