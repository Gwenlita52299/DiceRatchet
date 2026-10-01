"""T-09-10 fixture（合法侧）：随机只来自 derive_seed，无 time.time()/uuid4()。"""

from gamer_rsi.infra.config import derive_seed


def pick(seed: int, n: int) -> int:
    return derive_seed("pick", seed, n) % n
