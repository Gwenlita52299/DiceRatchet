"""局末反思编排（MOD-06 PRD FR-06-3/4；Spec §4.7）。

两件事，都不产生"主循环失败"：

1. ``summarize_episode``：把 ``RecordedEpisode`` + 最近若干局摘要压成**确定性**
   ``EpisodeSummary``（不经 LLM、不含随机、只看轨迹）；
2. ``reflect``：渲染 ``harness/prompts/reflection.md`` → MOD-03 ``generate_json`` →
   MOD-05 ``ReflectionEntry``。**任何一步失败都只降级**：调用方写
   ``{"status":"skipped","reason":"<E_*>"}`` 后继续下一局（FR-06-4）。

反思失败绝不判该局失败、绝不阻塞 run。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from ..infra.errors import E, InfraError
from ..infra.io import sha256_bytes, validate, write_json_atomic
from ..infra.run import RunPaths
from .state import utc_now

__all__ = ["EpisodeSummary", "RecentSummary", "REFLECTION_RECORD_SCHEMA", "failure_causes",
           "memory_tags", "reason_of", "reflect", "reflection_output_schema",
           "reflection_path", "skipped_record", "ok_record", "summarize_episode",
           "template_sha256", "write_reflection_record"]

REFLECTION_RECORD_SCHEMA = "loop_reflection"


def failure_causes() -> tuple[str, ...]:
    """``failure_cause`` 的受控取值——**唯一事实来源是 MOD-05**（``ReflectionEntry``
    schema 的 ``failure_cause`` enum）。MOD-06 只引用，不复制，避免两处枚举漂移。
    """
    from ..memory.schemas import FAILURE_CAUSES

    return tuple(FAILURE_CAUSES)


def memory_tags() -> tuple[str, ...]:
    """MOD-05 ``MemoryEntry.tags`` 的受控取值（prompt 里作为 ``tags`` 的建议集）。"""
    from ..memory.schemas import MEMORY_TAGS

    return tuple(MEMORY_TAGS)


def reflection_output_schema() -> dict[str, Any]:
    """反思 LLM 输出的 JSON Schema（GBNF 友好：只用 object/array/string/enum）。"""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["text", "failure_cause", "tags"],
        "properties": {
            "text": {"type": "string", "minLength": 1},
            "failure_cause": {"type": "string", "enum": list(failure_causes())},
            "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        },
    }

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


# =====================================================================
# 确定性摘要
# =====================================================================

@dataclass(frozen=True)
class RecentSummary:
    """最近某一局的同类摘要（模板的「近期对照」段）。"""

    episode_key: str
    outcome: str
    stages_cleared: int
    hearts_left: int
    battles_won: int
    truncation_reason: str | None = None
    decisions: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"episode_key": self.episode_key, "outcome": self.outcome,
                "stages_cleared": int(self.stages_cleared),
                "hearts_left": int(self.hearts_left),
                "battles_won": int(self.battles_won),
                "truncation_reason": self.truncation_reason,
                "decisions": int(self.decisions)}


@dataclass(frozen=True)
class EpisodeSummary:
    """一局的确定性摘要（本模块从轨迹生成，**不经 LLM**，FR-06-3）。"""

    episode_key: str
    seed: int
    generation: int
    harness_version: int
    tick: int
    outcome: str
    truncation_reason: str | None
    stages_cleared: int
    stage_count: int
    hearts_left: int
    battles_won: int
    decisions: int
    illegal_attempts: int
    fallback_decisions: int
    cum_reward: float
    degraded: bool
    run_id: str = ""
    recent: tuple[RecentSummary, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "episode_key": self.episode_key, "run_id": self.run_id,
            "seed": int(self.seed),
            "generation": int(self.generation),
            "harness_version": int(self.harness_version), "tick": int(self.tick),
            "outcome": self.outcome, "truncation_reason": self.truncation_reason,
            "stages_cleared": int(self.stages_cleared),
            "stage_count": int(self.stage_count), "hearts_left": int(self.hearts_left),
            "battles_won": int(self.battles_won), "decisions": int(self.decisions),
            "illegal_attempts": int(self.illegal_attempts),
            "fallback_decisions": int(self.fallback_decisions),
            "cum_reward": round(float(self.cum_reward), 4),
            "degraded": bool(self.degraded),
            "recent": [r.to_dict() for r in self.recent],
        }


def summarize_episode(rec: Any, *, window: Sequence[Any] = ()) -> EpisodeSummary:
    """把 ``RecordedEpisode`` 与最近 ``window`` 局压成确定性摘要。

    ``window`` 的元素只需具备 ``episode_key/outcome/stages_cleared/hearts_left/
    battles_won`` 属性（``RecordedEpisode`` 或 ``RecentSummary`` 均可）。渲染时按
    **新→旧** 排列，最多保留 ``len(window)`` 条。
    """
    recent: list[RecentSummary] = []
    for item in window:
        if isinstance(item, RecentSummary):
            recent.append(item)
        else:
            recent.append(RecentSummary(
                episode_key=str(getattr(item, "episode_key", "")),
                outcome=str(getattr(item, "outcome", "lose")),
                stages_cleared=int(getattr(item, "stages_cleared", 0) or 0),
                hearts_left=int(getattr(item, "hearts_left", 0) or 0),
                battles_won=int(getattr(item, "battles_won", 0) or 0),
                truncation_reason=getattr(item, "truncation_reason", None),
                decisions=int(getattr(item, "decisions", 0) or 0)))
    recent.reverse() # 新 → 旧
    return EpisodeSummary(
        episode_key=str(rec.episode_key), seed=int(rec.seed),
        generation=int(rec.generation), harness_version=int(rec.harness_version),
        tick=int(getattr(rec, "tick", 0)), outcome=str(rec.outcome),
        run_id=str(getattr(rec, "run_id", "") or ""),
        truncation_reason=rec.truncation_reason,
        stages_cleared=int(rec.stages_cleared), stage_count=_stage_count(rec),
        hearts_left=int(rec.hearts_left), battles_won=int(rec.battles_won),
        decisions=int(rec.decisions), illegal_attempts=int(rec.illegal_attempts),
        fallback_decisions=int(rec.fallback_decisions),
        cum_reward=float(rec.cum_reward), degraded=bool(rec.degraded),
        recent=tuple(recent))


def _stage_count(rec: Any) -> int:
    """本局关数（公开信息）；取不到时用已通关数兜底。"""
    value = getattr(rec, "stage_count", None)
    if value is None:
        cfg = getattr(rec, "episode_config", None)
        value = getattr(cfg, "stage_count", None)
    if value is None:
        return int(getattr(rec, "stages_cleared", 0) or 0)
    return int(value)


# =====================================================================
# 模板渲染与反思
# =====================================================================

def _render_recent(recent: Sequence[RecentSummary]) -> str:
    if not recent:
        return "（无：这是本 run 的第一局训练局）"
    lines = []
    for item in recent:
        lines.append(
            f"- {item.episode_key}: {item.outcome}"
            f"（关卡 {item.stages_cleared}，心 {item.hearts_left}，"
            f"战斗胜 {item.battles_won}，truncation={item.truncation_reason}）")
    return "\n".join(lines)


def _render_template(text: str, summary: EpisodeSummary) -> str:
    values = {
        "episode_key": summary.episode_key,
        "outcome": summary.outcome,
        "truncation_reason": summary.truncation_reason or "null",
        "stages_cleared": str(summary.stages_cleared),
        "stage_count": str(summary.stage_count),
        "hearts_left": str(summary.hearts_left),
        "battles_won": str(summary.battles_won),
        "decisions": str(summary.decisions),
        "illegal_attempts": str(summary.illegal_attempts),
        "fallback_decisions": str(summary.fallback_decisions),
        "cum_reward": f"{summary.cum_reward:.4f}",
        "recent": _render_recent(summary.recent),
    }

    def _sub(match: re.Match[str]) -> str:
        return values.get(match.group(1), "")

    return _PLACEHOLDER_RE.sub(_sub, text)


def reflect(summary: EpisodeSummary, *, client: Any, template: Path | str,
            timeout_s: float = 60.0) -> Any:
    """渲染模板 → MOD-03 ``generate_json`` → MOD-05 ``ReflectionEntry``。

    返回 ``None`` 表示**降级跳过**（模型没给出可用文本），由调用方写 ``skipped`` 标注
    （FR-06-4）。可预期的失败（超时 / schema 不符 / 模板缺失）以 ``InfraError`` /
    ``TimeoutError`` 抛出，调用方用 ``reason_of()`` 映射成 ``E_*`` 后同样降级。
    """
    # Issue #1：反思是独立的模型角色（reflection），不再假定与 policy 同一个模型。
    from ..inference.client import coerce_client
    from ..memory import ReflectionEntry, validate_reflection_entry

    template_path = Path(template)
    try:
        template_bytes = template_path.read_bytes()
    except OSError as exc:
        raise InfraError(E.E_ACTION_TEMPLATE,
                         f"反思模板不可读：{template_path}\n{exc}",
                         hint="PRD FR-06-3 要求 harness/prompts/reflection.md 存在") from exc
    prompt = _render_template(template_bytes.decode("utf-8"), summary)

    # 反思 prompt 是 agent 可见出口之一（Spec §8.4 B-19）：落盘/调用前过泄漏护栏
    # （INV-5）。命中即 E_EVAL_INV5_LEAK，由调用方降级为 skipped——绝不把
    # held-out/test seed 喂给模型。
    _scan_reflection_prompt(prompt, seed=int(summary.seed))

    llm = coerce_client(client, role="reflection")
    causes = failure_causes()
    result = _call_with_timeout(
        lambda: llm.generate_json(
            [{"role": "user", "content": prompt}], reflection_output_schema(),
            mode="eval", sampling={"temperature": 0.0, "max_tokens": 256}, seed=0),
        timeout_s=float(timeout_s))

    payload = _extract_payload(result)
    text = str(payload.get("text") or "").strip()
    if not text:
        return None
    cause = str(payload.get("failure_cause") or "unknown")
    if cause not in causes:
        cause = "unknown"
    tags = tuple(str(t) for t in (payload.get("tags") or ()) if str(t).strip())[:5]
    usage = getattr(result, "usage", None)
    entry = ReflectionEntry(
        reflection_id=_reflection_id(summary, text),
        schema_version="1.1",
        created_at=utc_now(),
        run_id=str(summary.run_id),
        episode_id=summary.episode_key,
        seed=int(summary.seed), split="train", generation=int(summary.generation),
        harness_version=int(summary.harness_version), tick=int(summary.tick),
        text=text, outcome=str(summary.outcome),
        truncation_reason=summary.truncation_reason,
        stages_cleared=int(summary.stages_cleared),
        hearts_left=int(summary.hearts_left), battles_won=int(summary.battles_won),
        decisions=int(summary.decisions), illegal_attempts=int(summary.illegal_attempts),
        fallback_decisions=int(summary.fallback_decisions), failure_cause=cause,
        tags=tags,
        llm_latency_ms=_opt_int(getattr(usage, "latency_ms", None)),
        prompt_tokens=_opt_int(getattr(usage, "prompt_tokens", None)),
        completion_tokens=_opt_int(getattr(usage, "completion_tokens", None)))
    validate_reflection_entry(entry.to_dict())
    return entry


def _scan_reflection_prompt(prompt: str, *, seed: int) -> None:
    """INV-5：反思 prompt 出口的护栏（命中即 ``E_EVAL_INV5_LEAK``）。"""
    from ..evals.seeds import guard_prompt_context

    guard_prompt_context(split="train", seed=int(seed), text=prompt,
                         destination="reflection")


def _call_with_timeout(fn: Any, *, timeout_s: float) -> Any:
    """同步调用的墙钟上限（``loop.reflect_timeout_s``）。

    与 MOD-08 的 F20 同款残余风险：Python 无法杀死已在运行的线程；被放弃的调用由
    MOD-03 的调用级超时（``generate_timeout_ms`` / ``total_timeout_ms``）收尾。
    """
    if timeout_s <= 0:
        raise TimeoutError("reflect_timeout_s<=0")
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="loop-reflect")
    try:
        future = pool.submit(fn)
        return future.result(timeout=timeout_s)
    except FutureTimeoutError as exc:
        raise TimeoutError(f"反思超时（>{timeout_s}s）") from exc
    finally:
        pool.shutdown(wait=False)


def _extract_payload(result: Any) -> Mapping[str, Any]:
    """从 MOD-03 的 ``JsonResult`` 取结构化输出；拿不到就抛 ``E_LLM_SCHEMA_INVALID``。"""
    parsed = getattr(result, "parsed", None)
    if isinstance(parsed, Mapping):
        return dict(parsed)
    text = getattr(result, "text", None)
    if isinstance(text, str) and text.strip():
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise InfraError(E.E_LLM_SCHEMA_INVALID,
                             f"反思输出不是合法 JSON：{text[:120]!r}") from exc
        if isinstance(data, Mapping):
            return dict(data)
    raise InfraError(E.E_LLM_SCHEMA_INVALID, "反思输出缺少可解析的 JSON 对象")


def _reflection_id(summary: EpisodeSummary, text: str) -> str:
    """确定性 ``reflection_id``（NFR-06-1：不引入随机/墙钟）。"""
    payload = f"{summary.episode_key}|{summary.tick}|{summary.harness_version}|{text}"
    return "refl-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _opt_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


# =====================================================================
# 降级映射与落盘
# =====================================================================

def reason_of(exc: BaseException) -> str:
    """异常 → 已登记的 ``E_*`` 错误码（PRD §10 的降级矩阵）。"""
    code = getattr(exc, "code", None)
    if code is not None:
        return str(code)
    if isinstance(exc, (TimeoutError, FutureTimeoutError)):
        return str(E.E_LLM_TIMEOUT)
    if isinstance(exc, json.JSONDecodeError):
        return str(E.E_LLM_SCHEMA_INVALID)
    name = type(exc).__name__
    if name in ("ValidationError", "SchemaError"):
        return str(E.E_LLM_SCHEMA_INVALID)
    return str(E.E_LLM_SCHEMA_INVALID)


def reflection_path(paths: RunPaths, episode_key: str) -> Path:
    return Path(paths.reflections) / f"{episode_key}.json"


def write_reflection_record(paths: RunPaths, *, record: Mapping[str, Any]) -> Path:
    """写 ``reflections/<episode_key>.json``（成功与 skipped 共用同一落点）。"""
    validate(REFLECTION_RECORD_SCHEMA, record)
    path = reflection_path(paths, str(record["episode_key"]))
    write_json_atomic(path, dict(record))
    return path


def skipped_record(*, record: Any, reason: str, template_sha256: str = "") -> dict[str, Any]:
    """``status="skipped"`` 的记录外壳（FR-06-4）。"""
    return {
        "reflection_id": None, "episode_key": str(record.episode_key),
        "seed": int(record.seed), "generation": int(record.generation),
        "harness_version": int(record.harness_version),
        "tick": int(getattr(record, "tick", 0)), "outcome": str(record.outcome),
        "truncation_reason": record.truncation_reason, "status": "skipped",
        "reason": str(reason), "entry": None, "llm": None,
        "template_sha256": str(template_sha256),
    }


def ok_record(*, record: Any, entry: Any, llm: Mapping[str, Any] | None = None,
              template_sha256: str = "") -> dict[str, Any]:
    """``status="ok"`` 的反思记录外壳。"""
    return {
        "reflection_id": str(getattr(entry, "reflection_id", "")),
        "episode_key": str(record.episode_key), "seed": int(record.seed),
        "generation": int(record.generation),
        "harness_version": int(record.harness_version),
        "tick": int(getattr(record, "tick", 0)), "outcome": str(record.outcome),
        "truncation_reason": record.truncation_reason, "status": "ok", "reason": None,
        "entry": dict(entry.to_dict()), "llm": (dict(llm) if llm else None),
        "template_sha256": str(template_sha256),
    }


def template_sha256(path: Path | str) -> str:
    """模板文件的 ``sha256:<hex>`` 摘要（不可读时返回空串，不阻断）。"""
    try:
        return "sha256:" + sha256_bytes(Path(path).read_bytes())
    except OSError:
        return ""

