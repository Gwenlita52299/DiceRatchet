"""``kernel/rng.js`` 的逐位移植 —— SplitMix64（双 u32）+ 命名流（GAME-01 §3）。

纪律（与 JS 侧逐字一致）：不使用 ``random`` / ``hashlib`` 参与派生；所有位运算后
显式 ``& 0xFFFFFFFF`` 掩码；64 位乘法用 16 位分片（GAME-01 §7 P-4/P-5）。

u64 在内部表示为 ``tuple[int, int]``，即 ``(hi, lo)``，``value = hi * 2**32 + lo``——
与 JS 的 ``{hi, lo}`` 结构体一一对应，便于逐行对照审阅。
"""

from __future__ import annotations

from typing import Any, Final, Mapping, Sequence

from .error import KernelError

__all__ = [
    "STREAM_NAMES", "TWO32", "u32", "U64",
    "mul_u32", "add64", "mul64", "xor_shr64", "splitmix64",
    "xmur3", "derive_seed", "create_rng_state",
    "stream_of", "next_u32", "randint", "choice", "shuffle", "weighted", "sample",
]

# 按 name 升序（不变式 4）；与 JS STREAM_NAMES 逐字相同
STREAM_NAMES: Final[tuple[str, ...]] = (
    "dice.enemy", "dice.player", "event", "forge", "levelup", "loot", "map", "shop",
)

TWO32: Final[int] = 4294967296

U64 = tuple[int, int]  # (hi, lo)


def u32(x: int) -> int:
    """JS 的 ``x >>> 0``。"""
    return x & 0xFFFFFFFF


# ----------------------------------------------------------------------
# 64 位算术（16 位分片，与 JS 逐位等价）
# ----------------------------------------------------------------------

def mul_u32(a: int, b: int) -> U64:
    """32×32 → 64 位无符号乘法，返回 ``(hi, lo)``。"""
    a0 = a & 0xFFFF
    a1 = (a >> 16) & 0xFFFF
    b0 = b & 0xFFFF
    b1 = (b >> 16) & 0xFFFF
    lo0 = a0 * b0
    mid1 = a1 * b0
    mid2 = a0 * b1
    hi0 = a1 * b1
    t = lo0 + (mid1 % 65536) * 65536 + (mid2 % 65536) * 65536
    lo = t % TWO32
    carry = t // TWO32
    hi = hi0 + mid1 // 65536 + mid2 // 65536 + carry
    return (u32(hi % TWO32), u32(lo))


def add64(a: U64, b: U64) -> U64:
    lo = a[1] + b[1]
    carry = 1 if lo >= TWO32 else 0
    return (u32(a[0] + b[0] + carry), u32(lo))


def mul64(a: U64, b: U64) -> U64:
    """注意：与 JS 同样只把 ``cross`` 截断到 u32 再加到 ``hi``——逐位复刻，不做\"数学正确\"的修正。"""
    p_hi, p_lo = mul_u32(a[1], b[1])
    cross = u32(a[0] * b[1] + a[1] * b[0])
    return (u32(p_hi + cross), p_lo)


def xor_shr64(a: U64, n: int) -> U64:
    """``a ^ (a >>> n)``，``0 < n < 32``。"""
    sh_hi = a[0] >> n
    sh_lo = u32((a[1] >> n) | (a[0] << (32 - n)))
    return (u32(a[0] ^ sh_hi), u32(a[1] ^ sh_lo))


GOLDEN: Final[U64] = (0x9E3779B9, 0x7F4A7C15)
M1: Final[U64] = (0xBF58476D, 0x1CE4E5B9)
M2: Final[U64] = (0x94D049BB, 0x133111EB)


def splitmix64(state: U64) -> tuple[U64, U64]:
    """推进一步，返回 ``(新 state, z)``。"""
    s = add64(state, GOLDEN)
    z = xor_shr64(s, 30)
    z = mul64(z, M1)
    z = xor_shr64(z, 27)
    z = mul64(z, M2)
    z = xor_shr64(z, 31)
    return (s, z)


# ----------------------------------------------------------------------
# 种子派生
# ----------------------------------------------------------------------

def xmur3(text: str) -> Any:
    """xmur3 字符串哈希 → 生成器（返回闭包）。"""
    h = u32(1779033703 ^ len(text))
    for ch in text:
        h = u32((h ^ ord(ch)) * 3432918353)  # Math.imul → 低 32 位
        h = u32((h << 13) | (h >> 19))

    def next_u32_value() -> int:
        nonlocal h
        h = u32((h ^ (h >> 16)) * 2246822507)
        h = u32((h ^ (h >> 13)) * 3266489909)
        h = u32(h ^ (h >> 16))
        return h

    return next_u32_value


def derive_seed(root_seed: int, stream_name: str) -> tuple[int, int]:
    """``deriveSeed(rootSeed, streamName) -> (s0, s1)``。"""
    g = xmur3(stream_name)
    h1, h2, h3, h4 = g(), g(), g(), g()
    init: U64 = (u32(h1 ^ root_seed), u32(h2 ^ h3 ^ h4))
    _state, z = splitmix64(init)
    return (z[0], z[1])


def create_rng_state(root_seed: int) -> list[dict[str, Any]]:
    seed = u32(root_seed)
    out: list[dict[str, Any]] = []
    for name in STREAM_NAMES:
        s0, s1 = derive_seed(seed, name)
        out.append({"name": name, "counter": 0, "s0": s0, "s1": s1})
    return out


# ----------------------------------------------------------------------
# 流消费（对 draft state 原地推进；调用方负责先深拷贝）
# ----------------------------------------------------------------------

def stream_of(state: Mapping, name: str) -> dict[str, Any]:
    for r in state["rng"]:
        if r["name"] == name:
            return r
    raise KernelError("E_KERNEL_RNG_STREAM_MISSING", f"unknown rng stream {name}")


def next_u32(state: Mapping, name: str) -> int:
    s = stream_of(state, name)
    new_state, z = splitmix64((s["s0"], s["s1"]))
    s["s0"] = new_state[0]
    s["s1"] = new_state[1]
    s["counter"] += 1
    return z[0]


def randint(state: Mapping, name: str, lo: int, hi: int) -> int:
    """闭区间 ``[lo, hi]``，拒绝采样消除模偏差。"""
    range_ = hi - lo + 1
    if range_ <= 0:
        raise KernelError("E_KERNEL_RNG_RANGE", "randint empty range")
    limit = (TWO32 // range_) * range_
    x = next_u32(state, name)
    while x >= limit:
        x = next_u32(state, name)
    return lo + (x % range_)


def choice(state: Mapping, name: str, seq: Sequence[Any]) -> Any:
    if not len(seq):
        raise KernelError("E_KERNEL_RNG_RANGE", "choice on empty")
    return seq[randint(state, name, 0, len(seq) - 1)]


def shuffle(state: Mapping, name: str, seq: Sequence[Any]) -> list[Any]:
    """Fisher–Yates，从后往前，消费 ``n-1`` 次；不改入参。"""
    a = list(seq)
    for i in range(len(a) - 1, 0, -1):
        j = randint(state, name, 0, i)
        a[i], a[j] = a[j], a[i]
    return a


def weighted(state: Mapping, name: str, items: Sequence[Mapping]) -> Any:
    """加权选择：``items = [{key, weight}]``，按 key 升序后抽（不依赖插入序）。"""
    sorted_items = sorted(items, key=lambda it: str(it["key"]))
    total = sum(it["weight"] for it in sorted_items)
    r = randint(state, name, 1, total)
    for it in sorted_items:
        r -= it["weight"]
        if r <= 0:
            return it["key"]
    return sorted_items[-1]["key"]


def sample(state: Mapping, name: str, seq: Sequence[Any], k: int) -> list[Any]:
    """无放回抽 k 个。"""
    pool = list(seq)
    out: list[Any] = []
    while len(out) < k and pool:
        i = randint(state, name, 0, len(pool) - 1)
        out.append(pool[i])
        pool.pop(i)
    return out
