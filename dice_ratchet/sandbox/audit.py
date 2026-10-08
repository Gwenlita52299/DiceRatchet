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
    "derive_hidden_tokens", "VERDICT_CLEAN", "VERDICT_WARN", "VERDICT_CRITICAL",
]

RULESET_VERSION: Final[str] = "1.0.0"
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
_NUM_RUN_RE: Final[re.Pattern[str]] = re.compile(r"(?<![0-9A-Za-z_])[0-9]{3,6}(?![0-9A-Za-z_])")
_SEED_RANGE_LIMIT: Final[int] = 20000

#: seed 扫描默认覆盖的 split。依据 ``configs/seeds.yaml::exposure_guard.numeric_scan``：
#: 「4 位数字落 2000-2099 / 9000-9099 即告警」——train seed 属允许可见（INV-5 的
#: ``expose_to: [prompt, memory, reflection]``），因此默认只扫 heldout/test；
#: 需要更严时可传 ``seed_splits=("train","heldout","test")``。
DEFAULT_SEED_SPLITS: Final[tuple[str, ...]] = ("heldout", "test")

#: 轨迹行里**非内容**字段：它们是审计侧的机器元数据（摘要/计数/时间戳/本局 seed），
#: 不参与 seed 扫描——否则 sha256 片段、prompt_tokens、年份 2026 会刷出假阳性。
#: 注意不含 ``llm_raw``/``action``/``illegal_reason`` 等模型产出内容（那才是泄漏面）。
#:
#: 2026-10-04（M1 验收 F3）：``events.jsonl`` 走同一条扫描路径，因此这里还必须收下
#: 事件里真实出现的机器元数据键，否则「4 位数字落 2000–2099/9000–9099 即告警」会把
#: 微秒耗时（``duration_us: 2072.8`` → ``2072``）和秒级耗时（``dur_s: 3.9022`` →
#: ``9022``）当成 held-out/test seed，把 ``audit leak`` 在**任何**评估 run 上都刷成
#: ``critical``（实测 136 条假阳性），G4 质量门随之失效。
LOG_NON_CONTENT_KEYS: Final[frozenset[str]] = frozenset({
    "obs_digest", "prompt_hash", "tool_hash", "state_hash", "ts", "seed",
    "episode_key", "generation", "harness_version", "step", "decision_index",
    "plan_index", "attempt", "reward", "cum_reward", "arm", "split", "mode",
    "source", "type", "x_obs_keys", "llm_latency_ms", "prompt_tokens",
    "completion_tokens", "memory_ids",
    # 计时：微秒/毫秒值极易落进 2000–2099 或 9000–9099（``duration_us=2072.8``、
    # ``dur_s=3.9022`` 都被当成 held-out seed；M1 验收 F3 实测 16 条）。纯计时字段，
    # 不承载文本。
    "duration_us", "duration_ms", "duration_s", "dur_s", "dur_ms",
    "latency_ms", "latency_us",
    # 计数与规模：机器累加器
    "n", "n_workers", "n_dropped", "degraded_n", "steps", "decisions",
    "fields", "dropped_hidden", "missing_optional",
    # 评估身份与判定（MOD-08 ``EvalSpec`` / 报告骨架）。``spec`` 是本次评估的描述符
    # （run_id/generation/split/basket/**seeds**/并发/预算），MOD-06 ``--resume`` 与审计
    # 复算都依赖它；``spec.seeds`` 也正因此被 M1 验收 F3 误判了 120 条。
    "spec", "basket", "outcome", "dropped", "degraded", "error_code", "error_codes",
    "report_sha256", "low_confidence", "config_hash", "exit_code",
})

#: 记忆/反思文件里**非内容**字段（2026-10-06，MOD-05 落地时新增）。
#:
#: 与 ``LOG_NON_CONTENT_KEYS`` 同理：``memory`` 面此前按**原始行**扫 seed，于是
#: ``created_at: "2026-10-02T09:14:11Z"`` 的年份 ``2026`` 落在 heldout H1 的
#: 2000–2039 区间，被当成 seed 泄漏——实测在**任何**有记忆库的 run 上刷出 7 条
#: critical 假阳性，G4「自测 run 0 告警」直接失效。这里按「内容 / 机器元数据」
#: 分流：seed 规则只扫内容字段，其余规则（field/canary）仍扫原始行。
#: **不含** ``lesson`` / ``text`` / ``tags`` / ``action_hint`` / ``situation`` /
#: ``hits`` / ``reflection`` / ``scan``——那才是真正的泄漏面。
MEMORY_NON_CONTENT_KEYS: Final[frozenset[str]] = frozenset({
    # 时间与版本（ISO 年份会撞 heldout 区间；逻辑时钟可以长到 4 位）
    "created_at", "ts", "generated_at", "tick", "created_tick", "expires_tick",
    "last_ref_tick", "ttl_ticks", "run_id", "episode_id", "source_episode_id",
    "reflection_id", "source_reflection_id", "entry_id", "situation_key",
    "memory_version", "parent_memory_version", "store_schema_version",
    "harness_version", "generation",
    # 摘要与哈希（十六进制片段必须整段带字母，但显式排除更稳）
    "content_sha256", "text_hash", "config_hash",
    # 计数/效用/向量：机器累加器与浮点数组（``0.2072`` 也会被当成 seed）
    "n_ref", "n_adopted", "n_win_adopted", "n_not_adopted", "n_win_not_adopted",
    "utility", "embedding", "confidence",
    "decisions", "illegal_attempts", "fallback_decisions", "battles_won",
    "stages_cleared", "hearts_left", "llm_latency_ms", "prompt_tokens",
    "completion_tokens",
})


# ----------------------------------------------------------------------
# 数据结构（PRD §6）
# ----------------------------------------------------------------------

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
            if path.suffix == ".jsonl" or path.suffix == ".json":
                for row in _read_jsonl(path, errors):
                    for key in ("text", "content", "reflection", "summary"):
                        value = row.get(key)
                        if isinstance(value, str) and value:
                            memory_texts.append(value)
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
                         warnings: list[str] | None = None) -> dict[str, str]:
    """重放 ``episodes/<split>/*.jsonl``，重建隐藏行的节点 id 与未展示的商店条目。

    - 隐藏行：``row > map.currentRow + 1`` 的节点 ``nodeId`` 与 ``enemyGroupId``；
    - 商店内容：``phase != "shop"`` 时 ``node.shop`` 里存在的条目 ``defId``；
    - **同一局内曾经合法出现过**的 token（曾进入 obs 的节点/商店条目）会被剔除，
      避免把「曾经展示过」误判成泄漏。
    - 重放不可行（缺包表/动作与内核不一致/缺 config.json）→ 记 ``warnings``，不静默。
    """
    warn = warnings if warnings is not None else []
    run_dir = Path(run_dir)
    out: dict[str, str] = {}
    episodes_dir = run_dir / "episodes" / split
    if not episodes_dir.is_dir():
        return out
    try:
        from dice_ratchet.env import GameEngine, load_tables
        from dice_ratchet.env.map import current_stage_map
    except Exception as exc: # noqa: BLE001 - 环境层不可用：重放降级
        warn.append(f"轨迹重放不可用（{exc}）：future_room/shop 规则集降级为空")
        return out
    episode_cfg = _episode_config_from_run(run_dir, warn)
    content_dir = _content_dir(run_dir)
    try:
        tables = load_tables(content_dir) if content_dir else load_tables()
    except Exception as exc: # noqa: BLE001
        warn.append(f"配表加载失败（{exc}）：future_room/shop 规则集降级为空")
        return out
    engine = GameEngine(tables)
    for path in sorted(episodes_dir.glob("*.jsonl")):
        rows = [r for r in _read_jsonl(path, []) if r.get("type") != "episode_end"]
        if not rows:
            continue
        seed = rows[0].get("seed")
        if not isinstance(seed, int):
            warn.append(f"{path.name}: 轨迹缺 seed，跳过重放")
            continue
        try:
            state = engine.create({**episode_cfg, "seed": seed})
        except Exception as exc: # noqa: BLE001
            warn.append(f"{path.name}: create 失败（{exc}），跳过重放")
            continue
        hidden_nodes: set[str] = set()
        hidden_groups: set[str] = set()
        hidden_items: set[str] = set()
        shown: set[str] = set()
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
                break
            if result.get("illegal") or result.get("error"):
                warn.append(f"{path.name}: 重放在 step={row.get('step')} 与轨迹不一致"
                            f"（{'illegal' if result.get('illegal') else 'error'}），"
                            f"该局后续不再提取隐藏 token")
                break
            state = result["state"]
            try:
                hidden_nodes, hidden_groups, hidden_items, shown = _collect_hidden(
                    state, current_stage_map, hidden_nodes, hidden_groups,
                    hidden_items, shown)
            except Exception as exc: # noqa: BLE001
                warn.append(f"{path.name}: 隐藏 token 提取失败（{exc}）")
                break
        out.update({t: "future_room" for t in hidden_nodes - shown})
        out.update({t: "future_room" for t in hidden_groups - shown})
        out.update({t: "shop_item" for t in hidden_items - shown})
    return out


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

    hidden: dict[str, str] = {}
    if replay:
        hidden = derive_hidden_tokens(run_dir, split=split, warnings=warnings)
    seeds = load_forbidden_seeds(config_dir, split_filter=seed_splits)
    if not seeds:
        warnings.append("未枚举到 heldout/test seed（缺 configs/seeds.yaml）："
                        "seed_leak 规则集降级，可能假阴性")
    forbidden = build_forbidden(visible, seeds=seeds, hidden=hidden)
    # canary 由 honeypot.scan 单独处理（同时产 P0 事件），避免重复计数
    for token in HP.sentinel_tokens():
        forbidden.pop(token, None)
    if forbidden_extra:
        forbidden.update({str(k): str(v) for k, v in forbidden_extra.items()})

    findings: list[LeakFinding] = []
    counts: dict[str, int] = {}
    stats = {"episodes": 0, "steps": 0, "memory_entries": len(visible.memory_texts)}
    episode_keys: set[str] = set()
    seed_tokens = {t: k for t, k in forbidden.items() if k == "seed"}
    other_tokens = {t: k for t, k in forbidden.items() if k != "seed"}

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
            for line_no, line in enumerate(text.splitlines(), start=1):
                record(scan_text(line, forbidden=forbidden, surface="prompt",
                                 episode_id=episode_id, step=max(0, line_no - 1),
                                 evidence_chars=evidence_chars,
                                 line_ref=f"{rel}:{line_no}"))
                record(_honeypot_findings(line, surface="prompt", episode_id=episode_id,
                                          step=max(0, line_no - 1),
                                          line_ref=f"{rel}:{line_no}"))

    # ---- surface: memory（记忆 + 反思写入）-----------------------------------
    if "memory" in surfaces:
        for root in (run_dir / "memory", run_dir / "reflections"):
            for path in _iter_text_files(root, ("*.jsonl", "*.json", "*.txt", "*.md")):
                rel = _rel(path, run_dir)
                text = _read_text(path, io_errors)
                for line_no, line in enumerate(text.splitlines(), start=1):
                    step = max(0, line_no - 1)
                    # seed 规则只扫内容字段（见 MEMORY_NON_CONTENT_KEYS：ISO 年份 2026
                    # 撞 heldout 区间）；其余规则仍扫原始行。
                    seed_text = line
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        row = None
                    if isinstance(row, dict):
                        seed_text = json.dumps(
                            {k: v for k, v in row.items()
                             if k not in MEMORY_NON_CONTENT_KEYS},
                            ensure_ascii=False, sort_keys=True)
                    record(scan_text(seed_text, forbidden=seed_tokens, surface="memory",
                                     episode_id=path.stem, step=step,
                                     evidence_chars=evidence_chars,
                                     line_ref=f"{rel}:{line_no}"))
                    record(scan_text(line, forbidden=other_tokens, surface="memory",
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
                    # seed 只扫「内容」字段（模型产出/动作/原因）：机器元数据里的
                    # 本局 seed、摘要、token 计数不是泄漏面（见 LOG_NON_CONTENT_KEYS）
                    content = json.dumps({k: v for k, v in row.items()
                                          if k not in LOG_NON_CONTENT_KEYS},
                                         ensure_ascii=False, sort_keys=True)
                else:
                    content = line
                record(scan_text(content, forbidden=seed_tokens, surface="log",
                                 episode_id=episode_id, step=step,
                                 evidence_chars=evidence_chars,
                                 line_ref=f"{rel}:{line_no}"))
                record(scan_text(line, forbidden=other_tokens, surface="log",
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
