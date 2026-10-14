"""MOD-07 数据模型与 JSON Schema（PRD §6.1/§6.3/§7；Spec v1.4 §4.5）。

本模块是 evolve 侧**唯一**的类型与 schema 事实来源：

- ``ExpectedEffect``：``{"metric","direction","priority"}``；序列化为
  ``[metric, direction, priority]`` 三元组省 token（FR-07-1）；
- ``Candidate``：候选 diff + 期望效果 + 改动面计数（``candidate_id`` 由
  ``sha256(kind + diff + rationale)[:12]`` 决定，NFR-07-2 确定性）；
- ``EvidenceBundle``：proposer 上下文白名单（FR-07-2）；``__post_init__`` 是**唯一**
  校验入口，``source_tags ⊆ {"memory:train","traj:train","reflect:train","harness:current"}``；
- ``ProposalResult`` / ``ValidationResult`` / ``CandidateReport`` / ``Verdict`` /
  ``GateState``：跨模块返回值（G5 round-trip 的四个 schema 见 ``schemas/*.schema.json``）。

``Candidate`` / ``CandidateReport`` / ``Verdict`` / ``GateState`` / ``HarnessRef`` 五个
dataclass 都提供 ``as_dict()`` / ``from_dict()``，并与 ``schemas/`` 下的 JSON Schema
一一对应（PRD §12 G5）。

确定性（NFR-07-1）：本模块不 import ``random`` / ``time`` / ``uuid``；``created_at``
一类时刻字段由 MOD-09 注入（``--freeze-time`` 冻结），不参与 ``candidate_id``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Final, Iterable, Literal, Mapping, Sequence

from dice_ratchet.infra.errors import E, InfraError
from dice_ratchet.infra.io import read_json, write_json_atomic

from .errors import HoldoutLeakError

__all__ = [
    "CANDIDATE_KINDS", "CANDIDATE_SCHEMA_VERSION", "EFFECT_DIRECTIONS",
    "EFFECT_METRICS", "EVIDENCE_SOURCE_TAGS", "HARNESS_SNAPSHOT_SCHEMA_VERSION",
    "MAIN_METRICS", "CANDIDATE_SCHEMA", "CANDIDATE_REPORT_SCHEMA",
    "EVIDENCE_BUNDLE_SCHEMA", "GATE_STATE_SCHEMA", "HARNESS_REF_SCHEMA",
    "HARNESS_SNAPSHOT_SCHEMA", "VERDICT_SCHEMA",
    "Candidate", "CandidateReport", "EvidenceBundle", "ExpectedEffect", "GateState",
    "HarnessRef", "HarnessSnapshot", "ProposalResult", "VALIDATION_STATUSES",
    "ValidationResult", "Verdict", "canonical_json", "candidate_id_of",
    "count_diff_lines", "diff_digest", "parse_diff_paths", "read_candidate_report",
    "read_gate_state", "validate_candidate_dict", "validate_candidate_report",
    "validate_evidence_bundle", "validate_gate_state", "validate_harness_ref",
    "validate_verdict", "write_candidate_report",
]

# =====================================================================
# 常量（词表）
# =====================================================================

CANDIDATE_KINDS: Final[tuple[str, ...]] = ("prompt_patch", "tool_patch", "config_patch")
EFFECT_DIRECTIONS: Final[tuple[str, ...]] = ("up", "down")
#: C2 的候选指标集（§8.2：方法归 MOD-08 ``compare_paired``）。
EFFECT_METRICS: Final[tuple[str, ...]] = ("win_rate", "mean_stages_cleared",
                                          "mean_hearts_left")
#: 同 ``evals.metrics.MAIN_METRICS`` 的主指标面（本模块只引用，不定义统计）。
MAIN_METRICS: Final[tuple[str, ...]] = ("win_rate", "mean_stages_cleared",
                                        "mean_hearts_left")
#: 证据束条目来源白名单（FR-07-2）。
EVIDENCE_SOURCE_TAGS: Final[frozenset[str]] = frozenset({
    "memory:train", "traj:train", "reflect:train", "harness:current",
})
VALIDATION_STATUSES: Final[tuple[str, ...]] = ("pass", "fail")

CANDIDATE_SCHEMA_VERSION: Final[str] = "evolve-candidate-1.0.0"
#: 1.2.0（D1=A，2026-10-14）：新增 ``HarnessSnapshot.context``——**只读**上下文文件
#: （``harness/dead_ends.md``）的正文。它与 ``contents``（候选**可改**文件的正文）职责
#: 不同：``contents`` 是 diff 的上下文来源，``context`` 只给提案方看、禁止改动。
HARNESS_SNAPSHOT_SCHEMA_VERSION: Final[str] = "evolve-harness-snapshot-1.2.0"

_ID_ALPHABET: Final[str] = "0123456789abcdef"
_RATIONALE_MAX: Final[int] = 300


# =====================================================================
# 序列化助手（确定性）
# =====================================================================

def canonical_json(obj: Any) -> str:
    """规范 JSON 文本（``sort_keys`` + 紧凑分隔符）—— ``candidate_id`` 与去重口径。

    ``default=str`` 保证非 JSON 原生类型（Path / Enum）不炸；浮点按 repr 落文本，
    调用方不应把浮点放进被哈希的结构里。
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def candidate_id_of(kind: str, diff: str, rationale: str) -> str:
    """``candidate_id = sha256(kind + diff + rationale)[:12]``（FR-07-1 / NFR-07-1）。

    三字段用 ``\\x00`` 分隔，避免 ``("ab","c")`` 与 ``("a","bc")`` 撞同一哈希。
    """
    return _sha256_hex("\x00".join((str(kind), str(diff), str(rationale))))[:12]


def _normalize_diff(diff: str) -> str:
    """去重口径（FR-07-5）：行尾空白归一 + 末尾换行归一。

    只做**文本归一**，不重排 hunk —— 重排会把语义不同的 diff 判成同一个。
    """
    lines = [line.rstrip() for line in str(diff).replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def diff_digest(diff: str) -> str:
    """归一化 diff 的 sha256（FR-07-5 去重键）。"""
    return _sha256_hex(_normalize_diff(diff))


# =====================================================================
# ExpectedEffect
# =====================================================================

@dataclass(frozen=True)
class ExpectedEffect:
    """候选的期望效果（FR-07-1）。

    序列化为 ``[metric, direction, priority]`` 三元组（省 token；PRD §6.1）。
    """

    metric: str
    direction: Literal["up", "down"]
    priority: int

    def __post_init__(self) -> None:
        if self.metric not in EFFECT_METRICS:
            raise InfraError(E.E_EVOLVE_PATCH_INVALID,
                             f"expected_effect.metric 非法：{self.metric!r}",
                             hint=f"合法值：{'｜'.join(EFFECT_METRICS)}")
        if self.direction not in EFFECT_DIRECTIONS:
            raise InfraError(E.E_EVOLVE_PATCH_INVALID,
                             f"expected_effect.direction 非法：{self.direction!r}",
                             hint="只允许 up｜down")
        if not isinstance(self.priority, int) or isinstance(self.priority, bool) \
                or not 1 <= int(self.priority) <= 3:
            raise InfraError(E.E_EVOLVE_PATCH_INVALID,
                             f"expected_effect.priority 非法：{self.priority!r}",
                             hint="priority ∈ 1..3（3 为最高）")

    def as_tuple(self) -> list[Any]:
        return [self.metric, self.direction, int(self.priority)]

    def as_dict(self) -> dict[str, Any]:
        return {"metric": self.metric, "direction": self.direction,
                "priority": int(self.priority)}

    @classmethod
    def from_any(cls, raw: Any) -> "ExpectedEffect":
        """接受三元组 / 字典 / 已是本类实例三种形态（LLM 输出规整的单一入口）。"""
        if isinstance(raw, ExpectedEffect):
            return raw
        if isinstance(raw, Mapping):
            return cls(str(raw.get("metric", "")), str(raw.get("direction", "")),
                       int(raw.get("priority", 0) or 0))
        if isinstance(raw, (list, tuple)) and len(raw) == 3:
            return cls(str(raw[0]), str(raw[1]), int(raw[2] or 0))
        raise InfraError(E.E_EVOLVE_PROPOSE_FAILED,
                         f"expected_effect 形态非法：{raw!r}",
                         hint="期望 [metric, direction, priority] 三元组或同名字典")


# =====================================================================
# Candidate
# =====================================================================

@dataclass(frozen=True)
class Candidate:
    """一个候选 harness 改动（FR-07-1；PRD §6.1）。"""

    candidate_id: str
    kind: str
    diff: str
    rationale: str
    expected_effect: ExpectedEffect
    touched_files: tuple[str, ...] = ()
    diff_lines: int = 0
    gen_id: str = ""

    @classmethod
    def create(cls, *, kind: str, diff: str, rationale: str,
               expected_effect: Any, touched_files: Sequence[str] = (),
               diff_lines: int | None = None, gen_id: str = "") -> "Candidate":
        """规范化构造：``candidate_id`` 与 ``diff_lines`` 自动算出（NFR-07-1）。"""
        effect = ExpectedEffect.from_any(expected_effect)
        text = str(rationale)
        files = tuple(str(f) for f in touched_files)
        return cls(candidate_id=candidate_id_of(kind, diff, text), kind=str(kind),
                   diff=str(diff), rationale=text, expected_effect=effect,
                   touched_files=files,
                   diff_lines=count_diff_lines(diff) if diff_lines is None
                   else int(diff_lines),
                   gen_id=str(gen_id))

    def as_dict(self) -> dict[str, Any]:
        """落盘形态（``candidates/{id}.json`` 的 ``candidate`` 键）。"""
        return {"candidate_id": self.candidate_id, "kind": self.kind, "diff": self.diff,
                "rationale": self.rationale,
                "expected_effect": self.expected_effect.as_tuple(),
                "touched_files": list(self.touched_files),
                "diff_lines": int(self.diff_lines), "gen_id": self.gen_id}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Candidate":
        cand = cls(candidate_id=str(raw["candidate_id"]), kind=str(raw["kind"]),
                   diff=str(raw.get("diff", "")), rationale=str(raw.get("rationale", "")),
                   expected_effect=ExpectedEffect.from_any(raw.get("expected_effect")),
                   touched_files=tuple(str(f) for f in (raw.get("touched_files") or ())),
                   diff_lines=int(raw.get("diff_lines", 0) or 0),
                   gen_id=str(raw.get("gen_id", "")))
        return cand


def count_diff_lines(diff: str) -> int:
    """改动行数 = ``+``/``-`` 行数，**排除** ``+++``/``---`` 文件头（FR-07-4）。

    ``\\ No newline at end of file`` 等反斜杠行不计入（它们不是改动）。
    """
    total = 0
    for line in str(diff).replace("\r\n", "\n").split("\n"):
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+") or line.startswith("-"):
            total += 1
    return total


def parse_diff_paths(diff: str) -> tuple[str, ...]:
    """从 unified diff 抽 ``+++ b/<path>`` 目标路径（去 ``a/``/``b/`` 前缀）。

    只认 ``+++`` 一侧：``---`` 可能是 ``/dev/null``（新增文件）。
    """
    paths: list[str] = []
    for line in str(diff).replace("\r\n", "\n").split("\n"):
        if not line.startswith("+++"):
            continue
        raw = line[3:].strip()
        if raw in ("/dev/null", ""):
            continue
        # ``+++ b/harness/prompts/decision.md`` 或 ``+++ b/path\ttimestamp``
        raw = raw.split("\t", 1)[0].strip()
        if raw.startswith(("a/", "b/")):
            raw = raw[2:]
        if raw and raw not in paths:
            paths.append(raw)
    return tuple(paths)


# =====================================================================
# HarnessSnapshot / HarnessRef
# =====================================================================

@dataclass(frozen=True)
class HarnessSnapshot:
    """当前 harness 的只读指纹（``version`` + 各文件 sha256 + 可改文件正文）。

    ``contents``（F38）只带**允许被候选修改**的文件正文（``prompts/*.md``、
    ``config.json``、``tools/*.py``）。原因：候选是 unified diff，hunk 的上下文行**必须
    与目标文件逐字符一致**，而只给 sha256 时模型看不到任何一行原文，产出的 diff 必然
    ``E_EVOLVE_PATCH_INVALID``（实测：``hunk 上下文不匹配（decision.md 第 1 行）``）。

    ``context``（D1=A，2026-10-14）只带**只读**上下文文件的正文（``harness/dead_ends.md``）：
    提案方必须读到「哪些改动已经被否决过」，但**不得**改动它们。
    """

    version: int
    files: tuple[tuple[str, str], ...] = ()
    artifact_hash: str = ""
    contents: tuple[tuple[str, str], ...] = ()
    #: D1=A（2026-10-14）：**只读**上下文文件的正文（``harness/dead_ends.md``）。
    #: 渲染进提案 prompt 时必须与 ``contents`` **分开标注**——模型的 diff 只能落在
    #: ``contents`` 那一组上，改 ``context`` 里的文件会被 ``forbidden_path`` 直接拒。
    context: tuple[tuple[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"schema_version": HARNESS_SNAPSHOT_SCHEMA_VERSION,
                "version": int(self.version), "artifact_hash": self.artifact_hash,
                "files": [[str(p), str(h)] for p, h in self.files],
                "contents": [[str(p), str(t)] for p, t in self.contents],
                "context": [[str(p), str(t)] for p, t in self.context]}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "HarnessSnapshot":
        return cls(version=int(raw.get("version", 0) or 0),
                   files=tuple((str(p), str(h)) for p, h in (raw.get("files") or ())),
                   artifact_hash=str(raw.get("artifact_hash", "")),
                   contents=tuple((str(p), str(t))
                                  for p, t in (raw.get("contents") or ())),
                   context=tuple((str(p), str(t))
                                 for p, t in (raw.get("context") or ())))


@dataclass(frozen=True)
class HarnessRef:
    """一个 harness 版本的引用（PRD §6.4 的 ``version.json`` 面）。"""

    version: int
    dir: str
    parent: int | None = None
    git_commit: str = ""
    created_at: str = ""
    reason: str = ""
    accepted_by: str = "auto"
    artifact_hash: str = ""
    base_model: str | None = None
    diff_path: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"version": int(self.version), "parent": self.parent, "dir": self.dir,
                "git_commit": self.git_commit, "created_at": self.created_at,
                "reason": self.reason, "accepted_by": self.accepted_by,
                "artifact_hash": self.artifact_hash, "base_model": self.base_model,
                "diff_path": self.diff_path}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "HarnessRef":
        parent = raw.get("parent", None)
        return cls(version=int(raw["version"]), dir=str(raw.get("dir", "")),
                   parent=None if parent is None else int(parent),
                   git_commit=str(raw.get("git_commit", "")),
                   created_at=str(raw.get("created_at", "")),
                   reason=str(raw.get("reason", "")),
                   accepted_by=str(raw.get("accepted_by", "auto")),
                   artifact_hash=str(raw.get("artifact_hash", "")),
                   base_model=raw.get("base_model", None),
                   diff_path=str(raw.get("diff_path", "")))


# =====================================================================
# EvidenceBundle（FR-07-2/3）
# =====================================================================

@dataclass(frozen=True)
class EvidenceBundle:
    """proposer 的**唯一**上下文（FR-07-2 / INV-3）。

    ``__post_init__`` 校验 ``source_tags ⊆ EVIDENCE_SOURCE_TAGS``，并在构造期就把
    ``split`` 归一为 ``train``；heldout 泄漏的文本扫描由
    ``proposer.assert_no_holdout_leak()`` 完成（需要 cfg 里的 seed 注册表）。
    """

    bundle_id: str
    memory_items: tuple[dict[str, Any], ...] = ()
    trajectory_summaries: tuple[dict[str, Any], ...] = ()
    reflections: tuple[dict[str, Any], ...] = ()
    harness_snapshot: HarnessSnapshot = field(
        default_factory=lambda: HarnessSnapshot(version=0))
    source_tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        unknown = sorted(set(self.source_tags) - EVIDENCE_SOURCE_TAGS)
        if unknown:
            raise HoldoutLeakError(
                f"证据束 source_tags 含白名单外标签：{unknown}",
                hint=f"合法值：{sorted(EVIDENCE_SOURCE_TAGS)}（FR-07-2 / INV-3）")

    @property
    def bundle_sha256(self) -> str:
        """证据束内容指纹（进 proposer prompt 的 ``bundle_id`` 溯源）。"""
        payload = canonical_json({
            "memory": [dict(m) for m in self.memory_items],
            "traj": [dict(t) for t in self.trajectory_summaries],
            "reflect": [dict(r) for r in self.reflections],
            "harness": self.harness_snapshot.as_dict(),
            "tags": list(self.source_tags),
        })
        return _sha256_hex(payload)

    def as_dict(self) -> dict[str, Any]:
        return {"bundle_id": self.bundle_id,
                "bundle_sha256": self.bundle_sha256,
                "memory_items": [dict(m) for m in self.memory_items],
                "trajectory_summaries": [dict(t) for t in self.trajectory_summaries],
                "reflections": [dict(r) for r in self.reflections],
                "harness_snapshot": self.harness_snapshot.as_dict(),
                "source_tags": list(self.source_tags)}


# =====================================================================
# ProposalResult / ValidationResult
# =====================================================================

@dataclass(frozen=True)
class ProposalResult:
    """``propose()`` 的返回（FR-07-1）。"""

    candidates: tuple[Candidate, ...] = ()
    error_code: str | None = None
    n_generated: int = 0
    n_deduped: int = 0
    prompt_tokens: int = 0
    latency_ms: int = 0
    reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.error_code and bool(self.candidates)

    def as_dict(self) -> dict[str, Any]:
        return {"candidates": [c.as_dict() for c in self.candidates],
                "error_code": self.error_code, "n_generated": int(self.n_generated),
                "n_deduped": int(self.n_deduped), "prompt_tokens": int(self.prompt_tokens),
                "latency_ms": int(self.latency_ms), "reason": self.reason}


@dataclass(frozen=True)
class ValidationResult:
    """``validate_candidate()`` 的返回（FR-07-4）。"""

    status: str = "pass"
    reason: str = ""
    error_code: str | None = None
    violations: tuple[str, ...] = ()
    diff_lines: int = 0
    touched_files: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == "pass"

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "reason": self.reason,
                "error_code": self.error_code, "violations": list(self.violations),
                "diff_lines": int(self.diff_lines),
                "touched_files": list(self.touched_files)}

    @classmethod
    def fail(cls, reason: str, *, error_code: str | None = None,
             violations: Iterable[str] = (), diff_lines: int = 0,
             touched_files: Sequence[str] = ()) -> "ValidationResult":
        return cls(status="fail", reason=str(reason),
                   error_code=error_code or str(E.E_EVOLVE_PATCH_INVALID),
                   violations=tuple(str(v) for v in violations),
                   diff_lines=int(diff_lines),
                   touched_files=tuple(str(f) for f in touched_files))

    @classmethod
    def passed(cls, *, diff_lines: int = 0,
               touched_files: Sequence[str] = ()) -> "ValidationResult":
        return cls(status="pass", diff_lines=int(diff_lines),
                   touched_files=tuple(str(f) for f in touched_files))


# =====================================================================
# CandidateReport（candidates/{id}.json 的 report 键 + 顶层）
# =====================================================================

@dataclass(frozen=True)
class CandidateReport:
    """单个候选的评估记录（FR-07-12；PRD §7）。

    ``smoke`` / ``heldout`` / ``paired`` / ``diversity`` 段全部来自 MOD-08；
    本模块**不做任何统计运算**（FR-07-7）。
    """

    candidate: Candidate
    smoke: Mapping[str, Any] | None = None
    heldout: Mapping[str, Any] | None = None
    paired: Mapping[str, Any] | None = None
    train: Mapping[str, Any] | None = None
    diversity: Mapping[str, Any] | None = None
    verdict: "Verdict | None" = None
    elapsed_s: float = 0.0
    error_code: str | None = None
    reason: str = ""

    @property
    def candidate_id(self) -> str:
        return self.candidate.candidate_id

    def as_dict(self) -> dict[str, Any]:
        return {"schema_version": CANDIDATE_SCHEMA_VERSION,
                "candidate_id": self.candidate.candidate_id,
                "kind": self.candidate.kind,
                "diff": self.candidate.diff,
                "rationale": self.candidate.rationale,
                "expected_effect": self.candidate.expected_effect.as_tuple(),
                "touched_files": list(self.candidate.touched_files),
                "diff_lines": int(self.candidate.diff_lines),
                "gen_id": self.candidate.gen_id,
                "report": {"smoke": _plain(self.smoke), "heldout": _plain(self.heldout),
                           "paired": _plain(self.paired), "train": _plain(self.train),
                           "diversity": _plain(self.diversity),
                           "elapsed_s": round(float(self.elapsed_s), 4),
                           "error_code": self.error_code},
                "decision": (self.verdict.decision if self.verdict else None),
                "decided_by": (self.verdict.decided_by if self.verdict else None),
                "reason": self.reason or (self.verdict.reason if self.verdict else ""),
                "error_code": self.error_code,
                "verdict": (self.verdict.as_dict() if self.verdict else None)}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CandidateReport":
        report = dict(raw.get("report") or {})
        verdict_raw = raw.get("verdict")
        return cls(candidate=Candidate.from_dict(raw),
                   smoke=report.get("smoke"), heldout=report.get("heldout"),
                   paired=report.get("paired"), train=report.get("train"),
                   diversity=report.get("diversity"),
                   verdict=(Verdict.from_dict(verdict_raw)
                            if isinstance(verdict_raw, Mapping) else None),
                   elapsed_s=float(report.get("elapsed_s", 0.0) or 0.0),
                   error_code=raw.get("error_code") or report.get("error_code"),
                   reason=str(raw.get("reason", "")))


def _plain(value: Any) -> Any:
    """把 dataclass / tuple 递归转成可 JSON 化的普通结构（保持键序稳定）。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    as_dict = getattr(value, "as_dict", None)
    if callable(as_dict):
        return _plain(as_dict())
    return str(value)


# =====================================================================
# Verdict（判定表结果）
# =====================================================================

@dataclass(frozen=True)
class Verdict:
    """判定表结果（§8.2 的六项条件 + ``condition_trace``）。"""

    decision: str = "reject"
    decided_by: str = "auto"
    reason: str = ""
    error_code: str | None = None
    condition_trace: tuple[dict[str, Any], ...] = ()
    candidate_id: str = ""
    generation: int = 0

    @property
    def accepted(self) -> bool:
        return self.decision == "accept"

    def as_dict(self) -> dict[str, Any]:
        return {"decision": self.decision, "decided_by": self.decided_by,
                "reason": self.reason, "error_code": self.error_code,
                "condition_trace": [dict(c) for c in self.condition_trace],
                "candidate_id": self.candidate_id, "generation": int(self.generation)}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Verdict":
        return cls(decision=str(raw.get("decision", "reject")),
                   decided_by=str(raw.get("decided_by", "auto")),
                   reason=str(raw.get("reason", "")),
                   error_code=raw.get("error_code"),
                   condition_trace=tuple(dict(c) for c in
                                         (raw.get("condition_trace") or ())),
                   candidate_id=str(raw.get("candidate_id", "")),
                   generation=int(raw.get("generation", 0) or 0))


# =====================================================================
# GateState（HITL）
# =====================================================================

@dataclass(frozen=True)
class GateState:
    """``gen-{N}/gate.json``（FR-07-11；PRD §7）。"""

    generation: int
    status: str = "pending"
    harness_version: int = 0
    parent_version: int = 0
    diff_path: str = ""
    report_path: str = ""
    expires_at: str = ""
    created_at: str = ""
    resolved_by: str = ""
    reason: str = ""
    candidate_id: str = ""
    error_code: str | None = None

    @property
    def pending(self) -> bool:
        return self.status == "pending"

    @property
    def resolved(self) -> bool:
        return self.status in ("approved", "rejected", "timeout")

    def as_dict(self) -> dict[str, Any]:
        return {"generation": int(self.generation), "status": self.status,
                "harness_version": int(self.harness_version),
                "parent_version": int(self.parent_version),
                "diff_path": self.diff_path, "report_path": self.report_path,
                "expires_at": self.expires_at, "created_at": self.created_at,
                "resolved_by": self.resolved_by, "reason": self.reason,
                "candidate_id": self.candidate_id, "error_code": self.error_code}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "GateState":
        return cls(generation=int(raw.get("generation", 0) or 0),
                   status=str(raw.get("status", "pending")),
                   harness_version=int(raw.get("harness_version", 0) or 0),
                   parent_version=int(raw.get("parent_version", 0) or 0),
                   diff_path=str(raw.get("diff_path", "")),
                   report_path=str(raw.get("report_path", "")),
                   expires_at=str(raw.get("expires_at", "")),
                   created_at=str(raw.get("created_at", "")),
                   resolved_by=str(raw.get("resolved_by", "")),
                   reason=str(raw.get("reason", "")),
                   candidate_id=str(raw.get("candidate_id", "")),
                   error_code=raw.get("error_code"))


# =====================================================================
# JSON Schema（draft 2020-12）
# =====================================================================

HARNESS_SNAPSHOT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "version", "artifact_hash", "files"],
    "properties": {
        "schema_version": {"type": "string"},
        "version": {"type": "integer", "minimum": 0},
        "artifact_hash": {"type": "string"},
        "files": {"type": "array",
                  "items": {"type": "array", "minItems": 2, "maxItems": 2,
                            "items": {"type": "string"}}},
        # F38：允许被候选修改的文件的正文（[相对路径, 文本]）。
        "contents": {"type": "array",
                     "items": {"type": "array", "minItems": 2, "maxItems": 2,
                               "items": {"type": "string"}}},
    },
}

CANDIDATE_SCHEMA: Final[dict[str, Any]] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://diceratchet.local/schemas/evolve_candidate.schema.json",
    "title": "evolve_candidate",
    "description": "MOD-07 Candidate（PRD §6.1）。落盘于 runs/<run_id>/generations/"
                   "gen-{NNN}/candidates/{candidate_id}.json 的 candidate 段。",
    "type": "object",
    "additionalProperties": False,
    "required": ["candidate_id", "kind", "diff", "rationale", "expected_effect",
                 "touched_files", "diff_lines", "gen_id"],
    "properties": {
        "candidate_id": {"type": "string", "minLength": 12, "maxLength": 12,
                         "pattern": "^[0-9a-f]{12}$"},
        "kind": {"enum": list(CANDIDATE_KINDS)},
        "diff": {"type": "string"},
        "rationale": {"type": "string", "maxLength": _RATIONALE_MAX},
        # 序列化形态是 [metric, direction, priority] 三元组（PRD §6.1 省 token）。
        # 用 prefixItems（draft 2020-12）而不是数组形态的 items：后者在
        # ``referencing`` 的 subresource 解析里会被当成 subresource 而炸。
        "expected_effect": {
            "type": "array", "minItems": 3, "maxItems": 3,
            "prefixItems": [{"enum": list(EFFECT_METRICS)},
                            {"enum": list(EFFECT_DIRECTIONS)},
                            {"type": "integer", "minimum": 1, "maximum": 3}],
        },
        "touched_files": {"type": "array", "items": {"type": "string"}},
        "diff_lines": {"type": "integer", "minimum": 0},
        "gen_id": {"type": "string"},
    },
}

EVIDENCE_BUNDLE_SCHEMA: Final[dict[str, Any]] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://diceratchet.local/schemas/evolve_evidence_bundle.schema.json",
    "title": "evolve_evidence_bundle",
    "description": "MOD-07 EvidenceBundle（PRD FR-07-2；INV-3 白名单）。",
    "type": "object",
    "additionalProperties": False,
    "required": ["bundle_id", "bundle_sha256", "memory_items", "trajectory_summaries",
                 "reflections", "harness_snapshot", "source_tags"],
    "properties": {
        "bundle_id": {"type": "string"},
        "bundle_sha256": {"type": "string"},
        "memory_items": {"type": "array", "items": {"type": "object"}},
        "trajectory_summaries": {"type": "array", "items": {"type": "object"}},
        "reflections": {"type": "array", "items": {"type": "object"}},
        "harness_snapshot": HARNESS_SNAPSHOT_SCHEMA,
        "source_tags": {"type": "array",
                        "items": {"enum": sorted(EVIDENCE_SOURCE_TAGS)}},
    },
}

_VERDICT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "decided_by", "reason", "condition_trace"],
    "properties": {
        "decision": {"enum": ["accept", "reject", "pending", "rollback"]},
        "decided_by": {"enum": ["auto", "human"]},
        "reason": {"type": "string"},
        "error_code": {"type": ["string", "null"]},
        "condition_trace": {"type": "array", "items": {"type": "object"}},
        "candidate_id": {"type": "string"},
        "generation": {"type": "integer", "minimum": 0},
    },
}

VERDICT_SCHEMA: Final[dict[str, Any]] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://diceratchet.local/schemas/evolve_verdict.schema.json",
    "title": "evolve_verdict",
    "description": "MOD-07 Verdict（PRD §8.2 判定表结果）。",
    **_VERDICT_SCHEMA,
}

HARNESS_REF_SCHEMA: Final[dict[str, Any]] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://diceratchet.local/schemas/evolve_harness_ref.schema.json",
    "title": "evolve_harness_ref",
    "description": "MOD-07 HarnessRef（PRD §6.4 version.json；base_model 预留给 MOD-10）。",
    "type": "object",
    "additionalProperties": False,
    "required": ["version", "parent", "dir", "git_commit", "created_at", "reason",
                 "accepted_by", "artifact_hash", "base_model", "diff_path"],
    "properties": {
        "version": {"type": "integer", "minimum": 0},
        "parent": {"type": ["integer", "null"], "minimum": 0},
        "dir": {"type": "string"},
        "git_commit": {"type": "string"},
        "created_at": {"type": "string"},
        "reason": {"type": "string"},
        "accepted_by": {"type": "string"},
        "artifact_hash": {"type": "string"},
        "base_model": {"type": ["string", "null"]},
        "diff_path": {"type": "string"},
    },
}

GATE_STATE_SCHEMA: Final[dict[str, Any]] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://diceratchet.local/schemas/evolve_gate_state.schema.json",
    "title": "evolve_gate_state",
    "description": "MOD-07 GateState（PRD FR-07-11；gen-{N}/gate.json）。",
    "type": "object",
    "additionalProperties": False,
    "required": ["generation", "status", "harness_version", "parent_version",
                 "diff_path", "report_path", "expires_at"],
    "properties": {
        "generation": {"type": "integer", "minimum": 1},
        "status": {"enum": ["pending", "approved", "rejected", "timeout"]},
        "harness_version": {"type": "integer", "minimum": 0},
        "parent_version": {"type": "integer", "minimum": 0},
        "diff_path": {"type": "string"},
        "report_path": {"type": "string"},
        "expires_at": {"type": "string"},
        "created_at": {"type": "string"},
        "resolved_by": {"type": "string"},
        "reason": {"type": "string"},
        "candidate_id": {"type": "string"},
        "error_code": {"type": ["string", "null"]},
    },
}

CANDIDATE_REPORT_SCHEMA: Final[dict[str, Any]] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://diceratchet.local/schemas/evolve_candidate_report.schema.json",
    "title": "evolve_candidate_report",
    "description": "MOD-07 candidates/{candidate_id}.json（PRD §7：candidate + report + "
                   "decision + reason + error_code）。",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "candidate_id", "kind", "diff", "rationale",
                 "expected_effect", "touched_files", "diff_lines", "gen_id", "report",
                 "decision", "decided_by", "reason", "error_code"],
    "properties": {
        "schema_version": {"type": "string"},
        "candidate_id": CANDIDATE_SCHEMA["properties"]["candidate_id"],
        "kind": CANDIDATE_SCHEMA["properties"]["kind"],
        "diff": {"type": "string"},
        "rationale": {"type": "string"},
        "expected_effect": CANDIDATE_SCHEMA["properties"]["expected_effect"],
        "touched_files": {"type": "array", "items": {"type": "string"}},
        "diff_lines": {"type": "integer", "minimum": 0},
        "gen_id": {"type": "string"},
        "report": {
            "type": "object",
            "additionalProperties": False,
            "required": ["smoke", "heldout", "paired", "elapsed_s", "error_code"],
            "properties": {
                "smoke": {"type": ["object", "null"]},
                "heldout": {"type": ["object", "null"]},
                "paired": {"type": ["object", "null"]},
                "train": {"type": ["object", "null"]},
                "diversity": {"type": ["object", "null"]},
                "elapsed_s": {"type": "number", "minimum": 0},
                "error_code": {"type": ["string", "null"]},
            },
        },
        "decision": {"type": ["string", "null"],
                     "enum": ["accept", "reject", "pending", "rollback", None]},
        "decided_by": {"type": ["string", "null"], "enum": ["auto", "human", None]},
        "reason": {"type": "string"},
        "error_code": {"type": ["string", "null"]},
        "verdict": {"type": ["object", "null"]},
    },
}


# =====================================================================
# 校验入口（G5 round-trip）
# =====================================================================

_VALIDATOR_CACHE: dict[str, Any] = {}


def _jsonschema_validator(schema: Mapping[str, Any], key: str) -> Any:
    cached = _VALIDATOR_CACHE.get(key)
    if cached is not None:
        return cached
    import jsonschema # 延迟导入：轻路径不触发

    validator = jsonschema.Draft202012Validator(dict(schema))
    _VALIDATOR_CACHE[key] = validator
    return validator


def _validate(schema: Mapping[str, Any], obj: Mapping[str, Any], *, name: str,
              code: E | str) -> None:
    try:
        _jsonschema_validator(schema, name).validate(dict(obj))
    except Exception as exc: # noqa: BLE001 - jsonschema.ValidationError / SchemaError
        path = getattr(exc, "json_path", "$")
        message = getattr(exc, "message", str(exc))
        raise InfraError(code, f"{name} schema 校验失败 at {path}: {message}") from exc


def validate_candidate_dict(obj: Mapping[str, Any]) -> None:
    """``Candidate`` **字典**校验（G5 round-trip）；失败 → ``E_EVOLVE_PATCH_INVALID``。

    注意与 ``proposer.validate_candidate(cand, cfg=...)`` 区分：后者做的是 FR-07-4 的
    改动面/白名单/签名语义校验，本函数只做 schema 形状校验。
    """
    _validate(CANDIDATE_SCHEMA, obj, name="evolve_candidate",
              code=E.E_EVOLVE_PATCH_INVALID)


def validate_candidate_report(obj: Mapping[str, Any]) -> None:
    """``CandidateReport`` 字典校验；失败 → ``E_EVOLVE_JUDGE_FAILED``。"""
    _validate(CANDIDATE_REPORT_SCHEMA, obj, name="evolve_candidate_report",
              code=E.E_EVOLVE_JUDGE_FAILED)


def validate_verdict(obj: Mapping[str, Any]) -> None:
    _validate(VERDICT_SCHEMA, obj, name="evolve_verdict", code=E.E_EVOLVE_JUDGE_FAILED)


def validate_harness_ref(obj: Mapping[str, Any]) -> None:
    _validate(HARNESS_REF_SCHEMA, obj, name="evolve_harness_ref",
              code=E.E_EVOLVE_VERSION_CONFLICT)


def validate_gate_state(obj: Mapping[str, Any]) -> None:
    _validate(GATE_STATE_SCHEMA, obj, name="evolve_gate_state",
              code=E.E_EVOLVE_VERSION_CONFLICT)


def validate_evidence_bundle(obj: Mapping[str, Any]) -> None:
    _validate(EVIDENCE_BUNDLE_SCHEMA, obj, name="evolve_evidence_bundle",
              code=E.E_EVAL_INV5_LEAK)


# =====================================================================
# 落盘助手（read_json/write_json_atomic 的薄包装，schema 名与文件一一对应）
# =====================================================================

def write_candidate_report(path: Any, report: CandidateReport, *,
                           fsync: bool = False) -> str:
    """写 ``candidates/{id}.json``（原子写；先自校验再落盘）。"""
    payload = report.as_dict()
    validate_candidate_report(payload)
    return write_json_atomic(path, payload, fsync=fsync)


def read_candidate_report(path: Any) -> CandidateReport:
    raw = read_json(path)
    if not isinstance(raw, Mapping):
        raise InfraError(E.E_EVOLVE_JUDGE_FAILED,
                         f"候选报告不是 JSON 对象：{path}")
    return CandidateReport.from_dict(raw)


def read_gate_state(path: Any) -> GateState:
    raw = read_json(path)
    if not isinstance(raw, Mapping):
        raise InfraError(E.E_EVOLVE_JUDGE_FAILED, f"gate.json 不是 JSON 对象：{path}")
    return GateState.from_dict(raw)
