"""T-09-9 fixture：agent 侧直接 import 环境层 → E_IO_GUARD_LAYER。"""

from dice_ratchet.env.engine import GameEngine  # noqa: F401  —— 违反 INV-1


def play():
    return GameEngine
