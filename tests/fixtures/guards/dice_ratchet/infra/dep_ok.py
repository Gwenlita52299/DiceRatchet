"""合法的 infra 依赖（只 import 本包内模块）。"""

from dice_ratchet.infra.config import derive_seed


def seed_of(name: str) -> int:
    return derive_seed(name)
