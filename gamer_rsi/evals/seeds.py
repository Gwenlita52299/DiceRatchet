"""MOD-08 seed 协议与 INV-5 三重护栏（PRD §2/§4/§9/§10，Spec §4.6 B-07 / §4.7）。

**唯一出口**（Spec §4.7 P-14 / B-36）：MOD-06/07 只能经 ``take(split, ...)`` 取 seed，
不得自行按 ``seed % 2`` 之类的规则切分；本模块同时是区间冲突（``E_EVAL_SEED_OVERLAP``）
与 test 锁定（``E_EVAL_TEST_LOCKED``）的唯一判定处。

三类 seed 的默认区间（Spec B-07，``configs/seeds.yaml`` 为准）::

    train    1000–1999，per_generation=30：第 g 代 = 1000+30(g-1) … 1029+30(g-1)
    heldout  2000–2099：H1=2000–2039（代际裁判）/ H2=2040–2079（消融终检）/ H3=2080–2099（应急）
    test     9000–9099：默认锁定，仅 ``test_unlock`` 解锁（每里程碑 ≤1 次）

INV-5 的四道代码级护栏（FR-08-01…06）：

1. ``validate_protocol()``：三类区间两两不相交，否则 ``E_EVAL_SEED_OVERLAP``；
2. ``assert_split_isolation()``：某个 seed 跑在错误 split 上 → 同上；
3. ``guard_prompt_context()``：prompt/memory/reflection 三个出口的**文本**不得含
   heldout/test 的 4 位 seed token（含 ``heldout`` / ``H1`` 等禁用词）→ ``E_EVAL_INV5_LEAK``；
4. ``guard_memory_write()``：``split != "train"`` 的局任何记忆写入与反思落库被拦截。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, Mapping, Sequence

from gamer_rsi.infra.errors import E, InfraError

__all__ = [
    "Split", "BasketId", "Destination", "DESTINATIONS_FORBIDDEN", "SplitRange", "Basket",
    "ExposureGuard", "SeedProtocol", "default_protocol", "load_protocol", "coerce_protocol",
    "validate_protocol", "protocol_hash", "resolve_basket", "take", "train_window",
    "assert_split_isolation", "guard_prompt_context", "guard_memory_write",
    "assert_train_only_write", "test_attestation_path", "test_unlock", "is_test_unlocked",
    "scan_forbidden_tokens", "split_of_seed", "basket_seeds", "DEFAULT_TRAIN_WINDOW",
]

Split = Literal["train", "heldout", "test"]
BasketId = Literal["H1", "H2", "H3", "test_full", "train_window"]
Destination = Literal["prompt", "memory", "reflection"]

#: 三个 agent 可见出口（Spec §8.4 B-19：redaction/护栏只作用于这三个）
DESTINATIONS_FORBIDDEN: Final[tuple[Destination, ...]] = ("prompt", "memory", "reflection")

#: train 滚动窗口默认局数（PRD §8.1）
DEFAULT_TRAIN_WINDOW: Final[int] = 60

#: 文本中 4 位数字 token 的扫描（PRD §9 numeric_scan）
_NUM4_RE: Final[re.Pattern[str]] = re.compile(r"(?<![0-9A-Za-z])(\d{4})(?![0-9A-Za-z])")


# =====================================================================
# 协议数据模型（PRD §9）
# =====================================================================

@dataclass(frozen=True)
class Basket:
    """heldout 的一个固定篮子（成员不得重新抽样，FR-08-03）。"""

    id: str
    start: int
    end: int
    role: str = ""

    def seeds(self) -> list[int]:
        return list(range(self.start, self.end + 1))


@dataclass(frozen=True)
class SplitRange:
    """一个 split 的区间与消费方式。"""

    name: str
    start: int
    end: int
    per_generation: int | None = None
    consume: str = "in_order"
    expose_to: tuple[str, ...] = ()
    role: str = ""
    locked: bool = False
    unlock_policy: str = ""
    baskets: Mapping[str, Basket] = field(default_factory=dict)

    def seeds(self) -> list[int]:
        return list(range(self.start, self.end + 1))


@dataclass(frozen=True)
class ExposureGuard:
    """``configs/seeds.yaml`` 的 ``exposure_guard`` 段（FR-08-05）。"""

    destinations_forbidden: tuple[str, ...] = DESTINATIONS_FORBIDDEN
    numeric_scan: bool = True
    forbidden_tokens: tuple[str, ...] = ("heldout", "H1", "H2", "H3",
                                         "2000-2099", "9000-9099")


@dataclass(frozen=True)
class SeedProtocol:
    """``configs/seeds.yaml`` 的协议部分（PRD §9；加载归 MOD-09，校验归本模块）。"""

    version: int = 1
    hash_algo: str = "sha256"
    splits: Mapping[str, SplitRange] = field(default_factory=dict)
    exposure_guard: ExposureGuard = field(default_factory=ExposureGuard)
    seeds_hash: str | None = None
    unlocked: bool = False

    def split(self, name: str) -> SplitRange:
        if name not in self.splits:
            raise InfraError(E.E_EVAL_SEED_OVERLAP, f"协议里没有 split {name!r}",
                             hint="检查 configs/seeds.yaml 的 splits 段")
        return self.splits[name]


def default_protocol() -> SeedProtocol:
    """PRD §9 / Spec B-07 的默认协议（yaml 缺失或测试用）。"""
    return SeedProtocol(
        version=1,
        hash_algo="sha256",
        splits={
            "train": SplitRange("train", 1000, 1999, per_generation=30,
                                consume="in_order",
                                expose_to=("prompt", "memory", "reflection")),
            "heldout": SplitRange("heldout", 2000, 2099, expose_to=(), baskets={
                "H1": Basket("H1", 2000, 2039, "generation_judge"),
                "H2": Basket("H2", 2040, 2079, "ablation_final"),
                "H3": Basket("H3", 2080, 2099, "reserve"),
            }),
            "test": SplitRange("test", 9000, 9099, role="final_acceptance",
                               locked=True, unlock_policy="single_use_per_milestone"),
        },
        exposure_guard=ExposureGuard(),
        seeds_hash=None,
    )


def coerce_protocol(protocol: Any) -> SeedProtocol:
    """接受 ``SeedProtocol`` / ``infra.config.SeedsProtocol`` / ``Mapping`` / ``None``。"""
    if protocol is None:
        return default_protocol()
    if isinstance(protocol, SeedProtocol):
        return protocol
    if hasattr(protocol, "splits") and hasattr(protocol, "exposure_guard"):
        return _from_infra(protocol)
    if isinstance(protocol, Mapping):
        return _from_mapping(protocol)
    raise InfraError(E.E_EVAL_SEED_OVERLAP,
                     f"无法识别的 seed 协议类型 {type(protocol).__name__}")


def _from_infra(proto: Any) -> SeedProtocol:
    """``infra.config.SeedsProtocol`` → MOD-08 ``SeedProtocol``（保留全部区间）。"""
    splits: dict[str, SplitRange] = {}
    for name, spec in dict(getattr(proto, "splits", {}) or {}).items():
        spec = dict(spec)
        baskets = {
            bid: Basket(bid, int(b["start"]), int(b["end"]), str(b.get("role", "")))
            for bid, b in (spec.get("baskets") or {}).items()
        }
        if "start" in spec and "end" in spec:
            start, end = int(spec["start"]), int(spec["end"])
        elif baskets:
            # heldout 在 yaml 里只声明篮子（Spec B-07）：区间由篮子并集推出
            start = min(b.start for b in baskets.values())
            end = max(b.end for b in baskets.values())
        else:
            start, end = 0, 0
        splits[name] = SplitRange(
            name=name, start=start, end=end,
            per_generation=(int(spec["per_generation"])
                            if spec.get("per_generation") is not None else None),
            consume=str(spec.get("consume", "in_order")),
            expose_to=tuple(spec.get("expose_to") or ()),
            role=str(spec.get("role", "")), locked=bool(spec.get("locked", False)),
            unlock_policy=str(spec.get("unlock_policy", "")), baskets=baskets)
    guard = dict(getattr(proto, "exposure_guard", {}) or {})
    out = SeedProtocol(
        version=int(getattr(proto, "version", 1)),
        hash_algo=str(getattr(proto, "hash_algo", "sha256")),
        splits=splits,
        exposure_guard=ExposureGuard(
            destinations_forbidden=tuple(guard.get("destinations_forbidden")
                                         or DESTINATIONS_FORBIDDEN),
            numeric_scan=bool(guard.get("numeric_scan", True)),
            forbidden_tokens=tuple(guard.get("forbidden_tokens") or ())),
        seeds_hash=getattr(proto, "seeds_hash", None),
        unlocked=bool(getattr(proto, "unlocked", False)),
    )
    return out


def _from_mapping(data: Mapping[str, Any]) -> SeedProtocol:
    class _P: # 轻量适配：复用 _from_infra 的字段语义
        pass
    proto = _P()
    proto.version = data.get("version", 1)
    proto.hash_algo = data.get("hash_algo", "sha256")
    proto.splits = data.get("splits", {})
    proto.exposure_guard = data.get("exposure_guard", {})
    proto.seeds_hash = data.get("seeds_hash")
    proto.unlocked = data.get("unlocked", False)
    return _from_infra(proto)


def load_protocol(config_dir: Path | str = Path("configs")) -> SeedProtocol:
    """从 ``configs/seeds.yaml`` 加载（加载归 MOD-09 ``load_seeds``；失败回退默认）。"""
    try:
        from gamer_rsi.infra.config import load_seeds

        return coerce_protocol(load_seeds(Path(config_dir)))
    except InfraError:
        raise
    except Exception: # 配表不可用不应让测量层崩；回退 PRD 默认协议并显式标注
        proto = default_protocol()
        return proto


# =====================================================================
# 校验（FR-08-01 / FR-08-02 / FR-08-03）
# =====================================================================

def validate_protocol(protocol: SeedProtocol | Any) -> SeedProtocol:
    """校验三类区间两两不相交 + 篮子成员固定（FR-08-01）。

    ``E_EVAL_SEED_OVERLAP``：任何**跨 split** 的区间重叠，或同一 split 内篮子互相重叠。
    """
    proto = coerce_protocol(protocol)
    if proto.hash_algo != "sha256":
        raise InfraError(E.E_IO_CONFIG_INVALID,
                         f"seeds.hash_algo 只支持 sha256，实际 {proto.hash_algo!r}")
    ranges: list[tuple[str, str, int, int]] = []
    for name, split in proto.splits.items():
        if split.start > split.end:
            raise InfraError(E.E_EVAL_SEED_OVERLAP,
                             f"{name} 区间非法：{split.start} > {split.end}")
        ranges.append((name, name, split.start, split.end))
        for bid, basket in (split.baskets or {}).items():
            if basket.start > basket.end:
                raise InfraError(E.E_EVAL_SEED_OVERLAP,
                                 f"{name}/{bid} 篮子区间非法：{basket.start} > {basket.end}")
            if basket.start < split.start or basket.end > split.end:
                raise InfraError(E.E_EVAL_SEED_OVERLAP,
                                 f"{name}/{bid} 篮子 [{basket.start},{basket.end}] "
                                 f"越出 {name} 区间 [{split.start},{split.end}]")
            ranges.append((name, f"{name}/{bid}", basket.start, basket.end))
    for i in range(len(ranges)):
        for j in range(i + 1, len(ranges)):
            a, b = ranges[i], ranges[j]
            same_split = a[0] == b[0]
            # 同 split 的「整段 vs 篮子」天然包含，不算冲突；篮子之间必须互斥
            if same_split and (a[0] == a[1] or b[0] == b[1]):
                continue
            if not (a[3] < b[2] or b[3] < a[2]):
                raise InfraError(
                    E.E_EVAL_SEED_OVERLAP,
                    f"seed 区间重叠：{a[1]}[{a[2]}..{a[3]}] 与 {b[1]}[{b[2]}..{b[3]}]",
                    hint="三类 seed 必须严格隔离（INV-5）；修改 configs/seeds.yaml 后重跑校验")
    train = proto.splits.get("train")
    if train and train.per_generation is not None and train.per_generation <= 0:
        raise InfraError(E.E_IO_CONFIG_INVALID, "train.per_generation 必须 >= 1")
    return proto


def protocol_hash(protocol: SeedProtocol | Any) -> str:
    """协议摘要（``sha256:`` 前缀）——供 ``configs/seeds.yaml::seeds_hash`` 与 CI 校验。"""
    proto = coerce_protocol(protocol)
    payload = {
        "version": proto.version,
        "hash_algo": proto.hash_algo,
        "splits": {
            name: {
                "start": s.start, "end": s.end, "per_generation": s.per_generation,
                "consume": s.consume, "expose_to": list(s.expose_to),
                "role": s.role, "locked": s.locked, "unlock_policy": s.unlock_policy,
                "baskets": {bid: {"start": b.start, "end": b.end, "role": b.role}
                            for bid, b in (s.baskets or {}).items()},
            }
            for name, s in sorted(proto.splits.items())
        },
        "exposure_guard": {
            "destinations_forbidden": list(proto.exposure_guard.destinations_forbidden),
            "numeric_scan": proto.exposure_guard.numeric_scan,
            "forbidden_tokens": list(proto.exposure_guard.forbidden_tokens),
        },
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# =====================================================================
# 取用（FR-08-02 / FR-08-03 / Spec §4.7）
# =====================================================================

def resolve_basket(basket: BasketId | Sequence[int], *, generation: int = 0,
                   protocol: SeedProtocol | Any = None,
                   window: int = DEFAULT_TRAIN_WINDOW) -> tuple[int, ...]:
    """篮子/区间 → 固定 seed 序列（FR-08-03）。

    ``basket`` 可以是 ``H1`` / ``H2`` / ``H3`` / ``test_full`` / ``train_window``，
    也可以是显式 seed 序列（MOD-07 的冒烟集 ``smoke_seeds`` 就用这种写法）。
    """
    proto = coerce_protocol(protocol)
    if not isinstance(basket, str):
        return tuple(int(s) for s in basket)
    if basket == "test_full":
        return tuple(proto.split("test").seeds())
    if basket == "train_window":
        return tuple(train_window(generation, protocol=proto, window=window))
    if basket in ("H1", "H2", "H3"):
        heldout = proto.split("heldout")
        if basket not in heldout.baskets:
            raise InfraError(E.E_EVAL_SEED_OVERLAP,
                             f"heldout 协议里没有篮子 {basket!r}",
                             hint="H1/H2/H3 由 configs/seeds.yaml 固定（FR-08-03）")
        return tuple(heldout.baskets[basket].seeds())
    raise InfraError(E.E_EVAL_SEED_OVERLAP,
                     f"未知篮子 {basket!r}",
                     hint="合法值：H1|H2|H3|test_full|train_window 或显式 seed 序列")


def basket_seeds(basket: BasketId, *, protocol: SeedProtocol | Any = None,
                 generation: int = 0) -> list[int]:
    return list(resolve_basket(basket, generation=generation, protocol=protocol))


def train_window(generation: int, *, protocol: SeedProtocol | Any = None,
                 window: int = DEFAULT_TRAIN_WINDOW) -> list[int]:
    """第 g 代的滚动训练窗口（默认最近 60 局，PRD §8.1）。

    ``generation == 0``（gen-000 基线）没有已消费的训练局 → 空窗口。
    """
    proto = coerce_protocol(protocol)
    train = proto.split("train")
    per_gen = int(train.per_generation or 30)
    consumed = max(0, min(int(generation), (train.end - train.start + 1) // per_gen)) * per_gen
    if consumed <= 0:
        return []
    size = min(int(window), consumed)
    return [train.start + consumed - size + i for i in range(size)]


def take(split: Split, *, generation: int | None = None, basket: BasketId | None = None,
         protocol: SeedProtocol | Any = None,
         window: int = DEFAULT_TRAIN_WINDOW) -> list[int]:
    """seed 唯一出口（Spec §4.7）。

    - ``train`` 必须给 ``generation``：第 g 代 = ``start+per_generation*(g-1) … +per_generation-1``；
      越界 → ``E_EVAL_SEED_EXHAUSTED``（耗尽即停，禁止循环复用）。
    - ``heldout`` 必须给 ``basket``（H1/H2/H3）。
    - ``test`` 默认锁定：未解锁 → ``E_EVAL_TEST_LOCKED``。
    """
    proto = validate_protocol(coerce_protocol(protocol))
    if split == "train":
        if generation is None:
            raise InfraError(E.E_EVAL_SEED_EXHAUSTED,
                             "take('train') 必须给 generation",
                             hint="Spec §4.7：train 按代切块，第 g 代用 1000+30(g-1)…")
        if generation < 1:
            raise InfraError(E.E_EVAL_SEED_EXHAUSTED,
                             f"train 代际从 1 开始，收到 generation={generation}",
                             hint="gen-000 是 v0 基线，不消耗训练 seed")
        train = proto.split("train")
        per_gen = int(train.per_generation or 30)
        start = train.start + per_gen * (generation - 1)
        end = start + per_gen - 1
        if end > train.end:
            raise InfraError(
                E.E_EVAL_SEED_EXHAUSTED,
                f"train 池耗尽：第 {generation} 代需要 {start}..{end}，"
                f"但区间只到 {train.end}",
                hint="停止演化，扩大 train 池或降低 loop.k（PRD §10）")
        return list(range(start, end + 1))
    if split == "heldout":
        if basket is None:
            raise InfraError(E.E_EVAL_SEED_OVERLAP, "take('heldout') 必须给 basket（H1/H2/H3）")
        return list(resolve_basket(basket, generation=generation or 0, protocol=proto))
    if split == "test":
        if not is_test_unlocked(proto):
            raise InfraError(
                E.E_EVAL_TEST_LOCKED,
                "test 池默认锁定：未解锁不得取用",
                hint="用 eval test --unlock-once 解锁并写 test_attestation.json（FR-08-04）")
        return proto.split("test").seeds()
    raise InfraError(E.E_EVAL_SEED_OVERLAP, f"未知 split {split!r}",
                     hint="合法值：train | heldout | test")


def split_of_seed(seed: int, *, protocol: SeedProtocol | Any = None) -> Split | None:
    """某个 seed 属于哪个 split（篮子归其父 split）。"""
    proto = coerce_protocol(protocol)
    for name in ("train", "heldout", "test"):
        split = proto.splits.get(name)
        if split and split.start <= seed <= split.end:
            return name # type: ignore[return-value]
    return None


def assert_split_isolation(split: Split, seeds: Sequence[int],
                           protocol: SeedProtocol | Any = None) -> None:
    """``split`` 与 seed 的归属必须一致（FR-08-01/05，INV-5）。

    任何 seed 落在别的 split 区间 → ``E_EVAL_SEED_OVERLAP``（拒绝启动）。
    """
    proto = validate_protocol(coerce_protocol(protocol))
    for seed in seeds:
        owner = split_of_seed(int(seed), protocol=proto)
        if owner != split:
            raise InfraError(
                E.E_EVAL_SEED_OVERLAP,
                f"seed {seed} 属于 {owner or '未知区间'}，不能以 split={split!r} 运行",
                hint="三类 seed 严格隔离（INV-5）；seed 只能经 evals.seeds.take 取用")
    if split == "test" and not is_test_unlocked(proto):
        raise InfraError(E.E_EVAL_TEST_LOCKED, "test 池锁定中，拒绝执行（FR-08-04）")


# =====================================================================
# INV-5 护栏（FR-08-05 / FR-08-06）
# =====================================================================

def _emit_guard_violation(*, split: str, seed: int, destination: str,
                          matched_token: str, error_code: str) -> None:
    try:
        from gamer_rsi.infra import events

        events.emit("eval.guard_violation", error_code=error_code, split=split, seed=int(seed),
                    destination=destination, matched_token=matched_token)
    except Exception: # 埋点失败不得改变 fail-closed 语义
        pass


def _forbidden_seed_ranges(proto: SeedProtocol) -> list[tuple[str, int, int]]:
    out: list[tuple[str, int, int]] = []
    for name in ("heldout", "test"):
        split = proto.splits.get(name)
        if split:
            out.append((name, split.start, split.end))
    return out


def scan_forbidden_tokens(text: str, *, protocol: SeedProtocol | Any = None) -> list[str]:
    """扫描文本命中：heldout/test 的 4 位 seed token 或禁用词（返回命中的 token 列表）。"""
    proto = coerce_protocol(protocol)
    guard = proto.exposure_guard
    hits: list[str] = []
    if not isinstance(text, str):
        return hits
    for token in guard.forbidden_tokens:
        if not token:
            continue
        pattern = re.compile(r"(?<![0-9A-Za-z_])" + re.escape(token) + r"(?![0-9A-Za-z_])")
        if pattern.search(text):
            hits.append(token)
    if guard.numeric_scan:
        for match in _NUM4_RE.finditer(text):
            value = int(match.group(1))
            for name, start, end in _forbidden_seed_ranges(proto):
                if start <= value <= end:
                    hits.append(match.group(1))
                    break
    # 去重并保持出现顺序
    seen: set[str] = set()
    unique: list[str] = []
    for token in hits:
        if token not in seen:
            seen.add(token)
            unique.append(token)
    return unique


def guard_prompt_context(*, split: Split, seed: int, text: str,
                         destination: Destination,
                         protocol: SeedProtocol | Any = None) -> None:
    """prompt/memory/reflection 三个出口的文本护栏（FR-08-05）。

    命中 heldout/test seed token 或禁用词 → ``E_EVAL_INV5_LEAK``（P0，fail-closed）。
    """
    if destination not in DESTINATIONS_FORBIDDEN:
        raise InfraError(E.E_EVAL_SCHEMA,
                         f"destination 必须是 {DESTINATIONS_FORBIDDEN}，收到 {destination!r}")
    proto = coerce_protocol(protocol)
    hits = scan_forbidden_tokens(text, protocol=proto)
    if hits:
        token = hits[0]
        _emit_guard_violation(split=split, seed=seed, destination=destination,
                              matched_token=token, error_code=str(E.E_EVAL_INV5_LEAK))
        raise InfraError(
            E.E_EVAL_INV5_LEAK,
            f"{destination} 出口文本命中 heldout/test token {hits}（split={split}, seed={seed}）",
            hint="INV-5：heldout/test seed 不得进入任何 agent 可见出口；"
                 "该局记 dropped，整代评估作废并要人工复核")


def guard_memory_write(split: Split, *, seed: int = 0,
                       destination: Destination = "memory") -> None:
    """非 train 局的记忆写入 / 反思落库拦截（FR-08-06，INV-5）。"""
    if split == "train":
        return
    _emit_guard_violation(split=split, seed=seed, destination=destination,
                          matched_token="", error_code=str(E.E_EVAL_INV5_LEAK))
    raise InfraError(
        E.E_EVAL_INV5_LEAK,
        f"split={split!r} 的局禁止写 {destination}（仅 train 局可写记忆）",
        hint="评估局使用 memory.frozen(v) 只读视图；命中即 P0（Spec §8.4 B-19）")


#: 语义别名：调用点更直白
assert_train_only_write = guard_memory_write


# =====================================================================
# test 解锁与存证（FR-08-04）
# =====================================================================

def is_test_unlocked(protocol: SeedProtocol | Any) -> bool:
    proto = coerce_protocol(protocol)
    test = proto.splits.get("test")
    if test is None:
        return False
    return bool(proto.unlocked) or not test.locked


def test_attestation_path(run_dir: Path | str, generation: int) -> Path:
    """``runs/<run_id>/generations/gen-{NNN}/test_attestation.json``（PRD §7）。"""
    return Path(run_dir) / "generations" / f"gen-{int(generation):03d}" / "test_attestation.json"


def test_unlock(*, run_id: str, generation: int, run_dir: Path | str,
                attestation_path: Path | str | None = None, reason: str = "",
                decided_by: Literal["auto", "human"] = "human",
                protocol: SeedProtocol | Any = None,
                test_metrics: Mapping[str, Any] | None = None) -> Path:
    """解锁 test 并落 ``test_attestation.json``（FR-08-04，每里程碑 ≤1 次）。

    结果**不得**进入代际决策路径：存证文件显式写 ``decision_path=false``。
    已存在存证（同一 run + generation）→ 拒绝二次解锁（单次语义）。
    """
    path = Path(attestation_path) if attestation_path else test_attestation_path(run_dir, generation)
    if path.exists():
        raise InfraError(E.E_EVAL_TEST_LOCKED,
                         f"test 已在本里程碑解锁过：{path}",
                         hint="unlock_policy=single_use_per_milestone（FR-08-04）")
    payload: dict[str, Any] = {
        "run_id": run_id,
        "generation": int(generation),
        "unlock_policy": "single_use_per_milestone",
        "decided_by": decided_by,
        "reason": reason,
        "decision_path": False,
        "test_metrics": dict(test_metrics or {}),
        "seeds_hash": protocol_hash(protocol) if protocol is not None else None,
    }
    from gamer_rsi.infra.io import write_json_atomic

    write_json_atomic(path, payload)
    try:
        from gamer_rsi.infra import events

        events.emit("eval.test_unlock", generation=int(generation),
                    attestation_path=str(path), decided_by=decided_by)
    except Exception:
        pass
    return path
