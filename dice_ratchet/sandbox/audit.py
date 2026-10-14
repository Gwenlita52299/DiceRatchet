"""MOD-02 泄漏审计：可见集合重建 + 6 类规则 + 报告（INV-2/3/5，PRD FR-02-16 ~ FR-02-19）。

扫描面（FR-02-18）：``prompt``（LLM 真实入参）、``memory``（记忆/反思写入）、
``log``（轨迹与 events 的 JSONL 全字段）。任一出口命中即告警：canary/seed/未来房间/
商店未展示内容判 ``critical``，纯字段名类判 ``medium``（B-25：只做黑名单扫描 + canary
探针，不做「prompt 里每个数值都必须归因」的正向校验，避免噪声淹没真阳性）。

forbidden 集合 = ``seeds ∪ future_room_ids ∪ shop_items ∪ hidden_field_names ∪ canary``
（PRD §8）。其中 ``future_room_ids``/``shop_items`` 由**轨迹重放**（MOD-01 内核 + 轨迹里的
seed/动作）重建：隐藏行的节点 id、非 shop 阶段存在的商店条目。重放失败不静默——记进
``warnings`` 并把 verdict 抬到至少 ``warn``。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Final, Iterable, Mapping, Sequence

from dice_ratchet.infra.errors import E, InfraError

from . import honeypot as HP
from .projection import obs_field_paths

__all__ = [
    "RULESET_VERSION", "LeakClass", "VisibleSet", "LeakFinding", "AuditResult",
    "SEVERITY", "HIDDEN_FIELD_NAMES", "FULL_STATE_FIELD_NAMES",
    "rebuild_visible_set", "scan_text", "build_forbidden", "register_sensitive",
    "run_audit", "write_report", "audit_to_console", "load_forbidden_seeds",
    "derive_hidden_tokens", "HiddenTokenIndex",
    "VERDICT_CLEAN", "VERDICT_WARN", "VERDICT_CRITICAL",
]

#: 规则集版本。**1.1.0（2026-10-14，F58/F59）**：`future_room_leak` 改**逐局作用域**、
#: seed 数字扫描的前导边界加 `.`（浮点小数部分不算 seed）。历史报告里的 `1.0.0`
#: 因此可辨识：「那一版的 future_room findings 是全局作用域下的产物」。
RULESET_VERSION: Final[str] = "1.1.0"
VERDICT_CLEAN: Final[str] = "clean"
VERDICT_WARN: Final[str] = "warn"
VERDICT_CRITICAL: Final[str] = "critical"
SURFACES: Final[tuple[str, ...]] = ("prompt", "memory", "log")

#: 单类 finding 上限（报告不因单点噪声膨胀；计数仍完整）。
MAX_FINDINGS_PER_CLASS: Final[int] = 200


class LeakClass(str, Enum):
    """6 类泄漏（FR-02-17）。"""

    SEED_LEAK = "seed_leak"
    FUTURE_ROOM_LEAK = "future_room_leak"
    SHOP_CONTENT_LEAK = "shop_content_leak"
    FULL_STATE_FIELD_LEAK = "full_state_field_leak"
    HIDDEN_FIELD_NAME_LEAK = "hidden_field_name_leak"
    CANARY_LEAK = "canary_leak"


#: token_class（build_forbidden 的取值）→ LeakClass。
TOKEN_CLASS_TO_LEAK: Final[Mapping[str, LeakClass]] = {
    "seed": LeakClass.SEED_LEAK,
    "future_room": LeakClass.FUTURE_ROOM_LEAK,
    "shop_item": LeakClass.SHOP_CONTENT_LEAK,
    "full_state": LeakClass.FULL_STATE_FIELD_LEAK,
    "hidden_field": LeakClass.HIDDEN_FIELD_NAME_LEAK,
    "canary": LeakClass.CANARY_LEAK,
}

#: 判定严重度：canary/seed/未来房间/商店内容 → critical；字段名类 → medium（B-25）。
SEVERITY: Final[Mapping[str, str]] = {
    LeakClass.SEED_LEAK.value: "critical",
    LeakClass.FUTURE_ROOM_LEAK.value: "critical",
    LeakClass.SHOP_CONTENT_LEAK.value: "critical",
    LeakClass.CANARY_LEAK.value: "critical",
    LeakClass.FULL_STATE_FIELD_LEAK.value: "medium",
    LeakClass.HIDDEN_FIELD_NAME_LEAK.value: "medium",
}

#: GameState 里存在、但投影**绝不输出**的字段名（FR-02-17 的 hidden_field_name_leak）。
HIDDEN_FIELD_NAMES: Final[tuple[str, ...]] = (
    "drawPile", "discardPile", "enemyGroupId", "deckSchoolCounts",
    "deckConcentrationPermille", "pendingVirtue", "pendingStatuses", "pendingDraw",
)

#: 全量状态/审计专有字段名（FR-02-17 的 full_state_field_leak）。
FULL_STATE_FIELD_NAMES: Final[tuple[str, ...]] = (
    "illegalCount", "consecutiveIllegal", "outcomeReason", "truncationReason",
    "stateVersion", "dieSeq", "shopSeq", "slotSeq", "seenEvents", "configHash",
    "rngDigest", "stateDigest", "rngState", "fullState",
)

#: 数值型 token（seed）用数字串扫描代替逐 token find（NFR-02-2：≤2 s/1000 步）。
#: 边界要求两侧都不是字母数字——hex 摘要里的数字串不算 seed（否则 sha256 会刷屏假阳性）。
#: 数值型 token（seed）的数字串扫描（NFR-02-2：≤2 s/1000 步）。
#: 边界要求两侧都不是字母数字——hex 摘要里的数字串不算 seed（否则 sha256 会刷屏假阳性）。
#:
#: **F59（2026-10-14）——前导边界加 `.`**：紧跟在 `.` 之后的数字段**不算 seed**。
#:
#: 现场（`runs/m3-formal`，`audit leak` 的 4 条 `seed_leak`）：`elapsed_ms: 0.731042`
#: 命中 H2 的 731040–731079、`0.947084 / 0.947042 / 1.947041` 命中 test 的
#: 947000–947099——命中的都是**浮点数的小数部分**，而它们的整数部分是 `0` / `1`。
#: F45-2 把 heldout/test 移到六位端口空间，是为了避开「耗时/预算/年份」这类**整数**字面量；
#: 六位**小数**分数当时没被考虑，于是端口空间重新撞上了另一类机器元数据。
#:
#: 边界方向（**只放宽「跟在点号之后」这一种情形**，其余一字不动）：
#: - `0.731042` ⇒ 不命中（小数部分）；
#: - `"seed": 731042` ⇒ 命中（真泄漏仍然被抓）；
#: - `947042.5` ⇒ **命中**（整数部分就是那个值）：刻意的 fail-closed——整数部分与 seed
#:   相等时宁可报，也不因为「它长得像浮点」而漏掉一个真泄漏；
#: - `sha256:ab12…731042…` ⇒ 不命中（前后环视早已排除 hex 内部）。
_NUM_RUN_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![0-9A-Za-z_.])[0-9]{3,6}(?![0-9A-Za-z_])")
_SEED_RANGE_LIMIT: Final[int] = 20000

#: seed 扫描默认覆盖的 split。依据 ``configs/seeds.yaml::exposure_guard.numeric_scan``：
#: 「3–6 位数字落 731000-731099 / 947000-947099 即告警」（F45-2 起的六位端口空间）——
#: train seed 属允许可见（INV-5 的 ``expose_to: [prompt, memory, reflection]``），
#: 因此默认只扫 heldout/test；需要更严时可传 ``seed_splits=("train","heldout","test")``。
DEFAULT_SEED_SPLITS: Final[tuple[str, ...]] = ("heldout", "test")

#: F45-2（2026-10-11）：原先这里有两张**人工死名单**
#: （``LOG_NON_CONTENT_KEYS`` / ``MEMORY_NON_CONTENT_KEYS``，扩过四次：M1 F3 的 136 条、
#: ``episode_s``/``cpu_s``/``elapsed_s``、F35 的年份 ``2026``、F45b 的 ``max_tokens: 2048``），
#: 用来把「机器元数据」从 seed 扫描里豁免掉。**已删除**，因为 heldout/test 已移到六位端口空间
#: （``configs/seeds.yaml``：731000-731099 / 947000-947099），不再与耗时/预算/日历字面量撞车，
#: 「数字串相等即泄漏」这条规则重新变得**健全** ⇒ 扫描面比带名单时**更严**（不再有字段被跳过）
#: 且代码更简单。实测量化（删除前）：去掉全部豁免重扫 19 个工件 → 12 个键撞上、总命中
#: **118,646** 次，其中 ``ts``（ISO 年份 2026）占 **112,101** 次（94%）；全量归档 1311 个工件
#: （188 MB）用**旧**区间命中 **799,868** 次，用**新**区间命中 **0** 次。
#:
#: 顺带更正一条**错误论据**：名单注释里曾写「64 位 hex 约 3.7% 概率撞 20XX」——实测
#: 20000 个随机 sha256 命中 **0/20000**，真实 run 里 ``request_hash``/``response_hash``/
#: ``prompt_digest`` 各 9200 样本命中 **0**（正则的前后环视把 hex 内部的数字段全排除了）。
#: ``request_hash``/``response_hash``/``prompt_digest`` 三个键正是**按这条错误论据**加进名单的。
#:
#: **仍然保留的豁免只有一条，理由与「数字空间撞车」无关**：框架自己的**播种计划**。
#: 每条 ``episode_end`` 行都带本局的 ``seed`` / ``episode_key``，``eval.start`` 事件带
#: ``spec.seeds``（本次评估的 held-out 篮子）。它们是 run 的**测量元数据**，不是模型可见
#: 内容：``audit leak`` 要抓的是「held-out seed 出现在 agent 可见出口」，而不是「run 记录了
#: 自己用了哪些 seed」。若不豁免，**每个**基线/评估 run 都会因自己的 seed 被判 critical
#: （实测 clean-run 用例直接变 2 条 finding）——那是把审计变成噪声。
#: 豁免**只按这三个键**（不做前后缀通配），并把这三个键的**值里**出现的任何其它 seed
#: 仍然留在扫描面内（例如 ``spec.seeds`` 之外的 ``reason`` 字段写 seed 一定会被抓）。
LOG_STRUCTURAL_KEYS: Final[frozenset[str]] = frozenset({"seed", "episode_key", "spec"})

# ----------------------------------------------------------------------
# 数据结构（PRD §6）
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class HiddenTokenIndex:
    """逐局隐藏 token 索引（F58，2026-10-14）。

    ``by_episode``：``episode_key`` → ``{token: token_class}``，**只对该局的出口有效**。
    ``unscoped``：无法定位到单局的出口（``memory/`` 全局库、``events.jsonl``）用的兜底集合——
    判据是「**任何一局都从未合法展示过**」的 token 仍算泄漏（fail-closed，但无跨局假阳性）。
    ``replayed``：成功重放的局数（报告 ``scanned`` 段用它，便于说明派生面）。
    """

    by_episode: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    unscoped: Mapping[str, str] = field(default_factory=dict)
    replayed: int = 0
    #: 有轨迹但**重放失败/不一致**的局数（那些局只剩 ``unscoped`` 兜底 ⇒ 必须在 warnings 里可见）。
    failed: int = 0
    #: 只有 ``episode_end``（如基线臂的摘要文件）的局数：**没有可重放的步**，不算降级。
    no_trajectory: int = 0

    def for_episode(self, episode_id: str) -> Mapping[str, str]:
        """该局的隐藏 token（没有该局时返回空——调用方再用 ``unscoped`` 兜底）。"""
        return dict(self.by_episode.get(str(episode_id)) or {})


@dataclass(frozen=True)
class VisibleSet:
    """agent 可见信息集合（FR-02-16）。"""

    obs_paths: tuple[str, ...]
    obs_digests: tuple[str, ...]
    memory_texts: tuple[str, ...]
    harness_version: int
    seeds_seen: tuple[int, ...] # 只收 split=="train"（INV-5）

    def to_dict(self) -> dict[str, Any]:
        return {"obs_paths": list(self.obs_paths),
                "obs_digests": list(self.obs_digests),
                "harness_version": self.harness_version,
                "seeds_seen": list(self.seeds_seen),
                "memory_entries": len(self.memory_texts)}


@dataclass(frozen=True)
class LeakFinding:
    """一条泄漏 finding（``surface ∈ {prompt,memory,log}``）。"""

    leak_class: LeakClass
    surface: str
    episode_id: str
    step: int
    token_class: str
    evidence: str
    line_ref: str

    def to_dict(self) -> dict[str, Any]:
        return {"leak_class": self.leak_class.value, "surface": self.surface,
                "episode_id": self.episode_id, "step": self.step,
                "token_class": self.token_class, "evidence": self.evidence,
                "line_ref": self.line_ref}


@dataclass(frozen=True)
class AuditResult:
    """``audit leak`` 的结果（PRD §6 + ``generated_at``/``io_errors``/``warnings``/``machinery_errors``）。

    两个**正交**的结论通道（见 PRD §18.2）：

    - ``verdict``：泄漏结论（clean / warn / critical）→ 退出码 0 / 1 / 2；
    - ``machinery_errors``：审计器**自身**的守卫违例（蜜罐失效等 ``E_ENV_*``）→ 退出码 **4**
      （Spec §8.3「沙箱/守卫违例」，与 ``infra.errors.EXIT_CODE`` 的登记保持一致）。
    """

    run_id: str
    ruleset_version: str
    scope: str = "leak"
    scanned: Mapping[str, int] = field(default_factory=dict)
    findings: tuple[LeakFinding, ...] = ()
    leak_counts: Mapping[str, int] = field(default_factory=dict)
    honeypot: Mapping[str, Any] = field(default_factory=dict)
    verdict: str = VERDICT_CLEAN
    generated_at: str = ""
    visible_set: Mapping[str, Any] = field(default_factory=dict)
    io_errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    machinery_errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "generated_at": self.generated_at,
            "ruleset_version": self.ruleset_version,
            "scope": self.scope,
            "verdict": self.verdict,
            "scanned": dict(self.scanned),
            "visible_set": dict(self.visible_set),
            "findings": [f.to_dict() for f in self.findings],
            "leak_counts": dict(self.leak_counts),
            "honeypot": dict(self.honeypot),
            "io_errors": list(self.io_errors),
            "warnings": list(self.warnings),
            "machinery_errors": list(self.machinery_errors),
        }

    @property
    def exit_code(self) -> int:
        """退出码 0/1/2/4（优先级 4 > 2 > 1 > 0）。

        - ``0`` 无 finding；``1`` 仅 medium；``2`` 含 critical（FR-02-19）——
          即"**有没有泄漏**"这根轴；
        - ``4`` 审计器自身守卫违例（``machinery_errors`` 非空，如蜜罐自测 <100%）——
          即"**审计结论还可不可信**"这根轴（Spec §8.3「沙箱/守卫违例」；
          ``infra.errors.EXIT_CODE[E_ENV_HONEYPOT] == 4``）。

        4 优先于 2：检测器坏了的时候，无论报告写 clean 还是 critical 都不能按原义使用；
        报告仍保留 ``verdict``（FR-02-23/§10「蜜罐失效恒判 critical」），信息不丢。
        """
        if self.machinery_errors:
            return 4
        if self.verdict == VERDICT_CRITICAL:
            return 2
        if self.verdict == VERDICT_WARN:
            return 1
        return 0

    @property
    def exit_reason(self) -> str:
        """退出码的人话解释（CLI 打印用）。"""
        if self.machinery_errors:
            return ("沙箱/守卫违例：" + ",".join(self.machinery_errors)
                    + "（审计器自身失效，verdict 不可按原义采信）")
        return {"clean": "0 finding", "warn": "仅 medium finding",
                "critical": "含 critical finding"}.get(self.verdict, self.verdict)


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------

def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_jsonl(path: Path, io_errors: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        io_errors.append(f"读不到 {path}：{exc}")
        return rows
    for i, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            io_errors.append(f"JSONL 损坏 {path}:{i}：{exc.msg}")
            continue
        if isinstance(obj, dict):
            rows.append(obj)
        else:
            io_errors.append(f"JSONL 行不是对象 {path}:{i}")
    return rows


def _read_json_document(path: Path, io_errors: list[str]) -> Any:
    """整文件读一个 JSON **文档**（F44：与 JSONL 区分开）。

    MOD-06 的 ``reflections/<episode_key>.json`` 是多行 pretty JSON，按 JSONL 逐行解析会
    在第 2 行起报 ``Extra data`` ⇒ ``E_ENV_AUDIT_IO`` ⇒ ``audit leak`` 退出码 2。
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        io_errors.append(f"读不到 JSON {path}：{exc}")
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        io_errors.append(f"JSON 损坏 {path}：{exc.msg}")
        return None


def _content_values(node: Any) -> list[str]:
    """取出一个条目里**会被泄漏的正文**（顶层与一层 ``entry`` 子对象都要看）。

    键集沿用原实现（``text``/``content``/``reflection``/``summary``）；反思记录的正文在
    ``entry.text``，故必须下探一层——否则 F44 修好 IO 后会「解析成功但什么都没扫到」。
    """
    out: list[str] = []
    for target in (node, node.get("entry") if isinstance(node, dict) else None):
        if not isinstance(target, dict):
            continue
        for key in ("text", "content", "reflection", "summary"):
            value = target.get(key)
            if isinstance(value, str) and value:
                out.append(value)
    return out


def _read_text(path: Path, io_errors: list[str]) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        io_errors.append(f"读不到文本 {path}：{exc}")
        return ""


def _iter_text_files(root: Path, patterns: Sequence[str]) -> list[Path]:
    if not root.is_dir():
        return []
    out: list[Path] = []
    for pattern in patterns:
        out.extend(sorted(p for p in root.rglob(pattern) if p.is_file()))
    return sorted(set(out))


# ----------------------------------------------------------------------
# 可见集合重建（FR-02-16）
# ----------------------------------------------------------------------

def rebuild_visible_set(run_dir: Path, *, split: str = "train",
                        io_errors: list[str] | None = None) -> VisibleSet:
    """从 ``episodes/<split>/*.jsonl`` + memory 文本重建 ``VisibleSet``。

    - ``obs_paths``：``OBSERVATION_SCHEMA`` 与轨迹求交。轨迹里可选的 ``x_obs_keys``
      给出该局 agent 实际拿到过的字段路径；缺省时取完整注册表（保守：可见集合越大，
      越不会把合法的 obs 字段误判成泄漏）。
    - ``seeds_seen``：只收 ``split == "train"`` 的 seed（INV-5）。
    """
    run_dir = Path(run_dir)
    errors = io_errors if io_errors is not None else []
    registered = obs_field_paths()
    digests: list[str] = [] # 保序去重：摘要序列本身是审计证据（G3 对拍）
    seen_digests: set[str] = set()
    harness_version = 0
    seeds: set[int] = set()
    declared: list[set[str]] = []
    n_episodes = 0
    steps = 0
    episodes_dir = run_dir / "episodes" / split
    for path in sorted(episodes_dir.glob("*.jsonl")) if episodes_dir.is_dir() else []:
        rows = _read_jsonl(path, errors)
        if not rows:
            continue
        n_episodes += 1
        for row in rows:
            if row.get("type") == "episode_end":
                continue
            steps += 1
            digest = row.get("obs_digest")
            if isinstance(digest, str) and digest and digest not in seen_digests:
                seen_digests.add(digest)
                digests.append(digest)

            hv = row.get("harness_version")
            if isinstance(hv, int):
                harness_version = max(harness_version, hv)
            seed = row.get("seed")
            if isinstance(seed, int) and row.get("split") == "train":
                seeds.add(seed)
            keys = row.get("x_obs_keys")
            if isinstance(keys, list) and keys:
                declared.append({str(k) for k in keys})
    if declared:
        common = set.intersection(*declared) if len(declared) > 1 else declared[0]
        obs_paths = tuple(sorted(common & set(registered)))
    else:
        obs_paths = tuple(registered) if n_episodes else ()

    memory_texts: list[str] = []
    for root in (run_dir / "memory", run_dir / "reflections"):
        for path in _iter_text_files(root, ("*.jsonl", "*.json", "*.txt", "*.md")):
            # F44（2026-10-10 实跑登记）：**按文件形状分别解析**。原先 `.jsonl` 与 `.json`
            # 都走 `_read_jsonl`，而 MOD-06 的 `reflections/<episode_key>.json` 是**多行
            # pretty JSON**（实测 51 行）⇒ 第 2 行起 `Extra data` ⇒ `E_ENV_AUDIT_IO` ⇒
            # `audit leak` 退出码 2。即**任何含反思的 `loop run` 都无法让审计 clean**，
            # 而「`audit leak` clean」正是 §3.2 的判据③。
            if path.suffix == ".jsonl":
                for row in _read_jsonl(path, errors):
                    memory_texts.extend(_content_values(row))
            elif path.suffix == ".json":
                # 单文档 JSON 整文件解析；反思正文嵌在 `entry.text`，故取值要下探一层。
                memory_texts.extend(_content_values(_read_json_document(path, errors)))
            else:
                memory_texts.append(_read_text(path, errors))

    version_file = run_dir.parent.parent / "harness" / "version.json"
    if harness_version == 0 and version_file.is_file():
        try:
            harness_version = int(json.loads(
                version_file.read_text(encoding="utf-8")).get("version", 0))
        except (OSError, ValueError, TypeError):
            pass
    return VisibleSet(obs_paths=obs_paths, obs_digests=tuple(digests),
                      memory_texts=tuple(memory_texts), harness_version=harness_version,
                      seeds_seen=tuple(sorted(seeds)))


# ----------------------------------------------------------------------
# forbidden 集合（PRD §8）
# ----------------------------------------------------------------------

def load_forbidden_seeds(config_dir: Path | None = None, *,
                         split_filter: Sequence[str] = DEFAULT_SEED_SPLITS) -> dict[str, str]:
    """从 ``configs/seeds.yaml`` 展开指定 split 的全部 seed → ``{"seed": token_class}``。"""
    from dice_ratchet.infra.config import load_seeds

    try:
        proto = load_seeds(Path(config_dir) if config_dir else Path("configs"))
    except Exception: # noqa: BLE001 - 缺 seeds.yaml 时降级为空集（不阻断审计）
        return {}
    out: dict[str, str] = {}
    for name, spec in (proto.splits or {}).items():
        if name not in split_filter or not isinstance(spec, Mapping):
            continue
        ranges: list[tuple[int, int]] = []
        if "start" in spec and "end" in spec:
            ranges.append((int(spec["start"]), int(spec["end"])))
        for bspec in (spec.get("baskets") or {}).values():
            if isinstance(bspec, Mapping) and "start" in bspec and "end" in bspec:
                ranges.append((int(bspec["start"]), int(bspec["end"])))
        for start, end in ranges:
            if end - start > _SEED_RANGE_LIMIT: # pragma: no cover - 防御异常配置
                continue
            for seed in range(start, end + 1):
                out[str(seed)] = "seed"
    return out


def build_forbidden(visible: VisibleSet, *, seeds: Mapping[str, str] | None = None,
                    hidden: Mapping[str, str] | None = None) -> dict[str, str]:
    """合并 forbidden 映射：``seeds ∪ future_room ∪ shop_items ∪ hidden_fields ∪ canary``。

    ``seeds`` 里属于本 run ``seeds_seen``（train）的条目被剔除——INV-5 允许训练 seed
    出现在 prompt/memory/reflection；出现**其它** seed 值即为泄漏。
    """
    out: dict[str, str] = {}
    for token, klass in (seeds or load_forbidden_seeds()).items():
        if token.isdigit() and int(token) in set(visible.seeds_seen):
            continue
        out[token] = klass
    for token in HIDDEN_FIELD_NAMES:
        out.setdefault(token, "hidden_field")
    for token in FULL_STATE_FIELD_NAMES:
        out.setdefault(token, "full_state")
    for token in HP.sentinel_tokens():
        out[token] = "canary"
    for token, klass in (hidden or {}).items():
        if len(token) >= 4:
            out.setdefault(token, klass)
    return out


def register_sensitive(redactor: Any, *, run_dir: Path | None = None,
                       config_dir: Path | None = None) -> int:
    """把本模块的敏感值注册进 MOD-09 ``Redactor``（MOD-09 PRD §5，返回注册条数）。

    注册：heldout/test seed、canary 串、``GameState`` 专有键名。反向依赖通过回调注册，
    避免 ``infra`` ↔ ``sandbox`` 循环 import。
    """
    count = 0
    if run_dir is not None:
        visible = rebuild_visible_set(Path(run_dir))
        known = set(visible.seeds_seen)
    else:
        known = set()
    for token, klass in load_forbidden_seeds(config_dir).items():
        if token.isdigit() and int(token) in known:
            continue
        redactor.register(int(token) if token.isdigit() else token, klass)
        count += 1
    for token in HP.sentinel_tokens():
        redactor.register(token, "canary")
        count += 1
    for token in HIDDEN_FIELD_NAMES:
        redactor.register(token, "hidden_field")
        count += 1
    return count


# ----------------------------------------------------------------------
# 扫描（FR-02-17/18/19）
# ----------------------------------------------------------------------

def _evidence(text: str, offset: int, length: int, width: int) -> str:
    lo = max(0, offset - width)
    hi = min(len(text), offset + length + width)
    return text[lo:hi].replace("\n", " ").replace("\r", " ")


def scan_text(text: str, *, forbidden: Mapping[str, str], surface: str,
              episode_id: str, step: int, evidence_chars: int = 12,
              line_ref: str = "") -> list[LeakFinding]:
    """在一个出口文本里找 forbidden token；返回全部 ``LeakFinding``。

    - 数值型 token（seed）用数字串正则定位，其余 token 用 ``str.find`` 逐次定位。
    - ``evidence`` 只保留命中 token 前后各 ``evidence_chars`` 字符（FR-02-19：
      报告自身不得成为泄漏载体）。
    """
    if not isinstance(text, str) or not text:
        return []
    findings: list[LeakFinding] = []
    width = max(0, int(evidence_chars))
    numeric = {t: k for t, k in forbidden.items() if t.isdigit()}
    others = {t: k for t, k in forbidden.items() if not t.isdigit()}

    def add(token: str, klass: str, offset: int) -> None:
        leak = TOKEN_CLASS_TO_LEAK.get(klass)
        if leak is None: # pragma: no cover - build_forbidden 只产出已登记类别
            return
        findings.append(LeakFinding(
            leak_class=leak, surface=surface, episode_id=episode_id, step=step,
            token_class=klass, evidence=_evidence(text, offset, len(token), width),
            line_ref=line_ref))

    if numeric:
        for match in _NUM_RUN_RE.finditer(text):
            token = match.group(0)
            if token in numeric:
                add(token, numeric[token], match.start())
    for token, klass in others.items():
        start = 0
        while True:
            idx = text.find(token, start)
            if idx < 0:
                break
            add(token, klass, idx)
            start = idx + len(token)
    return findings


# ----------------------------------------------------------------------
# 轨迹重放：future_room / shop 内容的 forbidden token（PRD §8）
# ----------------------------------------------------------------------

def derive_hidden_tokens(run_dir: Path, *, split: str = "train",
                         warnings: list[str] | None = None,
                         splits: Sequence[str] | None = None) -> "HiddenTokenIndex":
    """重放 ``episodes/<split>/*.jsonl``，重建**逐局**的隐藏行节点 id 与未展示的商店条目。

    - 隐藏行：``row > map.currentRow + 1`` 的节点 ``nodeId`` 与 ``enemyGroupId``；
    - 商店内容：``phase != "shop"`` 时 ``node.shop`` 里存在的条目 ``defId``；
    - **同一局内曾经合法出现过**的 token（曾进入 obs 的节点/商店条目）会被剔除，
      避免把「曾经展示过」误判成泄漏；
    - 重放不可行（缺包表/动作与内核不一致/缺 config.json）→ 记 ``warnings``，不静默。

    **F58（2026-10-14）——返回值从「一个全局 dict」改为「逐局索引」**：

    现场（`runs/m3-formal`）：`future_room_leak` 报 **5,852 次 / 200 条 finding，全是假阳性**
    （逐局复核 280 局 / 96,583 行 **0 命中**）。根因是**作用域错配**：派生把**整批 train 局**
    的未揭示节点并成一个集合，而扫描遍历 ``episodes/**``（含 heldout）——
    节点 id（``n-03-2-0`` 之类）是**地图模板的结构 id，跨局复用**，
    于是「A 局里还没揭示」被当成「B 局里出现即泄漏」。而 B 局里它是**合法揭示**的（agent 走到了那一行）。

    现在：每局**自己**的隐藏集合只用于**同一局**的出口；无法定位到某一局的出口
    （``memory/`` 全局库、``events.jsonl``）用 ``unscoped``——那条规则是
    「**任何一局都从未合法展示过**的 token 仍算泄漏」，仍然 fail-closed，但不再有跨局假阳性。

    ``splits``：要重放的 split；缺省 ``(split,)``（兼容旧行为）。``run_audit`` 传
    ``episodes/`` 下**全部**有局文件的 split——**派生面必须与扫描面一致**，
    这正是 F58 的另一半。
    """
    warn = warnings if warnings is not None else []
    run_dir = Path(run_dir)
    wanted = tuple(splits) if splits is not None else (str(split),)
    by_episode: dict[str, dict[str, str]] = {}
    ever_shown: set[str] = set()
    klass_of: dict[str, str] = {}
    replayed = 0
    failed = 0
    no_trajectory = 0
    episodes_dir_root = run_dir / "episodes"
    if not episodes_dir_root.is_dir():
        return HiddenTokenIndex()
    # **没有任何可重放的局**时直接返回：不加载配表、不发「缺 config.json」这类
    # 与本次审计无关的告警（修复前是 `episodes/<split>/` 不存在就早退，等价口径）。
    wanted_paths = [p for name in wanted
                    for p in sorted((episodes_dir_root / name).glob("*.jsonl"))]
    if not wanted_paths:
        return HiddenTokenIndex()
    # 配表/内核**懒加载**：只有真的遇到「有可重放的步」的文件才加载。否则一个只含
    # `episode_end` 摘要（或空）的 run 会因为「缺 config.json」被无谓地记一条告警，
    # 而 `_verdict` 会把非空 warnings 抬到 `warn`（修复前的口径是「没有局就早退」，
    # 这里保持等价：没有可重放的步 ⇒ 不加载、不告警）。
    engine: Any = None
    current_stage_map: Any = None
    episode_cfg: dict[str, Any] = {}
    for path in wanted_paths:
        rows = [r for r in _read_jsonl(path, []) if r.get("type") != "episode_end"]
        if not rows:
            # 只有 episode_end 的文件（基线臂摘要）**没有可重放的步**：不是降级，不计 failed。
            no_trajectory += 1
            continue
        if engine is None:
            try:
                from dice_ratchet.env import GameEngine, load_tables
                from dice_ratchet.env.map import current_stage_map as _stage_map
            except Exception as exc: # noqa: BLE001 - 环境层不可用：重放降级
                warn.append(f"轨迹重放不可用（{exc}）：future_room/shop 规则集降级为空")
                return HiddenTokenIndex(no_trajectory=no_trajectory)
            current_stage_map = _stage_map
            episode_cfg = _episode_config_from_run(run_dir, warn)
            content_dir = _content_dir(run_dir)
            try:
                tables = load_tables(content_dir) if content_dir else load_tables()
            except Exception as exc: # noqa: BLE001
                warn.append(f"配表加载失败（{exc}）：future_room/shop 规则集降级为空")
                return HiddenTokenIndex(no_trajectory=no_trajectory)
            engine = GameEngine(tables)
        seed = rows[0].get("seed")
        if not isinstance(seed, int):
            warn.append(f"{path.name}: 轨迹缺 seed，跳过重放")
            failed += 1
            continue
        try:
            state = engine.create({**episode_cfg, "seed": seed})
        except Exception as exc: # noqa: BLE001
            warn.append(f"{path.name}: create 失败（{exc}），跳过重放")
            failed += 1
            continue
        hidden_nodes: set[str] = set()
        hidden_groups: set[str] = set()
        hidden_items: set[str] = set()
        shown: set[str] = set()
        replay_failed = False
        for row in rows:
            if row.get("illegal"):
                continue
            action = row.get("action")
            if not isinstance(action, Mapping):
                continue
            try:
                result = engine.reduce(state, action)
            except Exception as exc: # noqa: BLE001
                warn.append(f"{path.name}: 重放在 step={row.get('step')} 失败（{exc}）")
                replay_failed = True
                break
            if result.get("illegal") or result.get("error"):
                warn.append(f"{path.name}: 重放在 step={row.get('step')} 与轨迹不一致"
                            f"（{'illegal' if result.get('illegal') else 'error'}），"
                            f"该局后续不再提取隐藏 token")
                replay_failed = True
                break
            state = result["state"]
            try:
                hidden_nodes, hidden_groups, hidden_items, shown = _collect_hidden(
                    state, current_stage_map, hidden_nodes, hidden_groups,
                    hidden_items, shown)
            except Exception as exc: # noqa: BLE001
                warn.append(f"{path.name}: 隐藏 token 提取失败（{exc}）")
                replay_failed = True
                break
        episode_key = _episode_key_of(path, rows[0])
        # **同一局内曾经合法出现过**的 token 必须剔除（原实现在这里做 `hidden - shown`）：
        # agent 走到那一行之后，该节点 id 出现在**本局自己的**轨迹里是合法的。
        episode_map: dict[str, str] = {}
        for token in (hidden_nodes | hidden_groups) - shown:
            if len(token) >= _MIN_HIDDEN_TOKEN_LEN:
                episode_map[token] = "future_room"
        for token in hidden_items - shown:
            if len(token) >= _MIN_HIDDEN_TOKEN_LEN:
                episode_map[token] = "shop_item"
        by_episode[episode_key] = episode_map
        if replay_failed:
            failed += 1
        else:
            replayed += 1
        klass_of.update(episode_map)
        ever_shown.update(t for t in shown if len(t) >= _MIN_HIDDEN_TOKEN_LEN)
    unscoped = {t: klass_of[t] for t in sorted(set(klass_of) - ever_shown)}
    return HiddenTokenIndex(by_episode=by_episode, unscoped=unscoped, replayed=replayed,
                            failed=failed, no_trajectory=no_trajectory)


def _episode_splits(run_dir: Path) -> tuple[str, ...]:
    """``episodes/`` 下**有局文件**的 split（字典序）——F58：派生面与扫描面必须一致。"""
    root = Path(run_dir) / "episodes"
    if not root.is_dir():
        return ()
    return tuple(sorted(p.name for p in root.iterdir()
                        if p.is_dir() and any(p.glob("*.jsonl"))))


def _episode_key_of(path: Path, first_row: Mapping[str, Any]) -> str:
    """episode 文件 → ``episode_key``（行里优先，退回文件名；与 ``load_episode_results`` 同口径）。"""
    key = str(first_row.get("episode_key") or "")
    return key or path.stem


#: 进 forbidden 面所需的**最短**隐藏 token 长度（与 ``build_forbidden`` 的过滤同值）。
_MIN_HIDDEN_TOKEN_LEN: Final[int] = 4


def _collect_hidden(state: Mapping[str, Any], current_stage_map: Any,
                    hidden_nodes: set[str], hidden_groups: set[str],
                    hidden_items: set[str], shown: set[str],
                    ) -> tuple[set[str], set[str], set[str], set[str]]:
    """收集一步的隐藏 token 与「已合法展示」的 token。"""
    map_state = state.get("map")
    phase = state.get("phase")
    if isinstance(map_state, Mapping):
        current_row = map_state.get("currentRow", -1)
        current_row = current_row if isinstance(current_row, int) else -1
        stage_map = current_stage_map(state)
        for r, node_row in enumerate(stage_map.get("rows", [])):
            for node in node_row:
                node_id = str(node.get("nodeId", ""))
                group = str(node.get("enemyGroupId") or "")
                if r > current_row + 1:
                    if node_id:
                        hidden_nodes.add(node_id)
                    if group:
                        hidden_groups.add(group)
                else: # 已揭示/下一行：这些 token 是合法可见的
                    if node_id:
                        shown.add(node_id)
                    if group:
                        shown.add(group)
    node = state.get("node")
    if isinstance(node, Mapping):
        shop = node.get("shop")
        if isinstance(shop, Mapping):
            for item in shop.get("items") or []:
                if not isinstance(item, Mapping):
                    continue
                for key in ("defId", "name", "itemId"):
                    token = item.get(key)
                    if not isinstance(token, str) or not token:
                        continue
                    if phase == "shop":
                        shown.add(token)
                    else:
                        hidden_items.add(token)
    return hidden_nodes, hidden_groups, hidden_items, shown


def _content_dir(run_dir: Path) -> Path | None:
    """定位 GAME 配表目录：优先 run 快照里的 ``env.content_dir``，其次仓库内默认路径。

    MOD-01 的 ``GAME_CONTENT_DIR`` 是 **cwd 相对**的，而 ``audit leak`` 允许从任意
    目录执行；这里显式回退到仓库根，避免「从别处审计 → 重放降级 → 假告警」。
    """
    configured = "src/game/content"
    try:
        raw = json.loads((Path(run_dir) / "config.json").read_text(encoding="utf-8"))
        value = ((raw.get("config") or {}).get("env") or {}).get("content_dir")
        if isinstance(value, str) and value:
            configured = value
    except (OSError, json.JSONDecodeError):
        pass
    repo_root = Path(__file__).resolve().parents[2]
    for candidate in (Path(configured), repo_root / configured):
        if candidate.is_dir():
            return candidate
    return None


def _episode_config_from_run(run_dir: Path, warnings: list[str]) -> dict[str, Any]:
    """从 ``runs/<id>/config.json`` 的 ``config.env.episode`` 重建 GAME create 输入。"""
    default = {"difficulty": "hard", "stageCount": 6, "character": "warrior"}
    path = Path(run_dir) / "config.json"
    if not path.is_file():
        warnings.append("缺 config.json：重放使用默认局（hard × 6）")
        return default
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        warnings.append(f"config.json 不可读（{exc}）：重放使用默认局")
        return default
    episode = (((raw.get("config") or {}).get("env") or {}).get("episode") or {})
    out = dict(default)
    if isinstance(episode, Mapping):
        if episode.get("difficulty") in ("easy", "medium", "hard"):
            out["difficulty"] = episode["difficulty"]
        if isinstance(episode.get("stage_count"), int):
            out["stageCount"] = episode["stage_count"]
        if isinstance(episode.get("character"), str):
            out["character"] = episode["character"]
    return out


# ----------------------------------------------------------------------
# run_audit（FR-02-19）
# ----------------------------------------------------------------------

def run_audit(run_dir: Path, *, ruleset: str = RULESET_VERSION, split: str = "train",
              config_dir: Path | None = None, surfaces: Sequence[str] = SURFACES,
              evidence_chars: int = 12, self_test: bool = True,
              replay: bool = True, generated_at: str | None = None,
              forbidden_extra: Mapping[str, str] | None = None,
              seed_splits: Sequence[str] = DEFAULT_SEED_SPLITS) -> AuditResult:
    """审计一个 run：重建可见集合 → 扫描三个出口 → 输出严格符合 schema 的报告。

    ``self_test=True`` 时先跑 ``honeypot.self_test()``（FR-02-23），命中率 <100% 直接判
    ``critical``；``replay=True`` 时用轨迹重放补齐 future_room/shop 规则集。
    """
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        raise InfraError(E.E_ENV_AUDIT_IO, f"run 目录不存在：{run_dir}",
                         hint="检查 --run-id；缺证据 ≠ 无泄漏，审计按 critical 处理")
    io_errors: list[str] = []
    warnings: list[str] = []
    run_id = run_dir.name

    visible = rebuild_visible_set(run_dir, split=split, io_errors=io_errors)
    hp_result = HP.self_test() if self_test else None
    hp_ok = True if hp_result is None else hp_result.ok

    hidden = HiddenTokenIndex()
    if replay:
        # F58：**派生面必须与扫描面一致**——扫描遍历 ``episodes/**``，所以重放也要覆盖
        # ``episodes/`` 下全部有局文件的 split（只重放 train 而扫描 heldout 正是误报的温床）。
        hidden = derive_hidden_tokens(run_dir, split=split, warnings=warnings,
                                      splits=_episode_splits(run_dir))
    seeds = load_forbidden_seeds(config_dir, split_filter=seed_splits)
    if not seeds:
        warnings.append("未枚举到 heldout/test seed（缺 configs/seeds.yaml）："
                        "seed_leak 规则集降级，可能假阴性")
    forbidden = build_forbidden(visible, seeds=seeds, hidden=hidden.unscoped)
    # canary 由 honeypot.scan 单独处理（同时产 P0 事件），避免重复计数
    for token in HP.sentinel_tokens():
        forbidden.pop(token, None)
    if forbidden_extra:
        forbidden.update({str(k): str(v) for k, v in forbidden_extra.items()})

    findings: list[LeakFinding] = []
    counts: dict[str, int] = {}
    stats = {"episodes": 0, "steps": 0, "memory_entries": len(visible.memory_texts)}
    # F58：**重放失败**的局只剩「全局从未合法展示」兜底（比逐局作用域弱）⇒ 必须在
    # warnings 里可见（`_verdict` 会把非空 warnings 抬到 `warn`）。
    # **不**把「只有 episode_end 的摘要文件」（基线臂，实测 80/462）算成降级：它们本来就
    # 没有可重放的步，把它们计入只会让每一次含基线的 run 都无谓地停在 `warn`。
    # 也不塞进 ``scanned``：审计报告 schema 是冻结契约（`additionalProperties: false` + golden）。
    if replay and int(hidden.failed):
        warnings.append(f"隐藏 token 重放失败 {int(hidden.failed)} 局"
                        f"（成功 {int(hidden.replayed)}、无可重放轨迹 "
                        f"{int(hidden.no_trajectory)}）：失败的那些局只用「全局从未展示」兜底")
    episode_keys: set[str] = set()

    # F58：**逐局作用域**。某个出口属于哪一局，就只用那一局的隐藏 token；定位不到
    # （``memory/`` 全局库、``events.jsonl``）用 ``hidden.unscoped`` 兜底。
    # 按 episode_id 缓存：每行都要用，不缓存会把集合合并做成 O(行数 × token 数)。
    _scope_cache: dict[str, dict[str, str]] = {}

    def forbidden_for(episode_id: str) -> dict[str, str]:
        """该出口的 forbidden 映射（基准集在前；逐局隐藏 token **不覆盖**基准集）。"""
        cached = _scope_cache.get(episode_id)
        if cached is not None:
            return cached
        merged = dict(forbidden)
        for token, klass in hidden.for_episode(episode_id).items():
            merged.setdefault(token, klass)
        _scope_cache[episode_id] = merged
        return merged

    baseline_members = forbidden_for("")
    seed_tokens = {t: k for t, k in baseline_members.items() if k == "seed"}
    other_tokens = {t: k for t, k in baseline_members.items() if k != "seed"}

    def record(hits: Iterable[LeakFinding]) -> None:
        for hit in hits:
            counts[hit.leak_class.value] = counts.get(hit.leak_class.value, 0) + 1
            if counts[hit.leak_class.value] <= MAX_FINDINGS_PER_CLASS:
                findings.append(hit)

    # ---- surface: prompt（LLM 真实入参）--------------------------------------
    if "prompt" in surfaces:
        for path in _iter_text_files(run_dir / "prompts",
                                     ("*.txt", "*.md", "*.json", "*.jsonl")):
            episode_id = path.stem
            episode_keys.add(episode_id)
            text = _read_text(path, io_errors)
            rel = _rel(path, run_dir)
            member = forbidden_for(episode_id)
            for line_no, line in enumerate(text.splitlines(), start=1):
                record(scan_text(line, forbidden=member, surface="prompt",
                                 episode_id=episode_id, step=max(0, line_no - 1),
                                 evidence_chars=evidence_chars,
                                 line_ref=f"{rel}:{line_no}"))
                record(_honeypot_findings(line, surface="prompt", episode_id=episode_id,
                                          step=max(0, line_no - 1),
                                          line_ref=f"{rel}:{line_no}"))

    # ---- surface: memory（记忆 + 反思写入）-----------------------------------
    if "memory" in surfaces:
        for root in (run_dir / "memory", run_dir / "reflections"):
            # ``reflections/<episode_key>.json`` 是**该局**的产物 ⇒ 逐局作用域；
            # ``memory/`` 是跨局的全局库 ⇒ 兜底集合（``unscoped``）。
            scoped_dir = root.name == "reflections"
            for path in _iter_text_files(root, ("*.jsonl", "*.json", "*.txt", "*.md")):
                rel = _rel(path, run_dir)
                text = _read_text(path, io_errors)
                member = forbidden_for(path.stem) if scoped_dir else baseline_members
                member_seed_tokens = {t: k for t, k in member.items() if k == "seed"}
                member_other_tokens = {t: k for t, k in member.items() if k != "seed"}
                # F45a（2026-10-10 实跑登记）：`.json` 在这里是**多行 JSON 文档**
                # （MOD-06 的 `reflections/<episode_key>.json` 实测 51 行），逐行
                # `json.loads` 必失败 ⇒ `row=None`。原先靠「投影掉机器元数据键」补救；
                # F45-2 删掉名单后**整份文档**都进扫描面：文档类仍整份解析一次（保证
                # 不因逐行解析失败而漏扫），但**不再丢任何字段**。
                is_document = path.suffix == ".json"
                seed_scanned_doc = False
                for line_no, line in enumerate(text.splitlines(), start=1):
                    step = max(0, line_no - 1)
                    # F45-2：seed 规则扫**原始行/原始文档**（不再有字段豁免）。
                    if is_document:
                        if not seed_scanned_doc:
                            record(scan_text(text, forbidden=member_seed_tokens,
                                             surface="memory",
                                             episode_id=path.stem, step=0,
                                             evidence_chars=evidence_chars, line_ref=rel))
                            seed_scanned_doc = True
                    else:
                        record(scan_text(line, forbidden=member_seed_tokens, surface="memory",
                                         episode_id=path.stem, step=step,
                                         evidence_chars=evidence_chars,
                                         line_ref=f"{rel}:{line_no}"))
                    record(scan_text(line, forbidden=member_other_tokens, surface="memory",
                                     episode_id=path.stem, step=step,
                                     evidence_chars=evidence_chars,
                                     line_ref=f"{rel}:{line_no}"))
                    record(_honeypot_findings(line, surface="memory",
                                              episode_id=path.stem,
                                              step=step,
                                              line_ref=f"{rel}:{line_no}"))

    # ---- surface: log（轨迹 / events 的 JSONL 全字段）------------------------
    if "log" in surfaces:
        for path in _iter_text_files(run_dir / "episodes", ("*.jsonl",)) + \
                _iter_text_files(run_dir, ("events.jsonl",)):
            rel = _rel(path, run_dir)
            text = _read_text(path, io_errors)
            for line_no, line in enumerate(text.splitlines(), start=1):
                if not line.strip():
                    continue
                episode_id = path.stem
                step = line_no - 1
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    io_errors.append(f"JSONL 损坏 {rel}:{line_no}")
                    continue
                if isinstance(row, dict):
                    episode_id = str(row.get("episode_key") or path.stem)
                    if isinstance(row.get("step"), int):
                        step = int(row["step"])
                    if row.get("type") != "episode_end":
                        stats["steps"] += 1
                # F45-2：seed 规则扫**整行**——只去掉框架自己的**播种计划**三键
                # （``LOG_STRUCTURAL_KEYS``：本局 ``seed`` / ``episode_key`` / ``spec.seeds``），
                # 其余字段（含摘要、token 计数、耗时、采样浮点、模型产出）全部在扫描面内。
                # 修复前这里有 136+ 条人工死名单；六位端口空间把它们全部变成不必要。
                # F59（2026-10-14）：耗时/采样**浮点的小数部分**不再被当成 seed
                # （见 ``_NUM_RUN_RE``）；F58：本行的隐藏 token 只在**本局**作用域内生效。
                line_member = forbidden_for(episode_id)
                line_seed_tokens = {t: k for t, k in line_member.items() if k == "seed"}
                line_other_tokens = {t: k for t, k in line_member.items() if k != "seed"}
                if isinstance(row, dict):
                    seed_line = json.dumps(
                        {k: v for k, v in row.items() if k not in LOG_STRUCTURAL_KEYS},
                        ensure_ascii=False, sort_keys=True)
                else:
                    seed_line = line
                record(scan_text(seed_line, forbidden=line_seed_tokens, surface="log",
                                 episode_id=episode_id, step=step,
                                 evidence_chars=evidence_chars,
                                 line_ref=f"{rel}:{line_no}"))
                record(scan_text(line, forbidden=line_other_tokens, surface="log",
                                 episode_id=episode_id, step=step,
                                 evidence_chars=evidence_chars,
                                 line_ref=f"{rel}:{line_no}"))
                record(_honeypot_findings(line, surface="log", episode_id=episode_id,
                                          step=step, line_ref=f"{rel}:{line_no}"))
    if (run_dir / "episodes" / split).is_dir():
        stats["episodes"] = len(list((run_dir / "episodes" / split).glob("*.jsonl")))
    elif episode_keys:
        stats["episodes"] = len(episode_keys)

    # ---- honeypot 统计 -------------------------------------------------------
    triggered = counts.get(LeakClass.CANARY_LEAK.value, 0)
    caught_rate = 1.0 if hp_result is None else hp_result.caught_rate
    honeypot = {
        "injected": len(HP.sentinel_tokens()) if (visible.obs_digests or stats["episodes"]) else 0,
        "triggered": triggered,
        "caught_rate": caught_rate,
    }
    machinery_errors: list[str] = []
    if hp_result is not None and not hp_result.ok:
        # FR-02-23 / §10：蜜罐自测 <100% → E_ENV_HONEYPOT。verdict 恒为 critical（报告留痕），
        # 但退出码走 Spec §8.3 的"沙箱/守卫违例"= 4（与 infra.errors.EXIT_CODE 登记一致）。
        machinery_errors.append(E.E_ENV_HONEYPOT.value)
        warnings.append(f"蜜罐自测未全中：{hp_result.caught}/{hp_result.cases}"
                        f"（missed={list(hp_result.missed)}）")

    verdict = _verdict(counts=counts, io_errors=io_errors, hp_ok=hp_ok,
                       warnings=warnings)
    result = AuditResult(
        run_id=run_id, ruleset_version=ruleset, scope="leak", scanned=stats,
        findings=tuple(findings), leak_counts=dict(sorted(counts.items())),
        honeypot=honeypot, verdict=verdict,
        generated_at=generated_at or _utc_now(),
        visible_set=visible.to_dict(), io_errors=tuple(io_errors),
        warnings=tuple(warnings), machinery_errors=tuple(machinery_errors))
    _emit_audit(result)
    return result


def _verdict(*, counts: Mapping[str, int], io_errors: Sequence[str], hp_ok: bool,
             warnings: Sequence[str]) -> str:
    """
    判定优先级（PRD §6）：
    ``critical`` = canary 泄漏 / 蜜罐失效 / 任一 critical 类命中 / 轨迹缺失损坏；
    ``warn`` = 仅 medium 命中 / 重放降级（缺证据留下告警，但不阻断 CI 的 critical 门槛）。
    """
    if not hp_ok or counts.get(LeakClass.CANARY_LEAK.value):
        return VERDICT_CRITICAL
    if io_errors:
        return VERDICT_CRITICAL
    if any(SEVERITY.get(cls) == "critical" and n > 0 for cls, n in counts.items()):
        return VERDICT_CRITICAL
    if any(n > 0 for n in counts.values()) or warnings:
        return VERDICT_WARN
    return VERDICT_CLEAN


def _honeypot_findings(text: str, *, surface: str, episode_id: str, step: int,
                       line_ref: str) -> list[LeakFinding]:
    """蜜罐扫描（FR-02-22）→ ``canary_leak`` finding；命中同时登记 P0 事件。"""
    out: list[LeakFinding] = []
    for hit in HP.scan(text, surface=surface, episode_id=episode_id, step=step):
        out.append(LeakFinding(leak_class=LeakClass.CANARY_LEAK, surface=surface,
                               episode_id=episode_id, step=step, token_class="canary",
                               evidence=hit.canary, line_ref=line_ref))
        _emit("sandbox.honeypot_hit", sentinel=hit.canary, surface=surface,
              episode_id=episode_id, step=step, offset=hit.offset)
    return out


def _emit(event: str, **fields: Any) -> None:
    try:
        from dice_ratchet.infra import events

        events.emit(event, **fields)
    except Exception: # noqa: BLE001 - 埋点失败不得改变审计结论
        pass


def _emit_audit(result: AuditResult) -> None:
    _emit("sandbox.audit_finding", severity=result.verdict,
          verdict=result.verdict, leak_counts=dict(result.leak_counts),
          ruleset_version=result.ruleset_version, scope=result.scope)
    cases = len(HP.sentinel_tokens()) * len(SURFACES)
    caught_rate = float(result.honeypot.get("caught_rate", 1.0) or 0.0)
    _emit("sandbox.honeypot_selftest", cases=cases,
          caught=int(round(caught_rate * cases)))


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError: # pragma: no cover
        return str(path)


# ----------------------------------------------------------------------
# 落盘与 CLI 出口（FR-02-19）
# ----------------------------------------------------------------------

def write_report(result: AuditResult, out: Path) -> Path:
    """写 ``runs/<run_id>/audit/leak-<scope>.json``：0600 + schema 校验（FR-02-19）。"""
    from dice_ratchet.infra.io import validate

    payload = result.to_dict()
    validate("audit_report", payload)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, out)
    try:
        os.chmod(out, 0o600) # 报告自身也是泄漏载体，收权限
    except OSError: # pragma: no cover - 平台差异
        pass
    return out


def audit_to_console(result: AuditResult) -> int:
    """打印人类可读摘要并返回退出码（0/1/2/4；4 = 沙箱/守卫违例，见 ``exit_code``）。"""
    print(f"audit leak: run={result.run_id} ruleset={result.ruleset_version} "
          f"verdict={result.verdict} exit={result.exit_code}")
    if result.machinery_errors:
        for code in result.machinery_errors:
            print(f"  [{code}] {result.exit_reason}")
        print(f"  （报告仍按 FR-02-23 记 verdict=critical）")
    print(f"  scanned: episodes={result.scanned.get('episodes', 0)} "
          f"steps={result.scanned.get('steps', 0)} "
          f"memory_entries={result.scanned.get('memory_entries', 0)}")
    visible = result.visible_set or {}
    print(f"  visible_set: obs_paths={len(visible.get('obs_paths') or [])} "
          f"obs_digests={len(visible.get('obs_digests') or [])} "
          f"harness_version={visible.get('harness_version', 0)}")
    print(f"  honeypot: {dict(result.honeypot)}")
    if result.leak_counts:
        print("  leak_counts: " + ", ".join(f"{k}={v}" for k, v in result.leak_counts.items()))
    for finding in result.findings[:20]:
        print(f"  - [{finding.leak_class.value}/{SEVERITY.get(finding.leak_class.value)}] "
              f"{finding.surface} {finding.line_ref} step={finding.step} "
              f"evidence={finding.evidence!r}")
    if len(result.findings) > 20:
        print(f"  ... 其余 {len(result.findings) - 20} 条见报告")
    for error in result.io_errors:
        print(f"  [E_ENV_AUDIT_IO] {error}")
    for warning in result.warnings[:10]:
        print(f"  [warn] {warning}")
    if result.verdict == VERDICT_CLEAN:
        print("  ✓ 0 finding")
    return result.exit_code
