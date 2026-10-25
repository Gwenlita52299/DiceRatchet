"""MOD-04 prompt 组装：模板加载、占位符填充、预算裁剪与 ``prompt_hash``（PRD FR-04-1..6）。

设计要点（逐条对应 PRD）：

- **INV-3 唯一注入出口**（FR-04-5）：进入 prompt 的文本只有四类来源——当前 Observation、
  MOD-05 检索到的 memory、当前 harness 工件、任务说明 ``prompts/task.md``。本模块是唯一
  把这几类文本拼进 LLM 输入的地方；调用方只能提供结构化条目，不能提供成品 prompt。
- **单遍替换**（FR-04-2）：用一次 ``re.sub`` 完成全部占位符替换，替换值里即使出现
  ``{{...}}`` 也不会被二次解释；未知占位符**原样保留**并记入
  ``PromptBundle.unknown_placeholders``。
- **预算裁剪**（FR-04-3）：优先级 Observation > 近期反思 > 策略记忆。前三段（task /
  harness_version / tools / observation）永不裁剪；memory 按 ``(score desc, memory_id asc)``
  留 top_k 且先丢最低分；reflections 按 ``(generation desc, reflection_id asc)`` 留最新
  ``reflection_max`` 条；每次丢弃写进 ``PromptBundle.truncated``。仍超预算 →
  ``E_ACTION_BUDGET``（由 ``PolicyAgent`` 降级为启发式）。
- **纯函数**（NFR-04-1）：无随机、无墙钟、无 IO（``load_template`` 是唯一读盘函数）。
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Iterable, Mapping, Sequence

from dice_ratchet.infra.errors import E, InfraError

__all__ = [
    "DEFAULT_TEMPLATE_PATH", "TASK_PATH", "FIXED_TEMPLATE_PATH",
    "PLACEHOLDER_ORDER", "KNOWN_PLACEHOLDERS", "REQUIRED_PLACEHOLDERS",
    "MemoryItem", "ReflectionItem", "PromptSpec", "PromptContext", "PromptBundle",
    "TruncationRecord", "PromptTemplateError", "load_template", "check_placeholders",
    "assemble_prompt", "estimate_tokens", "sha256_of", "render_memory", "render_reflections",
    "load_task_text", "resolve_path",
]

#: 内置兜底模板（FR-04-1：主模板不可用时回退到这里，并置 ``fallback_used=True``）。
DEFAULT_TEMPLATE_PATH: Final[Path] = Path(__file__).resolve().parent / "decision_fallback.md"
#: INV-3 第 ④ 来源：人工版本化的任务说明，**不是**演化对象（Spec v1.4 B-27）。
TASK_PATH: Final[Path] = Path(__file__).resolve().parent / "task.md"
#: MOD-08 ``fixed_prompt`` 基线的冻结模板（不参与演化）。
FIXED_TEMPLATE_PATH: Final[Path] = Path(__file__).resolve().parent / "fixed_v1.md"

#: FR-04-2 规定的填充顺序（单遍替换，顺序仅用于文档与可读性）。
PLACEHOLDER_ORDER: Final[tuple[str, ...]] = (
    "task", "harness_version", "tools", "observation", "reflections", "memory", "constraints",
)
#: Spec §4.4 的 harness 模板还允许 ``{{legal_actions}}``（``decision.md`` 契约）。
EXTRA_PLACEHOLDERS: Final[tuple[str, ...]] = ("legal_actions",)
KNOWN_PLACEHOLDERS: Final[frozenset[str]] = frozenset(PLACEHOLDER_ORDER + EXTRA_PLACEHOLDERS)
#: FR-04-1：主模板必须含有的占位符；缺任一 → ``E_ACTION_TEMPLATE`` + 回退。
REQUIRED_PLACEHOLDERS: Final[frozenset[str]] = frozenset({"observation", "memory"})

_PLACEHOLDER_RE: Final[re.Pattern[str]] = re.compile(
    r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

#: ``chars_div_3`` 估算口径（与 MOD-03 ``CHARS_PER_TOKEN`` 对齐，避免两套预算口径）。
CHARS_PER_TOKEN: Final[int] = 3

_EMPTY_SECTION: Final[str] = "(none)"


class PromptTemplateError(InfraError):
    """模板不可用（缺失/不可解码/缺必需占位符）→ ``E_ACTION_TEMPLATE``（FR-04-1）。"""

    def __init__(self, message: str, *, path: str = "", hint: str | None = None) -> None:
        super().__init__(E.E_ACTION_TEMPLATE, message,
                         hint=hint or "检查 policy.template_path；缺失时回退内置 decision_fallback.md")
        self.path = path


# ----------------------------------------------------------------------
# 数据模型（PRD §7）
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class MemoryItem:
    """一条待注入的策略记忆（MOD-05 ``RetrieveResult.entries`` 的 agent 侧视图）。

    只依赖四个公开字段（MOD-04 PRD §14）：``memory_id`` / ``score`` / ``text`` / ``version``；
    ``action_hint`` 用于 ``DecisionResult.adopted_memory_ids``（MOD-05 §6.4）。
    """

    memory_id: str
    text: str
    score: float = 0.0
    version: int | None = None
    action_hint: Mapping[str, Any] | None = None

    def sort_key(self) -> tuple[float, str]:
        """显式全序（NFR-04-1）：``(score desc, memory_id asc)``。"""
        return (-float(self.score), str(self.memory_id))


@dataclass(frozen=True)
class ReflectionItem:
    """一条待注入的近期反思（MOD-06 产出的 ``ReflectionEntry`` 的 agent 侧视图）。"""

    reflection_id: str
    text: str
    generation: int = 0

    def sort_key(self) -> tuple[int, str]:
        """显式全序（NFR-04-1）：``(generation desc, reflection_id asc)``。"""
        return (-int(self.generation), str(self.reflection_id))


@dataclass(frozen=True)
class TruncationRecord:
    """一次裁剪记录（FR-04-3：每次丢弃写入 ``PromptBundle.truncated``）。"""

    kind: str # "memory" | "reflection"
    identifier: str
    reason: str # "top_k" | "reflection_max" | "budget"
    score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "id": self.identifier, "reason": self.reason}
        if self.score is not None:
            out["score"] = round(float(self.score), 6)
        return out


@dataclass(frozen=True)
class PromptSpec:
    """一段可用的模板文本及其派生元数据（``load_template`` 的产物）。"""

    path: str
    text: str
    template_hash: str
    placeholders: tuple[str, ...]
    unknown_placeholders: tuple[str, ...] = ()
    error_codes: tuple[str, ...] = ()
    fallback_used: bool = False

    def has(self, name: str) -> bool:
        return name in self.placeholders


@dataclass(frozen=True)
class PromptContext:
    """``assemble_prompt`` 的全部输入（结构化、可复现；无隐藏状态）。"""

    observation: str
    task: str = ""
    harness_version: int = 0
    tools: str = ""
    legal_actions: str = ""
    #: ``legal_actions`` 的**压缩渲染**（F13）：仅当完整枚举装不下预算时启用。
    #: 空串表示没有可用的压缩形式（此时保持原行为：记 ``E_ACTION_BUDGET``）。
    legal_actions_compact: str = ""
    reflections: tuple[ReflectionItem, ...] = ()
    memory: tuple[MemoryItem, ...] = ()
    constraints: str = ""
    obs_digest: str = ""
    memory_token_budget: int = 900
    token_budget: int = 3072
    token_estimator: str = "chars_div_3"
    reflection_max: int = 3
    memory_top_k: int = 5
    mode: str = "play"

    def with_constraints(self, text: str) -> "PromptContext":
        """重试时替换 ``{{constraints}}``（FR-04-9：写入上次 error_code + error_detail）。"""
        return PromptContext(**{**self.__dict__, "constraints": text})


@dataclass(frozen=True)
class PromptBundle:
    """一次组装的完整结果（PRD §7 字段 + 供 ``PolicyAgent`` 直接消费的 ``messages``）。"""

    text: str
    messages: tuple[Any, ...]
    prompt_hash: str
    template_hash: str
    obs_digest: str
    memory_ids: tuple[str, ...]
    source_ids: tuple[str, ...]
    token_estimate: int
    truncated: tuple[TruncationRecord, ...] = ()
    unknown_placeholders: tuple[str, ...] = ()
    error_codes: tuple[str, ...] = ()
    fallback_used: bool = False
    template_path: str = ""

    @property
    def message_list(self) -> list[Any]:
        return list(self.messages)


# ----------------------------------------------------------------------
# 哈希 / 估算 / 渲染（纯函数）
# ----------------------------------------------------------------------

def sha256_of(text: str) -> str:
    """``"sha256:" + sha256(utf-8 bytes)``（FR-04-4 的统一口径）。"""
    return "sha256:" + hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def estimate_tokens(text: str, estimator: str = "chars_div_3") -> int:
    """确定性 token 估算（NFR-04-2：不引入 tokenizer 依赖）。

    ``backend_tokenizer`` 需要 MOD-03 后端（非纯函数），因此在组装期统一按
    ``chars_div_3`` 估算；真正更准的计数由 MOD-03 在生成后回传
    ``prompt_tokens``（PRD §13），两者用途不同、不互相覆盖。
    """
    if not isinstance(text, str) or not text:
        return 0
    return int(math.ceil(len(text) / CHARS_PER_TOKEN))


def render_memory(items: Sequence[MemoryItem]) -> str:
    """memory 段渲染：每条一行 ``[memory_id] text``（顺序即注入顺序）。"""
    lines = [f"[{item.memory_id}] {item.text}" for item in items]
    return "\n".join(lines) if lines else _EMPTY_SECTION


def render_reflections(items: Sequence[ReflectionItem]) -> str:
    """reflections 段渲染：每条一行 ``[reflection_id] text``。"""
    lines = [f"[{item.reflection_id}] {item.text}" for item in items]
    return "\n".join(lines) if lines else _EMPTY_SECTION


def load_task_text(path: str | Path | None = None) -> str:
    """读取 INV-3 第 ④ 来源 ``prompts/task.md``；不可读时返回内置短说明（不阻断决策）。"""
    target = Path(path) if path is not None else TASK_PATH
    try:
        return target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError): # pragma: no cover - 交付物随包提供
        return "在骰子 roguelike 中做出合法且稳健的决策；只输出 JSON。"


def _strip_harness_prefix(path: Path) -> Path | None:
    """``harness/prompts/x`` → ``prompts/x``（**harness 根**级目录的探测用，F66）。"""
    parts = path.parts
    if len(parts) >= 2 and parts[0] == "harness":
        return Path(*parts[1:])
    return None


def resolve_path(path: str | Path, *, harness_dir: Path | None = None) -> Path:
    """把配置里的相对路径解析为可读文件。

    解析顺序（**F66，2026-10-14：调用方给的 harness 目录必须优先于 CWD**）：
      1. 绝对路径原样返回；
      2. ``harness_dir / path``（调用方给的是**仓库根**级目录）；
      3. ``harness_dir / <去掉 ``harness/`` 前缀的 path>``（调用方给的是**harness 根**，
         例如 MOD-07 的 ``generations/gen-NNN/staging/<id>/`` 拷贝）；
      4. ``harness_dir.parent / path``（旧语义：``D=harness/``、path 相对仓库根 ——
         ``fixed_prompt_path`` 这类**不在 harness/ 下**的随包冻结件靠这一条）；
      5. ``CWD / path``。

    **为什么第 3 条必须早于第 5 条**：候选补丁的 staging 目录布局是
    ``<staging>/<id>/prompts/decision.md``。若只探测 ``D.parent/path`` 就探测不到，
    于是回落到 CWD 的**未打补丁**模板 —— 实测（F66）三次候选评估因此全部测的是 parent
    （候选臂与 parent 的首个决策点 ``prompt_hash`` 逐字节相同，40/40）。
    返回第一个存在的候选；都不存在时返回**最后一个候选**（CWD 相对路径，供报错使用，
    与修复前一致）。
    """
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    probes: list[Path] = []
    if harness_dir is not None:
        d = Path(harness_dir)
        probes.append(d / candidate)
        stripped = _strip_harness_prefix(candidate)
        if stripped is not None:
            probes.append(d / stripped)
        probes.append(d.parent / candidate)
    probes.append(Path.cwd() / candidate)
    for probe in probes:
        if probe.exists():
            return probe
    return probes[-1]


# ----------------------------------------------------------------------
# 模板加载（FR-04-1）
# ----------------------------------------------------------------------

def _spec_from_text(path: str, text: str, *, fallback_used: bool,
                    error_codes: Iterable[str] = ()) -> PromptSpec:
    placeholders = tuple(sorted({m.group(1) for m in _PLACEHOLDER_RE.finditer(text)}))
    unknown = tuple(sorted(set(placeholders) - KNOWN_PLACEHOLDERS))
    return PromptSpec(path=path, text=text, template_hash=sha256_of(text),
                      placeholders=placeholders, unknown_placeholders=unknown,
                      error_codes=tuple(error_codes), fallback_used=fallback_used)


def check_placeholders(spec: PromptSpec,
                       required: frozenset[str] = REQUIRED_PLACEHOLDERS) -> None:
    """校验必需占位符；缺失抛 ``E_ACTION_TEMPLATE``（FR-04-1）。"""
    missing = sorted(required - set(spec.placeholders))
    if missing:
        raise PromptTemplateError(
            f"模板 {spec.path} 缺少必需占位符 {missing}",
            path=spec.path,
            hint="Spec §4.4：decision.md 必须含 {{observation}} 与 {{memory}}")


def load_template(path: str | Path, *,
                  fallback_path: str | Path = DEFAULT_TEMPLATE_PATH) -> PromptSpec:
    """加载模板；任何不可用情形都回退内置兜底模板并记 ``E_ACTION_TEMPLATE``。

    ``fallback_used=True`` 时 ``error_codes`` 已含 ``E_ACTION_TEMPLATE``，调用方直接
    并入 ``DecisionResult.error_codes``（FR-04-16）。
    """
    requested = str(path)
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return _load_fallback(fallback_path, requested,
                              f"模板不可读（{type(exc).__name__}: {exc}）")
    spec = _spec_from_text(requested, text, fallback_used=False)
    try:
        check_placeholders(spec)
    except PromptTemplateError as exc:
        return _load_fallback(fallback_path, requested, exc.message)
    return spec


def _load_fallback(fallback_path: str | Path, requested: str, reason: str) -> PromptSpec:
    try:
        text = Path(fallback_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc: # pragma: no cover - 交付物随包提供
        raise PromptTemplateError(
            f"主模板不可用（{reason}）且兜底模板也不可读：{exc}",
            path=str(fallback_path)) from exc
    return _spec_from_text(str(fallback_path), text, fallback_used=True,
                           error_codes=(E.E_ACTION_TEMPLATE,))


# ----------------------------------------------------------------------
# 组装（FR-04-2/3/4）
# ----------------------------------------------------------------------

def _select_reflections(items: Sequence[ReflectionItem], limit: int
                        ) -> tuple[list[ReflectionItem], list[TruncationRecord]]:
    ordered = sorted(items, key=ReflectionItem.sort_key)
    if limit < 0:
        limit = 0
    kept, dropped = ordered[:limit], ordered[limit:]
    records = [TruncationRecord("reflection", item.reflection_id, "reflection_max")
               for item in dropped if item.reflection_id]
    return kept, records


def _select_memory(items: Sequence[MemoryItem], limit: int
                   ) -> tuple[list[MemoryItem], list[TruncationRecord]]:
    ordered = sorted(items, key=MemoryItem.sort_key)
    if limit < 0:
        limit = 0
    kept, dropped = ordered[:limit], ordered[limit:]
    records = [TruncationRecord("memory", item.memory_id, "top_k", score=item.score)
               for item in dropped if item.memory_id]
    return kept, records


#: 仓库根（`dice_ratchet/agent/prompts/assembler.py` 往上三级）；装成 wheel 时不存在。
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]


def _source_path(path: str | Path) -> str:
    """来源清单里的模板路径只记**仓库相对**形式。

    绝对路径会把本机用户名与目录结构写进 golden、run 工件和审计，既是隐私泄漏，
    也让同一份 harness 在不同机器上产出不同的 ``source_ids``。仓库外的路径只留文件名。
    """
    target = Path(path)
    try:
        return target.resolve().relative_to(_REPO_ROOT).as_posix()
    except (OSError, ValueError):
        return target.name


def source_ids(spec: PromptSpec, ctx: PromptContext,
               memory: Sequence[MemoryItem],
               reflections: Sequence[ReflectionItem]) -> tuple[str, ...]:
    """INV-3 四类来源的显式清单（审计/复现用；顺序确定）。"""
    ids = [
        f"obs:{ctx.obs_digest or sha256_of(ctx.observation)}",
        f"template:{_source_path(spec.path)}",
        f"harness:{int(ctx.harness_version)}",
    ]
    if ctx.task:
        ids.append("task:" + sha256_of(ctx.task))
    ids.extend(f"memory:{item.memory_id}" for item in memory if item.memory_id)
    ids.extend(f"reflection:{item.reflection_id}" for item in reflections if item.reflection_id)
    return tuple(ids)


def assemble_prompt(spec: PromptSpec, ctx: PromptContext) -> PromptBundle:
    """把 ``PromptSpec`` + ``PromptContext`` 组装为确定性 ``PromptBundle``（FR-04-2..4）。

    裁剪顺序（FR-04-3 的优先级 + F13 的补充）：

    1. ``{{task}}``/``{{harness_version}}``/``{{tools}}``/``{{observation}}`` **永不裁剪**；
    2. 丢 ``memory``（``(score desc, id asc)`` 的末尾 = 分数最低者）；
    3. 丢 ``reflections``（末尾 = 最旧）；
    4. **仍超预算时**才把 ``legal_actions`` 换成压缩渲染（``ctx.legal_actions_compact``，
       记录 ``TruncationRecord("legal_actions", …, "budget")``）并再走一遍 2/3；
    5. 仍然超预算 → ``E_ACTION_BUDGET``（Observation 单独超限，由调用方降级为启发式）。
    """
    from dice_ratchet.inference import Message

    budget = int(ctx.token_budget)
    error_codes: list[str] = []

    def render(memory_items: Sequence[MemoryItem], reflection_items: Sequence[ReflectionItem],
               legal_text: str) -> str:
        values: dict[str, str] = {
            "task": ctx.task,
            "harness_version": str(int(ctx.harness_version)),
            "tools": ctx.tools or _EMPTY_SECTION,
            "observation": ctx.observation,
            "legal_actions": legal_text or _EMPTY_SECTION,
            "reflections": render_reflections(reflection_items),
            "memory": render_memory(memory_items),
            "constraints": ctx.constraints or "(none)",
        }

        def _sub(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in values: # 未知占位符原样保留（FR-04-2）
                return match.group(0)
            return values[name]

        return _PLACEHOLDER_RE.sub(_sub, spec.text)

    def shrink(legal_text: str) -> tuple[str, int, list[MemoryItem],
                                        list[ReflectionItem], list[TruncationRecord]]:
        """在给定 legal 渲染下按 2/3 的顺序丢 memory/reflections，返回最终文本与估算。"""
        kept_reflections, dropped = _select_reflections(ctx.reflections, ctx.reflection_max)
        kept_memory, mem_drops = _select_memory(ctx.memory, ctx.memory_top_k)
        records = list(dropped) + list(mem_drops)
        text = render(kept_memory, kept_reflections, legal_text)
        estimate = estimate_tokens(text, ctx.token_estimator)
        while estimate > budget and kept_memory:
            dropped_item = kept_memory.pop() # 已按 (score desc, id asc) 排序 → 末尾分最低
            records.append(TruncationRecord("memory", dropped_item.memory_id, "budget",
                                            score=dropped_item.score))
            text = render(kept_memory, kept_reflections, legal_text)
            estimate = estimate_tokens(text, ctx.token_estimator)
        while estimate > budget and kept_reflections:
            dropped_item = kept_reflections.pop() # 末尾即最旧（generation 最小）
            records.append(TruncationRecord("reflection", dropped_item.reflection_id, "budget"))
            text = render(kept_memory, kept_reflections, legal_text)
            estimate = estimate_tokens(text, ctx.token_estimator)
        return text, estimate, kept_memory, kept_reflections, records

    text, estimate, memory, reflections, truncated = shrink(ctx.legal_actions)
    if estimate > budget and ctx.legal_actions_compact \
            and ctx.legal_actions_compact != ctx.legal_actions:
        # F13：观测块永不裁剪（FR-04-3），所以最后一步是压缩 **legal_actions** 枚举。
        # 只有在真的更省时才切换（避免无谓地牺牲动作清单的完整性）。
        alt_text, alt_estimate, alt_memory, alt_reflections, alt_truncated = shrink(
            ctx.legal_actions_compact)
        if alt_estimate < estimate:
            text, estimate = alt_text, alt_estimate
            memory, reflections = alt_memory, alt_reflections
            truncated = list(alt_truncated)
            truncated.append(TruncationRecord("legal_actions", "compact", "budget"))
    if estimate > budget:
        # 丢光 memory/reflections 且压缩 legal 后仍超预算 → Observation 单独超限（§10）
        error_codes.append(E.E_ACTION_BUDGET)
    error_codes.extend(spec.error_codes)

    bundle = PromptBundle(
        text=text,
        messages=(Message(role="user", content=text),),
        prompt_hash=sha256_of(text),
        template_hash=spec.template_hash,
        obs_digest=ctx.obs_digest,
        memory_ids=tuple(item.memory_id for item in memory),
        source_ids=source_ids(spec, ctx, memory, reflections),
        token_estimate=estimate,
        truncated=tuple(truncated),
        unknown_placeholders=spec.unknown_placeholders,
        error_codes=tuple(dict.fromkeys(error_codes)),
        fallback_used=spec.fallback_used,
        template_path=spec.path,
    )
    return bundle
