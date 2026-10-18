"""MOD-07 候选生成与合法性校验（PRD FR-07-1 … FR-07-6；INV-3/INV-4/INV-5）。

本模块负责 RSI 闭环里**最危险**的一段：让 agent 改写自己的 harness。设计重心不是
"生成更聪明的改动"，而是**在结构上掐死自我确认环路**：

1. ``build_evidence_bundle()`` 是证据束的**唯一**构造函数（FR-07-2），并按
   ``source_tags`` 白名单收敛上下文；
2. ``assert_no_holdout_leak()`` 是生成侧的 INV-5 硬闸：非 train split、heldout seed
   字面量（正则 ``(?<!\\d){seed}(?!\\d)``）、``heldout``/``test`` 标签或 ``seeds.yaml``
   片段，命中任一即抛 ``E_EVAL_INV5_LEAK``（FR-07-3）；
3. ``render_proposer_prompt()`` 模板化产出，**不允许**插入证据束之外的上下文（INV-3）；
4. ``validate_candidate()`` 是 FR-07-4 的改动面硬约束（纯函数，不落盘、不调模型）；
5. ``propose()`` 只做「调 LLM → schema 重试 → 去重 → 优先级排序」，失败一律返回显式
   ``ProposalResult(candidates=[], error_code=…)``，**绝不静默退化**（FR-07-1）。

确定性（NFR-07-1）：本模块不 import ``random`` / ``time`` / ``uuid``；遍历一律
``sorted()``；唯一随机源是调用方传入的 ``rng_seed``。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Final, Iterable, Mapping, Sequence

from dice_ratchet.infra import events
from dice_ratchet.infra.config import HARNESS_CONFIG_ALLOWED_PREFIXES
from dice_ratchet.infra.errors import E, InfraError
from dice_ratchet.infra.io import read_json

from .errors import HoldoutLeakError, PatchInvalidError, ProposeFailedError
from .paths import harness_dir as default_harness_dir
from .paths import utc_now
from .schemas import (CANDIDATE_SCHEMA, Candidate, EvidenceBundle, ExpectedEffect,
                      HarnessSnapshot, ProposalResult, ValidationResult, _plain,
                      canonical_json, count_diff_lines, diff_digest, parse_diff_paths)

__all__ = [
    "ALLOWED_TOOL_EXT", "CONFIG_FILE", "DEAD_ENDS_FILE", "DEAD_ENDS_REL", "DECISION_PROMPT",
    "EVIDENCE_SCHEMA_VERSION",
    "FROZEN_SIGNATURE", "PROMPT_PLACEHOLDERS", "REFLECTION_PROMPT", "TOOLS_GLOB",
    "apply_unified_diff", "assert_no_holdout_leak", "authoring_prompt",
    "build_evidence_bundle", "candidate_id_of_raw", "candidate_schema_dict",
    "dedupe_candidates", "get_client_provider", "harness_snapshot",
    "normalize_generated_candidate", "order_candidates", "parse_proposal_payload",
    "assert_prompt_is_clean", "propose", "render_proposer_prompt",
    "set_client_provider", "validate_candidate",
]

# =====================================================================
# 常量（FR-07-4 的文件白名单与禁用面）
# =====================================================================

EVIDENCE_SCHEMA_VERSION: Final[str] = "evolve-evidence-1.0.0"

DECISION_PROMPT: Final[str] = "harness/prompts/decision.md"
REFLECTION_PROMPT: Final[str] = "harness/prompts/reflection.md"
CONFIG_FILE: Final[str] = "harness/config.json"
TOOLS_GLOB: Final[str] = "harness/tools/*.py"
ALLOWED_TOOL_EXT: Final[str] = ".py"

#: 死路记录（D1=A，2026-10-14 用户裁决）：**只给提案方看**、**禁止候选改动**。
#: ``DEAD_ENDS_REL`` 是 ``harness/`` 根下的相对名（``versioning`` 也用它做落盘路径，
#: 常量只有这一处定义，避免两个模块各写一份而漂移）。
DEAD_ENDS_REL: Final[str] = "dead_ends.md"
DEAD_ENDS_FILE: Final[str] = f"harness/{DEAD_ENDS_REL}"

#: 冻结的签名字符串（改动它的 diff 一律拒；Spec B-31 / FR-07-4）。
FROZEN_SIGNATURE: Final[str] = "rank_actions"

#: proposer prompt 的唯一允许占位符（FR-07-3 / INV-3 回归）。
PROMPT_PLACEHOLDERS: Final[tuple[str, ...]] = ("observation", "memory", "harness", "task")

#: 禁止出现在候选里的调用面（INV-4：网络/文件写/subprocess/env 内窥）。
_BANNED_CALL_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    (r"\burllib\.request\b", "urllib.request"),
    (r"\burlopen\s*\(", "urlopen"),
    (r"\brequests\.", "requests"),
    (r"\bsocket\.", "socket"),
    (r"\bsubprocess\b", "subprocess"),
    (r"\bos\.system\b", "os.system"),
    (r"\bos\.environ\b", "os.environ"),
    (r"\bos\.getenv\b", "os.getenv"),
    (r"\bgetenv\s*\(", "getenv"),
    (r"\bopen\s*\(", "open()"),
    (r"\bPath\s*\([^)]*\)\s*\.\s*write", "Path.write"),
    (r"\bshutil\.", "shutil"),
    (r"\b__import__\b", "__import__"),
    (r"\beval\s*\(", "eval()"),
    (r"\bexec\s*\(", "exec()"),
    (r"\bimportlib\b", "importlib"),
    (r"\bdice_ratchet\.env\b", "dice_ratchet.env"),
    (r"\bdice_ratchet\.sandbox\b", "dice_ratchet.sandbox"),
    (r"\bseeds\.yaml\b", "seeds.yaml"),
)

#: 禁止改写的 harness 元数据路径（MAL-6：历史重写 / 版本伪造）。
#: ``harness/dead_ends.md``（D1=A，2026-10-14）也在其中：模型若能改它，就能**抹掉自己的
#: 拒绝记录**再提同一份补丁——那正是这个机制要防的事。
_FORBIDDEN_PATH_PATTERNS: Final[tuple[str, ...]] = (
    "harness/version.json", "harness/versions", "harness/.git", "version.json",
    "harness/dead_ends.md", "dead_ends.md",
)

_IMPORT_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:from\s+([A-Za-z_][\w.]*)\s+import|import\s+([A-Za-z_][\w.]*))", re.MULTILINE)
_DEF_RE: Final[re.Pattern[str]] = re.compile(r"^\s*def\s+([A-Za-z_]\w*)\s*\(", re.MULTILINE)
_LAMBDA_ASSIGN_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*([A-Za-z_]\w*)\s*=\s*lambda\b", re.MULTILINE)
_DECORATOR_RE: Final[re.Pattern[str]] = re.compile(r"@harness_tool\b")
_JSON_KEY_RE: Final[re.Pattern[str]] = re.compile(r'"([A-Za-z_][\w.]*)"\s*:')

#: 顶层 import 白名单的兜底（``evolve.tool_import_allowlist`` 缺失时用；与 MOD-02 同步）。
_DEFAULT_IMPORT_ALLOWLIST: Final[tuple[str, ...]] = (
    "math", "re", "json", "itertools", "functools", "typing", "dataclasses",
)


# =====================================================================
# LLM 注入点（组合根 / 测试用；与 loop 的 provider 模式一致）
# =====================================================================

_CLIENT_PROVIDER: Callable[..., Any] | None = None


def set_client_provider(provider: Callable[..., Any] | None) -> None:
    """注册 MOD-03 生成客户端工厂（签名 ``() -> client`` 或 ``(cfg, **kw) -> client``）。"""
    global _CLIENT_PROVIDER
    _CLIENT_PROVIDER = provider


def get_client_provider() -> Callable[..., Any] | None:
    return _CLIENT_PROVIDER


def _resolve_client(cfg: Any, *, run_id: str, client: Any, runs_root: Any = None) -> Any:
    """取 proposer 角色的 MOD-03 客户端（Issue #1）。

    组合根注入的通常是 ``RoleModelProvider``（有 ``client_for``）：这里按 **proposer**
    角色解析，于是「决策/反思用本地 8B、提案用 DeepSeek」只是配置差异。注入的是普通
    客户端（测试假件）时原样返回 —— 鸭子类型，不 import MOD-03 的提供者类型。

    ⚠️ **F36（2026-10-09 登记）**：``cfg`` 必须是**完整 ``AppConfig``**，因为组合根注入的
    provider（``app._role_provider``）会读 ``cfg.inference``。而本模块内部的 ``cfg``
    到处是 **``EvolveConfig`` 段**，早先直接把段传进这里 ⇒ 在没有注入 ``client`` 的回退
    路径上 ``AttributeError: 'EvolveConfig' object has no attribute 'inference'``（实测：
    ``loop run --agent heuristic`` + 已注册 provider）。故 ``propose`` 另收 ``app_config``
    并优先用它；``client`` 非空时本函数**短路**，真实 LLM 臂不受影响 —— 这也正是它一直
    藏着没被发现的原因。
    """
    resolved = client
    if resolved is None:
        if _CLIENT_PROVIDER is None:
            return None
        kwargs: dict[str, Any] = {"run_id": run_id}
        if runs_root is not None:
            kwargs["runs_root"] = Path(runs_root)
        try:
            resolved = _CLIENT_PROVIDER(cfg, **kwargs)
        except TypeError:
            try:
                resolved = _CLIENT_PROVIDER(cfg)
            except TypeError:
                resolved = _CLIENT_PROVIDER()
    # 鸭子类型解析（与 loop 的 ``client_for_role`` 同一约定）：MOD-07 不 import MOD-03。
    getter = getattr(resolved, "client_for", None)
    return getter("proposer") if callable(getter) else resolved


# =====================================================================
# 证据束构造（FR-07-2）
# =====================================================================

def harness_snapshot(harness: Path | str | None = None) -> HarnessSnapshot:
    """扫描 ``harness/`` 得到 ``version`` + 各文件 sha256（``source_tags`` 的 ``harness:current``）。

    只读**生效工件**（``prompts/`` / ``tools/`` / ``config.json`` / ``version.json``），
    不进 ``versions/``（历史快照不进证据束）。

    F38：额外带上**允许被候选修改**的文件的**正文**（``contents``）。候选是 unified diff，
    hunk 上下文必须与目标文件逐字符一致；早先只给 sha256，模型看不到任何一行原文，产出的
    diff 必然 ``E_EVOLVE_PATCH_INVALID``（实测：``hunk 上下文不匹配（decision.md 第 1 行）``）
    ⇒ harness 永远无法演化。只收白名单内的文件，并按 ``_MAX_HARNESS_BODY_CHARS`` 逐文件
    设上限（超限不内联，避免 prompt 无界增长）。
    """
    root = default_harness_dir(harness)
    files: list[tuple[str, str]] = []
    contents: list[tuple[str, str]] = []
    context: list[tuple[str, str]] = []
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if rel.startswith("versions/") or rel.startswith("."):
                continue
            try:
                raw_bytes = path.read_bytes()
            except OSError: # pragma: no cover - 读失败不阻塞证据束
                continue
            files.append((rel, hashlib.sha256(raw_bytes).hexdigest()))
            try:
                text = raw_bytes.decode("utf-8")
            except UnicodeDecodeError: # pragma: no cover - 非文本不进正文
                continue
            # 可改文件的正文（diff 的上下文来源）与只读上下文（死路记录）分开收：
            # 前者能在候选里被改，后者只能被读。超限一律只留 sha256（避免 prompt 无界）。
            if _is_context_path(f"harness/{rel}"):
                if len(raw_bytes) <= _MAX_CONTEXT_BODY_CHARS:
                    # **唯一的入口脱敏**：死路记录是自由文本（补丁 rationale + 判定 reason），
                    # 而 `assert_no_holdout_leak` 扫的是**证据束**（不只是渲染产物）——
                    # 一个偶然的 `heldout` 会让整代以 `E_EVAL_INV5_LEAK` 中止（PRD §10）。
                    # 落盘的 `dead_ends.md` 保持原文（审计要看模型当时写了什么）。
                    context.append((rel, _sanitize_agent_text(text)))
            elif (_is_allowed_path(f"harness/{rel}")
                  and len(raw_bytes) <= _MAX_HARNESS_BODY_CHARS):
                contents.append((rel, text))
    version = 0
    artifact_hash = ""
    version_file = root / "version.json"
    if version_file.is_file():
        try:
            raw = read_json(version_file)
        except InfraError:
            raw = None
        if isinstance(raw, Mapping):
            version = int(raw.get("version", 0) or 0)
            artifact_hash = str(raw.get("artifact_hash", "") or "")
    return HarnessSnapshot(version=version, files=tuple(files),
                           artifact_hash=artifact_hash, contents=tuple(contents),
                           context=tuple(context))


#: 反思条目进证据束的字段白名单（F35，2026-10-09 登记）。
#:
#: 原先这里拷的是**整条**记录（``dict(_plain(raw))``，含 ``entry.created_at`` /
#: ``reflection_id`` / ``run_id`` / ``prompt_tokens`` …），而 ``_render_items`` 会把整条
#: ``json.dumps`` 进 prompt、``assert_no_holdout_leak`` 再对全部字符串做数字扫描。
#: 现场（F35，2026-10-09）：``numeric_scan`` 当时占用的四位区间（2000-2099）里**日历年
#: 2026 正在其中**，于是一个 ISO 时间戳 ``"2026-10-09T16:34:13Z"`` 被判成 heldout seed
#: 字面量，整代以 ``E_EVAL_INV5_LEAK``（退出码 4）中止——闭环跑不起来。实测命中 3 处，
#: 全在 ``.reflections[i].entry.created_at``，内容字段零命中。
#: **F45-2（2026-10-11）起 heldout/test 已移到六位端口空间**，「年份撞区间」这一具体成因
#: 不再存在；但白名单仍是本模块的**契约**：它让「进入 agent 可见文本的字段集」保持**封闭**
#: （新字段默认不进，必须显式加入），从根上避免下一次数字空间变化重演同一类误判。
_REFLECTION_FIELDS: Final[tuple[str, ...]] = (
    "episode_key", "generation", "harness_version", "outcome", "truncation_reason",
    "status", "reason", "seed", "tick",
)
#: 反思正文（``entry`` 子对象）：失败归因、量表、tags 与正文本身才是 proposer 的依据。
_REFLECTION_ENTRY_FIELDS: Final[tuple[str, ...]] = (
    "text", "tags", "failure_cause", "outcome", "truncation_reason",
    "stages_cleared", "hearts_left", "battles_won", "decisions",
    "illegal_attempts", "fallback_decisions", "split",
)
#: 记忆条目白名单：情境/教训/动作提示 + 采纳统计。
#: 刻意排除 ``created_at``/``created_tick``/``expires_tick``/``embedding``/各种 hash 与
#: ``run_id``（同上：元数据会撞 heldout 数字区间，且对 harness 改进毫无信息量）。
_MEMORY_FIELDS: Final[tuple[str, ...]] = (
    "entry_id", "situation", "lesson", "action_hint", "tags", "polarity",
    "confidence", "utility", "n_adopted", "n_ref", "n_win_adopted",
    "n_win_not_adopted", "generation", "harness_version", "status",
)


def _project(payload: Mapping[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    """按**显式白名单**投影证据束条目（F35）。

    为什么是白名单而不是「排除 created_at 之类的黑名单」：证据束的文本会被
    ``_render_items`` 原样 ``json.dumps`` 进 proposer 的 prompt，而
    ``assert_no_holdout_leak`` 会对**全部**字符串做数字扫描。历史上（F35 现场）
    ``numeric_scan`` 的四位区间与日历年撞车，任何**新加**的元数据字段都可能再次撞上
    heldout 区间并把整代打死（fail-closed，退出码 4）；F45-2 换了六位端口空间后该具体
    成因消失，但「黑名单会漏、白名单封闭」这条工程结论不变。白名单让「进入 agent 可见
    文本的字段集」是**封闭**的：新字段默认不进，必须显式加入。姊妹函数
    ``_trajectory_summaries`` 本来就是这么写的，这里把三者对齐。
    """
    return {name: payload[name] for name in fields if name in payload}


def _memory_items(*, memory: Any, cfg: Any, run_id: str) -> tuple[dict[str, Any], ...]:
    """``memory.retrieve_for_evolution(top_k)``（FR-07-2）；接口缺失时降级为空。"""
    if memory is None:
        return ()
    top_k = int(getattr(cfg, "memory_top_k", 12) or 12)
    fn = getattr(memory, "retrieve_for_evolution", None)
    if not callable(fn):
        return ()
    try:
        raw = fn(top_k=top_k)
    except TypeError:
        raw = fn(top_k)
    except Exception as exc: # noqa: BLE001 - 记忆是增强项，不阻塞候选生成
        events.emit("evolve.propose", gen=0, n_candidates=0, error_code=str(
            getattr(exc, "code", E.E_IO_MEMORY_WRITE_FAILED)), detail="memory_retrieve")
        return ()
    items = getattr(raw, "entries", raw)
    out: list[dict[str, Any]] = []
    for item in list(items or ())[:top_k]:
        payload = _plain(item)
        if isinstance(payload, Mapping):
            projected = _project(payload, _MEMORY_FIELDS)
            projected["split"] = "train" # MOD-05 检索面只可能来自 train 局
            out.append(projected)
    return tuple(out)


def _trajectory_summaries(*, run_dir: Path | str | None, n_traj: int,
                          generation: int) -> tuple[dict[str, Any], ...]:
    """最近 ``n_traj`` 条 **train** 轨迹摘要（FR-07-2）。

    来源是 ``runs/<run_id>/episodes/train/*.jsonl`` 的 ``episode_end`` 行；只取
    ``generation <= g`` 的局（第 g 代只能用已发生的训练局）。文件缺失 → 空元组。
    """
    if run_dir is None:
        return ()
    root = Path(run_dir) / "episodes" / "train"
    if not root.is_dir():
        return ()
    rows: list[dict[str, Any]] = []
    from dice_ratchet.infra.io import read_jsonl

    for path in sorted(root.glob("*.jsonl")):
        try:
            lines, _ = read_jsonl(path)
        except InfraError: # pragma: no cover - 坏行不阻塞证据束
            continue
        end = next((r for r in lines if r.get("type") == "episode_end"), None)
        if not isinstance(end, Mapping):
            continue
        gen = int(end.get("generation", 0) or 0)
        if gen > int(generation):
            continue
        rows.append({
            "split": "train", "episode_key": str(end.get("episode_key", path.stem)),
            "generation": gen, "harness_version": int(end.get("harness_version", 0) or 0),
            "outcome": str(end.get("outcome", "lose")),
            "truncation_reason": end.get("truncation_reason"),
            "stages_cleared": int(end.get("stages_cleared", 0) or 0),
            "hearts_left": int(end.get("hearts_left", 0) or 0),
            "battles_won": int(end.get("battles_won", 0) or 0),
            "decisions": int(end.get("decisions", 0) or 0),
            "degraded": bool(end.get("degraded", False)),
        })
    rows.sort(key=lambda r: (int(r["generation"]), str(r["episode_key"])))
    return tuple(rows[-int(n_traj):]) if n_traj > 0 else ()


def _reflection_set(*, run_dir: Path | str | None, n_traj: int,
                    generation: int) -> tuple[dict[str, Any], ...]:
    """``reflection_set(split="train")``（FR-07-2）。

    MOD-06 把反思记录写在 ``runs/<run_id>/reflections/*.json``；只读 ``split=train``
    或未标 split（旧记录）的条目，且 ``generation <= g``。
    """
    if run_dir is None:
        return ()
    root = Path(run_dir) / "reflections"
    if not root.is_dir():
        return ()
    out: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        try:
            raw = read_json(path)
        except InfraError: # pragma: no cover
            continue
        if not isinstance(raw, Mapping):
            continue
        split = str(raw.get("split", "train"))
        if split != "train":
            continue
        gen = int(raw.get("generation", 0) or 0)
        if gen > int(generation):
            continue
        payload = dict(_plain(raw))
        entry = payload.get("entry")
        projected = _project(payload, _REFLECTION_FIELDS)
        if isinstance(entry, Mapping):
            projected["entry"] = _project(entry, _REFLECTION_ENTRY_FIELDS)
        projected["split"] = "train"
        out.append(projected)
    out.sort(key=lambda r: (int(r.get("generation", 0) or 0),
                            str(r.get("episode_key", ""))))
    return tuple(out[-int(n_traj):]) if n_traj > 0 else ()


def build_evidence_bundle(*, run_id: str, n_traj: int, cfg: Any,
                          memory: Any = None, run_dir: Path | str | None = None,
                          harness: Path | str | None = None,
                          generation: int = 0) -> EvidenceBundle:
    """证据束的**唯一**构造函数（FR-07-2）。

    四个来源全部被收敛到 train 侧：

    ===========================  ==========================================
    来源                           ``source_tags`` 取值
    ===========================  ==========================================
    ``memory.retrieve_for_evolution()``  ``memory:train``
    ``trajectory_summary(split="train")``  ``traj:train``
    ``reflection_set(split="train")``     ``reflect:train``
    ``harness_snapshot()``                ``harness:current``
    ===========================  ==========================================

    返回后必须经 ``assert_no_holdout_leak()`` 才能进 prompt（FR-07-3）。
    """
    if run_dir is None:
        root = Path("runs") / str(run_id)
        run_dir = root if root.is_dir() else None
    snap = harness_snapshot(harness)
    memory_items = _memory_items(memory=memory, cfg=cfg, run_id=run_id)
    trajectories = _trajectory_summaries(run_dir=run_dir, n_traj=int(n_traj),
                                         generation=int(generation))
    reflections = _reflection_set(run_dir=run_dir, n_traj=int(n_traj),
                                  generation=int(generation))
    tags: list[str] = ["harness:current"]
    if memory_items:
        tags.append("memory:train")
    if trajectories:
        tags.append("traj:train")
    if reflections:
        tags.append("reflect:train")
    tags.sort()
    draft = EvidenceBundle(
        bundle_id="", memory_items=memory_items, trajectory_summaries=trajectories,
        reflections=reflections, harness_snapshot=snap, source_tags=tuple(tags))
    return EvidenceBundle(
        bundle_id=f"ev-{draft.bundle_sha256[:12]}", memory_items=memory_items,
        trajectory_summaries=trajectories, reflections=reflections,
        harness_snapshot=snap, source_tags=tuple(tags))


# =====================================================================
# INV-5：生成侧防自我确认（FR-07-3）
# =====================================================================

def _heldout_seeds(cfg: Any) -> tuple[int, ...]:
    """收集注册表里要防的 heldout/test seed 整值（MOD-08 的协议是唯一出口）。"""
    seeds: set[int] = set()
    raw = getattr(cfg, "heldout_seeds", None)
    if raw:
        seeds.update(_seeds_from_spec(str(raw)))
    try:
        from dice_ratchet.evals.seeds import coerce_protocol, validate_protocol

        proto = validate_protocol(coerce_protocol(None))
        for name in ("heldout", "test"):
            split = proto.splits.get(name)
            if split is None:
                continue
            seeds.update(range(int(split.start), int(split.end) + 1))
    except Exception: # noqa: BLE001 - MOD-08 不可用时只用显式配置
        pass
    return tuple(sorted(seeds))


def _seeds_from_spec(spec: str) -> list[int]:
    """解析 ``"heldout:731000-731039"`` / ``"731000-731003,731008"`` 形态的 seed 规格。"""
    text = str(spec).split(":", 1)[-1].strip()
    out: list[int] = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            lo, _, hi = chunk.partition("-")
            try:
                out.extend(range(int(lo), int(hi) + 1))
            except ValueError:
                continue
        else:
            try:
                out.append(int(chunk))
            except ValueError:
                continue
    return out


def _seed_token_re(seed: int) -> re.Pattern[str]:
    """``(?<!\\d){seed}(?!\\d)``（FR-07-3 的正则；``20130`` / ``x2013y`` 不误报）。"""
    return re.compile(r"(?<!\d)" + str(int(seed)) + r"(?!\d)")


def _bundle_texts(bundle: EvidenceBundle) -> list[str]:
    """展平证据束的全部文本字段（含 harness 快照的路径与 sha）。"""
    payload = bundle.as_dict()
    texts: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            texts.append(node)
        elif isinstance(node, Mapping):
            for key in sorted(node):
                texts.append(str(key))
                walk(node[key])
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(payload)
    return texts


def assert_no_holdout_leak(bundle: EvidenceBundle, *, cfg: Any,
                           protocol: Any = None) -> None:
    """生成侧 INV-5 硬闸（FR-07-3）。命中即抛 ``E_EVAL_INV5_LEAK``（**致命**）。

    三道检查：
    1. 证据束条目的 ``split`` 必须全为 ``train``；
    2. 全部文本字段做 seed 字面量扫描（正则 ``(?<!\\d){seed}(?!\\d)``）；
    3. 拒绝出现 ``heldout`` / ``test`` 标签或 ``seeds.yaml`` 片段。
    """
    # ---- 1) split 必须全为 train -------------------------------------------------
    offenders: list[str] = []
    for group in ("memory_items", "trajectory_summaries", "reflections"):
        for index, item in enumerate(getattr(bundle, group)):
            if not isinstance(item, Mapping):
                continue
            split = item.get("split", "train")
            if str(split) != "train":
                offenders.append(f"{group}[{index}].split={split!r}")
    if offenders:
        raise HoldoutLeakError(
            f"证据束含非 train 条目：{offenders[:5]}",
            hint="INV-5：候选生成阶段物理上看不到 heldout/test 数据（FR-07-3）")

    # ---- 2) seed 字面量扫描 --------------------------------------------------------
    texts = _bundle_texts(bundle)
    for seed in _heldout_seeds(cfg):
        pattern = _seed_token_re(seed)
        for text in texts:
            if pattern.search(text):
                raise HoldoutLeakError(
                    f"证据束文本命中 heldout/test seed 字面量 {seed}",
                    hint="INV-5：heldout seed 不得进入任何 agent 可见出口；"
                         "命中即整代中止并写 audit 告警（PRD §10）")

    # ---- 3) 禁用词 / seeds.yaml 片段 ------------------------------------------------
    forbidden = ("heldout", "seeds.yaml", "test_full", "H1", "H2", "H3")
    for text in texts:
        lowered = text.lower()
        for word in forbidden:
            token = word.lower()
            if token == "h1" or token == "h2" or token == "h3":
                if re.search(r"(?<![0-9A-Za-z_])" + token + r"(?![0-9A-Za-z_])", lowered):
                    raise HoldoutLeakError(
                        f"证据束文本命中 heldout 篮子标签 {word!r}",
                        hint="INV-5：篮子标签属 heldout 侧上下文（FR-07-3）")
                continue
            if token in lowered:
                raise HoldoutLeakError(
                    f"证据束文本命中禁用词 {word!r}",
                    hint="INV-3/INV-5：proposer 上下文只允许 train 侧内容")


# =====================================================================
# prompt 渲染（FR-07-3 / INV-3）
# =====================================================================

_SYSTEM_TEMPLATE = """你是 DiceRatchet 的 harness 改进提案者（proposer）。

你的唯一职责：根据下面给出的**训练侧**证据，提出对 harness 工件的**最小**改进。

硬约束（违反即被静态扫描拒绝，提案作废）：
1. 只能改这些文件：{allowed_files}；
2. 改动面 ≤ {max_lines} 行、≤ {max_chars} 字符，最多 {max_files} 个文件；
3. 禁止新增第三方依赖；顶层 import 只允许 {allowed_imports}；
4. 禁止网络、文件写、subprocess、读环境变量、访问 dice_ratchet.env.* / dice_ratchet.sandbox.*；
5. 禁止改动 {frozen} 的签名与 @harness_tool 的语义；
6. 输出必须是 unified diff（`--- a/<path>` / `+++ b/<path>` / `@@`），路径相对 harness/；
7. `config_patch` **只能**改这些点分键：{evolvable_keys}（改任何别的键 → 作废）；
8. `rationale` 长度 ≤ {rationale_max} 字符（按字符数，不是词数）——这个上限是**硬**的，
   超一个字符即作废，请按 ≤ {rationale_guide} 字符写以留余量；
9. 只输出一个 JSON 对象，字段见 user 消息的输出契约。
10. `diff` 的上下文行必须与上面给出的文件正文**逐字符一致**；行号数不准没关系
    （应用时会按上下文自动定位），但正文抄错就作废。
11. `## dead_ends` 段是**只读**的：那里列出的改动**已经被否决过**（含被否决的原因）。
    不得提交与其中任一条**同一文件 + 同一意图**的改动；必须换一个改动面或换一种机制，
    并在 `rationale` 里说明「与 dead_ends 里哪一条不同、为什么这次能成」。

你看不到留出集数据，也不需要它们。"""

#: ``config_patch`` 的**最小合法样例**（F-M3-4，2026-10-18）。
#:
#: m3r4 cycle 1 的失败现场：模型想改 ``retrieval.top_k``，但把整份 ``config.json``
#: 写成 ``{"retrieval": {"top_k": 3}}``，而系统提示只说了「只能改点分键」、没给具体
#: 形态 ⇒ 旧校验器把容器键 ``retrieval`` 判成非法。这里补一个可直接照抄的样例，
#: 并明确 ``harness/config.json`` 只收**扁平白名单键**（嵌套对象会被配置加载器拒绝）。
_CONFIG_PATCH_EXAMPLE = """## `config_patch` 的最小合法样例（只改可演化键）
--- a/harness/config.json
+++ b/harness/config.json
@@ -1 +1 @@
-{}
+{"memory.retrieve.top_k": 3}

`harness/config.json` 只接受**扁平点分键**，且顶层前缀必须是 `agent.policy.` /
`memory.retrieve.` / `agent.tools.`。概念名与实际键的对应：
`retrieval.top_k` → `memory.retrieve.top_k`，`sampling.temperature` →
`agent.policy.temperature`。禁止嵌套对象（如 `+{ "retrieval": { "top_k": 3 } }`）——
配置加载会拒绝它。"""

#: 静态校验全灭后的**重试提醒**（F-M3-4，2026-10-18）。
#:
#: 固定文案只陈述「上一轮没通过静态校验」这一事实与已有约束；调用方还会追加本轮
#: 校验器给出的拒绝原因（仅含候选自身的路径/键名），两者都不注入证据束之外的
#: 数据（INV-3/FR-07-3）。
_RETRY_NOTE = """

## 重试提醒
上一次提案没有通过静态校验（路径 / 改动面 / `config_patch` 键名等）。请重新逐条核对
上面的硬约束与 `config_patch` 样例，只输出输出契约要求的 JSON 对象。"""

_TASK_BLOCK = """## 任务
提出 {n_candidates} 个**互相不同**的候选改动，覆盖不同的改动面（prompt / tool / config）。
每个候选必须引用证据条目 id 作为理由依据。"""

_OUTPUT_CONTRACT = """## 输出契约（只输出这个 JSON 对象）
{"candidates": [{"kind": "prompt_patch|tool_patch|config_patch",
                 "touched_files": ["harness/prompts/decision.md"],
                 "diff": "--- a/harness/prompts/decision.md\\n+++ b/harness/prompts/decision.md\\n@@ ...",
                 "rationale": "≤300 字，必须引用 evidence 条目 id",
                 "expected_effect": ["win_rate", "up", 2]}]}"""


def render_proposer_prompt(bundle: EvidenceBundle, cfg: Any) -> tuple[str, str]:
    """产出 ``(system, user)``（FR-07-3：模板化，**不允许**插入证据束之外的上下文）。

    回归判据（PRD §11-4）：system+user 里出现的 ``{{...}}`` 占位符必须 ⊆
    ``PROMPT_PLACEHOLDERS``，且全文不含 ``heldout`` / ``test`` 子串。

    ⚠️ **F38 的例外（范围界定）**：``harness`` 段会**原样内联**可改文件的正文，而
    ``prompts/decision.md`` 本身就是**给 agent 的模板**（含 ``{{task}}``/``{{observation}}``
    …）。那些占位符是**要模型看到的目标文本**（diff 的上下文行必须逐字符一致），
    不是本函数模板的未替换槽位，故「无未替换 ``{{...}}``」这条**只适用于正文块之外的区域**。
    """
    allowed_files = "、".join(sorted(_allowed_file_specs()))
    allowlist = getattr(cfg, "tool_import_allowlist", _DEFAULT_IMPORT_ALLOWLIST)
    # F40：`config_patch` 可改的键必须与校验器用**同一份**配置（`evolve.evolvable_keys`），
    # 否则模型只能盲猜键名 —— 实测首轮 try 就因 `config_key_not_evolvable` 作废。
    evolvable_keys = "、".join(sorted(str(k) for k in
                                      (getattr(cfg, "evolvable_keys", ()) or ()))) or "（无）"
    system = _SYSTEM_TEMPLATE.format(
        allowed_files=allowed_files,
        max_lines=int(getattr(cfg, "max_diff_lines", 40)),
        max_chars=int(getattr(cfg, "max_diff_chars", 4000)),
        max_files=int(_MAX_FILES),
        evolvable_keys=evolvable_keys,
        rationale_max=_RATIONALE_MAX_CHARS,
        rationale_guide=_RATIONALE_GUIDE_CHARS,
        allowed_imports="、".join(sorted(str(a) for a in allowlist)),
        frozen=FROZEN_SIGNATURE)
    # F-M3-4：把合法形态放在**约束区**（system），而不是输出契约里，避免与
    # 「只输出一个 JSON 对象」的契约文本混在一起。
    system = system + "\n\n" + _CONFIG_PATCH_EXAMPLE

    task = _TASK_BLOCK.format(n_candidates=int(getattr(cfg, "n_candidates", 1)))
    memory_block = _render_items(bundle.memory_items, empty="（无可用策略记忆）")
    traj_block = _render_items(bundle.trajectory_summaries, empty="（无可用轨迹摘要）")
    reflect_block = _render_items(bundle.reflections, empty="（无可用反思）")
    harness_block = _render_harness(bundle.harness_snapshot)
    dead_ends_block = _render_dead_ends(bundle.harness_snapshot)

    # 占位符与证据束一一对应：observation→traj 摘要，memory→记忆，harness→快照，task→任务
    user = "\n\n".join((
        task,
        "## observation（最近 train 轨迹摘要）\n" + traj_block,
        "## memory（train 策略记忆）\n" + memory_block,
        "## reflections（train 反思）\n" + reflect_block,
        "## harness（当前生效工件的 sha256）\n" + harness_block,
        # D1=A（2026-10-14）：**只读**的死路记录段。位置紧邻 harness 段：模型读到「当前
        # 工件长什么样」之后立刻读到「哪些改动已经被否决、为什么」。
        "## dead_ends（**只读**：已被否决的改动，不得重复提交）\n" + dead_ends_block,
        "## evidence 条目 id（rationale 必须引用其中之一）\n" + _render_evidence_ids(bundle),
        _OUTPUT_CONTRACT,
    ))
    assert_prompt_is_clean(system, user)
    return system, user


def assert_prompt_is_clean(*texts: str) -> None:
    """INV-3 回归：prompt 里不得出现留出集侧标签（FR-07-3）。

    与 ``assert_no_holdout_leak`` 的区别：那条查**证据束**，这条查**渲染产物**——
    模板自己写错一个词就会把 heldout 上下文带进 proposer（PRD §11-4 的验收点）。
    """
    forbidden = ("heldout", "seeds.yaml", "H1", "H2", "H3", "test_full")
    for text in texts:
        lowered = str(text).lower()
        for word in forbidden:
            token = word.lower()
            if token.startswith("h") and token[1:].isdigit():
                hit = re.search(r"(?<![0-9a-z_])" + token + r"(?![0-9a-z_])", lowered)
            else:
                hit = token in lowered
            if hit:
                raise HoldoutLeakError(
                    f"proposer prompt 出现留出集侧标签 {word!r}",
                    hint="INV-3：proposer 上下文只能由证据束白名单构造（FR-07-3）")


def _render_items(items: Sequence[Any], *, empty: str) -> str:
    if not items:
        return empty
    lines: list[str] = []
    for index, item in enumerate(items):
        payload = dict(_plain(item)) if isinstance(item, Mapping) else {"value": str(item)}
        lines.append(f"- [{index}] " + json.dumps(payload, ensure_ascii=False,
                                                 sort_keys=True))
    return "\n".join(lines)


def _render_harness(snap: HarnessSnapshot) -> str:
    """渲染 harness 段：先给指纹（version/artifact_hash/各文件 sha256），再给**正文**。

    F38：正文是 unified diff 能对齐的前提（hunk 上下文必须逐字符一致）。只渲染
    ``_is_allowed_path`` 白名单内且未超限的文件（见 ``harness_snapshot``）。

    D1=A（2026-10-14）：``snap.context``（死路记录）渲染在**单独一段**里，并**显式声明
    只读**——模型必须知道「这些改动已经被否决过」，同时不得把它们当成 diff 的目标。
    """
    lines = [f"- version: {int(snap.version)}",
             f"- artifact_hash: {snap.artifact_hash or '(未计算)'}"]
    for name, digest in snap.files:
        if name == "version.json":
            continue
        lines.append(f"- {name}: sha256:{digest[:16]}")
    if not snap.contents:
        return "\n".join(lines)
    lines.append("")
    lines.append("### 可修改文件的当前正文（diff 的上下文行必须与它逐字符一致）")
    for name, text in snap.contents:
        lang = "python" if name.endswith(".py") else ("json" if name.endswith(".json") else "markdown")
        lines.append("")
        lines.append(f"#### `harness/{name}`")
        lines.append(f"```{lang}")
        lines.append(text.rstrip("\n"))
        lines.append("```")
    return "\n".join(lines)


def _render_dead_ends(snap: HarnessSnapshot) -> str:
    """渲染**只读**的死路记录段（D1=A，2026-10-14）。

    两个必须的处理：

    1. **脱敏**（``_sanitize_agent_text``）：死路记录里的文本源自补丁的 ``rationale``
       与判定 ``reason``，属**自由文本**。而 ``assert_no_holdout_leak`` /
       ``assert_prompt_is_clean`` 对**全部**渲染文本执行 INV-3/INV-5 的禁用词扫描
       —— 一个偶然出现的 ``heldout`` 会让整代以 ``E_EVAL_INV5_LEAK`` 中止（PRD §10）。
       在**渲染处**（唯一入口）统一替换，落盘原文保持可审计。
    2. **封顶**：``_MAX_CONTEXT_BODY_CHARS`` 字节，超限保留**最近**的条目（新的失败比
       旧的有价值），并在段首注明被截断。
    """
    entries = [(name, text) for name, text in snap.context if text.strip()]
    if not entries:
        return "（暂无被否决的改动记录）"
    lines: list[str] = []
    for _name, text in entries:
        body = _sanitize_agent_text(text)
        if len(body) > _MAX_CONTEXT_BODY_CHARS:
            kept = body[-_MAX_CONTEXT_BODY_CHARS:]
            body = "（…更早的条目已省略）\n" + kept
        lines.append(body.rstrip("\n"))
    return "\n\n".join(lines)


#: agent 可见文本里必须替换掉的禁用词（与 ``assert_no_holdout_leak`` /
#: ``assert_prompt_is_clean`` 的清单**同源**；那两处的扫描是 fail-closed，这里是唯一的
#: 预防点）。大小写不敏感；``H1/H2/H3`` 按词边界匹配（避免误伤 ``H1N1`` 之类的正常词）。
_AGENT_TEXT_REDACTIONS: Final[tuple[str, ...]] = (
    "heldout", "seeds.yaml", "test_full",
)


def _sanitize_agent_text(text: str) -> str:
    """把自由文本里的留出集侧标签替换为 ``[已屏蔽]``（INV-3/INV-5 的唯一预防点）。"""
    out = str(text)
    for word in _AGENT_TEXT_REDACTIONS:
        out = re.sub(re.escape(word), "[已屏蔽]", out, flags=re.IGNORECASE)
    for word in ("H1", "H2", "H3"):
        out = re.sub(r"(?<![0-9A-Za-z_])" + word + r"(?![0-9A-Za-z_])", "[已屏蔽]", out)
    return out


def _render_evidence_ids(bundle: EvidenceBundle) -> str:
    ids: list[str] = [f"harness:current#{bundle.harness_snapshot.version}"]
    ids += [f"memory:train#{i}" for i in range(len(bundle.memory_items))]
    ids += [f"traj:train#{i}" for i in range(len(bundle.trajectory_summaries))]
    ids += [f"reflect:train#{i}" for i in range(len(bundle.reflections))]
    return "\n".join(f"- {value}" for value in ids)


def candidate_schema_dict() -> dict[str, Any]:
    """``propose`` 的 LLM 输出 schema（与 ``CANDIDATE_SCHEMA`` 同源，包一层 envelope）。"""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["candidates"],
        "properties": {
            "candidates": {
                "type": "array", "minItems": 0,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "diff", "rationale", "expected_effect"],
                    "properties": {
                        "kind": {"enum": list(CANDIDATE_SCHEMA["properties"]["kind"]["enum"])},
                        "diff": {"type": "string"},
                        "touched_files": {"type": "array", "items": {"type": "string"}},
                        "rationale": {"type": "string"},
                        "expected_effect": CANDIDATE_SCHEMA["properties"]["expected_effect"],
                    },
                },
            },
        },
    }


# =====================================================================
# 改动面硬约束（FR-07-4；纯函数）
# =====================================================================

_MAX_FILES: Final[int] = 2
#: F37：**提案**（harness 补丁）的输出预算，按调用方交给 MOD-03（``generate_json`` 的
#: ``max_tokens``）。**不得**改为抬高全局 ``inference.max_tokens``——那会打断小窗口模型
#: （``guard_context`` 按 ``est_prompt + max_tokens ≤ context_length`` 判定；``mock``
#: 4096 实测 71 例 ``E_LLM_CONTEXT_OVERFLOW``）。
#: 取值依据：实测约 500 completion token 足够一条候选（diff + rationale），且
#: ``max_diff_chars=4000`` 的满额 diff 仍有余量；2048 ≈ 实测需求 × 4。
#: 与提案 prompt（内联 harness 正文后 ≈2.6k token）相加后仍在 8B（8192）与
#: ``deepseek-flash``（1M）的窗口内。
_PROPOSE_MAX_TOKENS: Final[int] = 2048
#: F40：`rationale` 的硬上限**取自候选 JSON Schema 本身**（`maxLength`），
#: 于是「prompt 里告诉模型的上限」「schema 声明」「校验器判定」三者不可能漂移。
_RATIONALE_MAX_CHARS: Final[int] = int(
    CANDIDATE_SCHEMA["properties"]["rationale"]["maxLength"])
#: prompt 里建议的目标值：比硬上限小 20 字符，吸收模型的轻微超额
#: （实测首轮 `deepseek-flash` 写了 322 → `rationale_too_long:322` 作废）。
_RATIONALE_GUIDE_CHARS: Final[int] = _RATIONALE_MAX_CHARS - 20
#: F39：hunk 定位时允许的**行偏移**搜索窗口（±N 行，逐字匹配、无 fuzz）。
#: 标准 ``patch`` 的 offset 语义。取 200：远大于 harness 文件规模（当前最大 57 行），
#: 又不至于在大文件里误命中远处同文。
_MAX_HUNK_OFFSET: Final[int] = 200
#: F38：单个 harness 文件内联进 prompt 的正文上限（字节）。当前 harness 最大文件
#: ``prompts/decision.md`` ≈3.1 KB，故 20 KB 有充足余量；超限只留 sha256 摘要，
#: 避免 prompt 无界增长（``tools/*.py`` 未来可能变大）。上限内联不做截断——
#: 截断会让 diff 的上下文行失真，比不内联更糟。
_MAX_HARNESS_BODY_CHARS: Final[int] = 20000
#: D1=A（2026-10-14）：``harness/dead_ends.md`` 内联进提案 prompt 的正文上限。
#: 死路记录是**累积**的（每代每条候选一行），必须封顶，否则提案 prompt 会逐代膨胀。
#: 超限时**保留最近的部分**（见 ``_render_dead_ends``）——最近的失败信息最有价值。
_MAX_CONTEXT_BODY_CHARS: Final[int] = 8000


def _allowed_file_specs() -> tuple[str, ...]:
    return (DECISION_PROMPT, REFLECTION_PROMPT, CONFIG_FILE, TOOLS_GLOB)


def _context_file_specs() -> tuple[str, ...]:
    """**只读**上下文文件（渲染进提案 prompt，但候选不得改动）。"""
    return (DEAD_ENDS_FILE,)


def _is_context_path(path: str) -> bool:
    return _rel_under_harness(path) == DEAD_ENDS_FILE


def _is_allowed_path(path: str) -> bool:
    text = _rel_under_harness(path)
    if ".." in text.split("/"):
        return False
    if text in (DECISION_PROMPT, REFLECTION_PROMPT, CONFIG_FILE):
        return True
    return text.startswith("harness/tools/") and text.endswith(ALLOWED_TOOL_EXT)


def _rel_under_harness(path: str) -> str:
    """路径归一到 ``harness/`` 根下的相对形式（去 ``a/`` ``b/`` 前缀与 ``./``）。

    ``harness/prompts/decision.md`` / ``b/harness/prompts/decision.md`` /
    ``./harness/prompts/decision.md`` 归一到同一个键；``prompts/decision.md``
    也接受（diff 头允许以 harness/ 为根书写）。
    """
    text = str(path).replace("\\", "/")
    while text.startswith(("./", "a/", "b/")):
        text = text[2:]
    if not text.startswith("harness/"):
        text = "harness/" + text
    return text


def _is_forbidden_path(path: str) -> bool:
    text = _rel_under_harness(path)
    lowered = text.lower()
    if lowered.startswith("harness/versions") or lowered.startswith("versions/"):
        return True
    if lowered.endswith("version.json"):
        return True
    return any(lowered == pat or lowered.startswith(pat.rstrip("/") + "/")
               for pat in _FORBIDDEN_PATH_PATTERNS)


def _added_lines(diff: str) -> list[str]:
    out: list[str] = []
    for line in str(diff).replace("\r\n", "\n").split("\n"):
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            out.append(line[1:])
    return out


def _context_lines(diff: str) -> list[str]:
    """unified diff 的上下文行（`` ``` ``` 开头但不是文件头/hunk 头）。"""
    out: list[str] = []
    for line in str(diff).replace("\r\n", "\n").split("\n"):
        if not line.startswith(" "):
            continue
        if line.startswith("---") or line.startswith("+++"):
            continue
        out.append(line[1:])
    return out


def _removed_lines(diff: str) -> list[str]:
    out: list[str] = []
    for line in str(diff).replace("\r\n", "\n").split("\n"):
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("-"):
            out.append(line[1:])
    return out


def _touched_paths(cand: Candidate) -> tuple[str, ...]:
    """候选碰到的文件（``touched_files`` 优先，缺失时从 diff 头解析）。"""
    declared = tuple(_rel_under_harness(p) for p in cand.touched_files if str(p).strip())
    parsed = tuple(_rel_under_harness(p) for p in parse_diff_paths(cand.diff))
    merged: list[str] = []
    for path in (*declared, *parsed):
        if path and path not in merged:
            merged.append(path)
    return tuple(merged)


def _validate_diff_shape(cand: Candidate, violations: list[str]) -> None:
    diff = str(cand.diff)
    if not diff.strip():
        violations.append("diff_empty")
        return
    if "+++" not in diff or "@@" not in diff:
        violations.append("diff_not_unified")
    for line in diff.split("\n"):
        if line.startswith(("---", "+++")):
            raw = line[3:].strip().split("\t", 1)[0]
            if raw.startswith(("a/", "b/")):
                raw = raw[2:]
            if raw == "/dev/null":
                violations.append("diff_creates_or_deletes_file")
                continue
            if ".." in raw.split("/"):
                violations.append(f"diff_path_traversal:{raw}")


def _validate_imports(cand: Candidate, cfg: Any, violations: list[str],
                      tool_paths: Sequence[str]) -> None:
    allow = set(str(a) for a in getattr(cfg, "tool_import_allowlist",
                                        _DEFAULT_IMPORT_ALLOWLIST))
    local = {Path(p).stem for p in tool_paths}
    text = "\n".join(_added_lines(cand.diff))
    for match in _IMPORT_RE.finditer(text):
        module = str(match.group(1) or match.group(2) or "")
        root = module.split(".", 1)[0]
        if not root or root in allow or root in local:
            continue
        if root.startswith("dice_ratchet"):
            violations.append(f"import_layer:{module}")
            continue
        violations.append(f"third_party_import:{module}")


def _validate_signature(cand: Candidate, violations: list[str]) -> None:
    """签名/装饰器语义冻结（FR-07-4；MAL-7 的绕道也要挡住）。"""
    changed = "\n".join((*_added_lines(cand.diff), *_removed_lines(cand.diff)))
    for name in _DEF_RE.findall(changed):
        if name == FROZEN_SIGNATURE:
            violations.append(f"signature_changed:{FROZEN_SIGNATURE}")
    for name in _LAMBDA_ASSIGN_RE.findall(changed):
        if name == FROZEN_SIGNATURE:
            violations.append(f"signature_bypassed:{FROZEN_SIGNATURE}=lambda")
    # 删除 ``@harness_tool`` 装饰器 = 改变工具注册语义
    removed = "\n".join(_removed_lines(cand.diff))
    added = "\n".join(_added_lines(cand.diff))
    if _DECORATOR_RE.search(removed) and not _DECORATOR_RE.search(added):
        violations.append("harness_tool_decorator_removed")


def _validate_banned_calls(cand: Candidate, violations: list[str]) -> None:
    text = "\n".join(_added_lines(cand.diff))
    for pattern, label in _BANNED_CALL_PATTERNS:
        if re.search(pattern, text):
            violations.append(f"banned_call:{label}")


def _validate_seed_literals(cand: Candidate, cfg: Any, violations: list[str]) -> None:
    """新增行里出现 heldout/test seed 整值 → 拒绝（MAL-5 的预读作弊，INV-5）。

    只扫 ``+`` 行：上下文行里的历史数字不是候选引入的。命中即 ``heldout_seed_literal``
    （把 heldout 数据硬编码进 harness 等于把留出集带进 agent 上下文）。
    """
    text = "\n".join(_added_lines(cand.diff))
    if not text:
        return
    for seed in _heldout_seeds(cfg):
        if _seed_token_re(seed).search(text):
            violations.append(f"heldout_seed_literal:{seed}")
            return # 一条足够（reason 只取首个）


def _validate_tool_patch(cand: Candidate, tool_paths: Sequence[str],
                         violations: list[str]) -> None:
    """``tool_patch`` 必须落在 ``harness/tools/*.py`` 且不掏空 @harness_tool 语义。"""
    if not tool_paths:
        violations.append("tool_patch_without_tool_file")
        return
    # 装饰器语义由 ``_validate_signature`` 的"删除 @harness_tool 且不补回"守住；
    # 这里只要求 diff 至少改动到工具源码本身（``_validate_diff_shape`` 已排除
    # ``/dev/null`` 的新建/删除），因此不做"新增行里必须出现装饰器"的过强校验
    # （合法的最小改动可能只改一行函数体，diff 上下文不含装饰器）。


def _json_leaf_paths(value: Any, prefix: str = "") -> set[str]:
    """把可解析的 JSON 对象展开成**叶子**点分路径（数组/标量都算叶子）。

    仅供 ``config_patch`` 的诊断信息使用：真正写进 ``harness/config.json`` 的键必须
    是**扁平点分键**（见 ``HARNESS_CONFIG_PREFIXES``），嵌套对象会被配置加载器拒绝。
    """
    if isinstance(value, Mapping) and value:
        paths: set[str] = set()
        for key, child in value.items():
            name = str(key)
            paths |= _json_leaf_paths(child, f"{prefix}.{name}" if prefix else name)
        return paths
    return {prefix} if prefix else set()


#: ``harness/config.json`` 允许的顶层前缀（与 ``infra.config`` 的加载器同一口径）。
#: 在这里提前判一遍，避免「校验通过、配置加载时才 ``E_IO_CONFIG_INVALID``」的候选
#: 白跑一代。
HARNESS_CONFIG_PREFIXES: Final[tuple[str, ...]] = tuple(HARNESS_CONFIG_ALLOWED_PREFIXES)

#: ``evolve.evolvable_keys`` 用的是**概念名**（PRD 口径），而 ``harness/config.json``
#: 必须写实际路径。两边的等价关系只在这里定义一次。
#: 2026-10-18（F-M3-5）：删除了 ``"prompt."`` → ``agent.policy.`` 这一条——概念白名单里
#: 已经没有 ``prompt.*`` 键（原 ``prompt.show_intent`` 无 ``PolicyConfig`` 字段，已从白名单
#: 移除）。保留它只会让 ``agent.policy.show_intent`` 折回一个不存在的概念名。
CONFIG_KEY_ALIASES: Final[dict[str, str]] = {
    "retrieval.": "memory.retrieve.",
    "sampling.": "agent.policy.",
}


def _actual_config_key(key: str) -> str:
    """概念名 → ``harness/config.json`` 实际扁平键（已是实际键则原样返回）。"""
    for alias, target in CONFIG_KEY_ALIASES.items():
        if key == alias.rstrip(".") or key.startswith(alias):
            return target + key[len(alias):]
    return key


def _canonical_config_key(key: str, evolvable: set[str]) -> str:
    """实际扁平键 → 概念名（``memory.retrieve.top_k`` → ``retrieval.top_k``）。

    反向映射**可能**有歧义（若将来一个实际前缀对应多个概念前缀，如
    ``sampling.*`` / ``prompt.*`` 都映射到 ``agent.policy.*``），用 ``evolvable``
    消歧：只认白名单里真实存在的那个概念名。2026-10-18（F-M3-5）起
    ``prompt.*`` 已不在白名单，别名表里只剩一对一映射，这里的消歧逻辑保留为防御。
    """
    for alias, target in CONFIG_KEY_ALIASES.items():
        if key == target.rstrip(".") or key.startswith(target):
            candidate = alias + key[len(target):]
            if candidate in evolvable:
                return candidate
    return key


def _is_harness_config_key(key: str) -> bool:
    return any(key == prefix.rstrip(".") or key.startswith(prefix)
               for prefix in HARNESS_CONFIG_PREFIXES)


def _validate_config_patch(cand: Candidate, cfg: Any, violations: list[str]) -> None:
    """``config_patch`` 只能改 ``evolve.evolvable_keys`` 声明的键（FR-07-4）。

    F-M3-4（2026-10-18，m3r4 cycle 1）：候选把 ``config.json`` 写成
    ``{"retrieval": {"top_k": 3}}``（嵌套对象），旧实现按 ``_JSON_KEY_RE`` 抽出
    ``retrieval`` 与 ``top_k``，把**容器键** ``retrieval`` 判成非法，整条候选作废
    （``n_candidates=0`` ⇒ 整代 `no_candidate`）。但嵌套对象本身也不是
    ``harness/config.json`` 的合法形态：加载器要求**扁平点分键**且顶层前缀必须在
    ``agent.policy.*`` / ``memory.retrieve.*`` / ``agent.tools.*`` 内（``infra.config``）。
    因此这里改成：

    1. 整对象 JSON：嵌套值 ⇒ ``config_patch_nested_object``（可操作地告诉模型写扁平键）；
    2. 扁平键：必须同时满足「加载器前缀」与「可演化键」（完整路径或末段短名）；
    3. 逐行片段：按同一规则判正则抽出的键。

    这样 ``{"memory.retrieve.top_k": 3}`` 通过，``{"retrieval": {"top_k": 3}}`` /
    ``{"retrieval.top_k": 3}`` / ``{"seed": 7}`` 都被拒绝（后两者在加载器那边也会失败）。

    F-M3-5（2026-10-18）：概念白名单里的 ``prompt.show_intent`` 已删除（无对应
    ``PolicyConfig`` 字段），别名表也去掉了 ``prompt.`` 前缀；``agent.policy.show_intent``
    现在走通用路径被 ``config_key_not_evolvable`` 拒绝，不再需要专门的
    ``config_key_unimplemented`` 分支。
    """
    evolvable = {str(k) for k in getattr(cfg, "evolvable_keys", ())}
    if not evolvable:
        violations.append("config_patch_without_evolvable_keys")
        return
    text = "\n".join(_added_lines(cand.diff))
    parsed: Any = None
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, Mapping):
        nested = sorted(str(k) for k, value in parsed.items()
                        if isinstance(value, Mapping))
        if nested:
            # 只把**能通过加载器**的叶子当作改写示例；否则（如已下线的
            # ``prompt.show_intent``）宁可不说，也不要给一个同样非法的键名。
            leaves = sorted({_actual_config_key(path)
                             for path in _json_leaf_paths(parsed)})
            usable = [leaf for leaf in leaves if _is_harness_config_key(leaf)]
            example = f"，例如 {usable[:1]}" if usable else ""
            violations.append(
                f"config_patch_nested_object:{nested}"
                f"（请改写成扁平点分键{example}；"
                "harness/config.json 只接受 agent.policy.* / memory.retrieve.* / "
                "agent.tools.*）")
            return
        keys = {str(k) for k in parsed}
    else:
        keys = {m.group(1) for m in _JSON_KEY_RE.finditer(text)}
        if not keys:
            # 非 JSON 形态的 diff（例如整文件替换）→ 退化为子串匹配
            keys = {k for k in evolvable if k in text}
            if not keys:
                violations.append("config_patch_no_evolvable_key")
                return
    illegal = sorted(k for k in keys
                     if not _is_harness_config_key(k)
                     or not _key_is_evolvable(k, evolvable))
    if illegal:
        violations.append(f"config_key_not_evolvable:{illegal}")


def _key_is_evolvable(key: str, evolvable: set[str]) -> bool:
    """``config.json`` 的键允许两种写法：概念名的完整路径，或**裸末段短名**。

    ``evolvable_keys`` 用 Spec §5 的概念路径（``retrieval.top_k``），而实际写进
    ``harness/config.json`` 的键是 ``memory.retrieve.top_k``（加载器前缀 + 概念别名）。
    因此这里先把实际键折回概念名再比；**裸末段**（``top_k``）仍接受，兼容逐行 diff
    片段里只出现叶子名的情况。带点的未知路径不再靠「末段碰巧相同」放行
    （``agent.policy.foo.top_k`` 必须拒绝——它在加载器那边也会因未知键失败）。
    """
    if key in evolvable:
        return True
    if _canonical_config_key(key, evolvable) in evolvable:
        return True
    if "." in key:
        return False
    return any(spec.rsplit(".", 1)[-1] == key for spec in evolvable)


def validate_candidate(cand: Candidate, *, cfg: Any) -> ValidationResult:
    """FR-07-4 的改动面硬约束（纯函数；不落盘、不调模型）。

    检查顺序固定（保证 ``reason`` 确定性）：schema → 路径 → 行/字符数 → 依赖 →
    签名 → 禁用调用 → 按 ``kind`` 的专项约束。
    """
    violations: list[str] = []
    paths = _touched_paths(cand)
    lines = count_diff_lines(cand.diff)
    chars = len(str(cand.diff))

    # ---- schema 形状 ---------------------------------------------------------------
    if cand.kind not in CANDIDATE_SCHEMA["properties"]["kind"]["enum"]:
        violations.append(f"unknown_kind:{cand.kind}")
    if len(str(cand.rationale)) > _RATIONALE_MAX_CHARS:
        violations.append(f"rationale_too_long:{len(str(cand.rationale))}")
    if not str(cand.rationale).strip():
        violations.append("rationale_empty")
    _validate_diff_shape(cand, violations)

    # ---- 声明与 diff 一致（防止 touched_files 掩盖真实改动面）------------------------
    declared = {_rel_under_harness(p) for p in cand.touched_files if str(p).strip()}
    parsed = {_rel_under_harness(p) for p in parse_diff_paths(cand.diff)}
    if declared and parsed and declared != parsed:
        violations.append(
            f"declared_files_mismatch:{sorted(declared ^ parsed)}")

    # ---- 文件白名单 / 禁止路径 ------------------------------------------------------
    if not paths:
        violations.append("touched_files_empty")
    if len(paths) > _MAX_FILES:
        violations.append(f"too_many_files:{len(paths)}>{_MAX_FILES}")
    for path in paths:
        if _is_forbidden_path(path):
            violations.append(f"forbidden_path:{path}")
        elif not _is_allowed_path(path):
            violations.append(f"path_not_in_whitelist:{path}")

    # ---- 改动面硬上限 ---------------------------------------------------------------
    max_lines = int(getattr(cfg, "max_diff_lines", 40))
    max_chars = int(getattr(cfg, "max_diff_chars", 4000))
    if lines > max_lines:
        violations.append(f"diff_lines:{lines}>{max_lines}")
    if chars > max_chars:
        violations.append(f"diff_chars:{chars}>{max_chars}")

    # ---- 依赖 / 签名 / 禁用调用 / heldout seed 字面量 --------------------------------
    tool_paths = [p for p in paths if p.startswith("harness/tools/")]
    _validate_imports(cand, cfg, violations, tool_paths)
    _validate_signature(cand, violations)
    _validate_banned_calls(cand, violations)
    _validate_seed_literals(cand, cfg, violations)

    # ---- kind 专项 -----------------------------------------------------------------
    if cand.kind == "tool_patch":
        _validate_tool_patch(cand, tool_paths, violations)
    elif cand.kind == "config_patch":
        if CONFIG_FILE not in paths:
            violations.append("config_patch_without_config_file")
        _validate_config_patch(cand, cfg, violations)
    elif cand.kind == "prompt_patch":
        if not any(p in (DECISION_PROMPT, REFLECTION_PROMPT) for p in paths):
            violations.append("prompt_patch_without_prompt_file")

    if violations:
        reason = f"{E.E_EVOLVE_PATCH_INVALID}:{violations[0]}"
        return ValidationResult.fail(reason, error_code=str(E.E_EVOLVE_PATCH_INVALID),
                                     violations=violations, diff_lines=lines,
                                     touched_files=paths)
    return ValidationResult.passed(diff_lines=lines, touched_files=paths)


# =====================================================================
# diff 应用（staging 用；``git apply``/``patch`` 的纯 Python 退化实现）
# =====================================================================

_HUNK_RE: Final[re.Pattern[str]] = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def apply_unified_diff(root: Path | str, diff: str, *, paths: Sequence[str] = ()) -> tuple[str, ...]:
    """把 unified diff 应用到 ``root`` 下的文件（staging 与测试共用）。

    只支持单文件 hunk（``@@`` 定位 + 上下文匹配）——harness 工件的改动面 ≤2 文件、
    ≤40 行，够用。失败抛 ``E_EVOLVE_PATCH_INVALID``（**不静默**）。
    返回被修改的相对路径元组（字典序）。
    """
    base = Path(root)
    touched: list[str] = []
    current: Path | None = None
    pending: list[tuple[int, list[str]]] = []
    header: str | None = None
    body: list[str] = []

    def close_hunk() -> None:
        nonlocal header, body
        if header is not None:
            match = _HUNK_RE.match(header)
            if match:
                lines = list(body)
                # diff 末尾的换行不是 hunk 行（否则会被当成一个空上下文行）
                if lines and lines[-1] == "":
                    lines.pop()
                pending.append((int(match.group(1)), lines))
        header, body = None, []

    def flush() -> None:
        nonlocal current, pending
        if current is None:
            return
        close_hunk()
        _apply_hunks(base, current, pending)
        rel = current.relative_to(base).as_posix()
        if rel not in touched:
            touched.append(rel)
        current, pending = None, []

    for line in str(diff).replace("\r\n", "\n").split("\n"):
        if line.startswith("--- "):
            continue
        if line.startswith("+++ "):
            flush()
            raw = line[4:].strip().split("\t", 1)[0]
            if raw.startswith(("a/", "b/")):
                raw = raw[2:]
            if not raw or raw == "/dev/null":
                raise PatchInvalidError("diff 不允许新建/删除文件（+++ /dev/null）")
            rel_under_root = _rel_under_harness(raw)[len("harness/"):]
            if not rel_under_root or ".." in Path(rel_under_root).parts:
                raise PatchInvalidError(f"diff 目标越出 harness 根：{raw}")
            # 不 resolve()：macOS 的 /var → /private/var 会让相对路径判定失真
            target = base / rel_under_root
            current = target
            continue
        if current is None:
            continue
        if _HUNK_RE.match(line):
            close_hunk()
            header, body = line, []
            continue
        if header is not None:
            body.append(line)
    flush()
    if not touched:
        raise PatchInvalidError("diff 没有可应用的文件（缺 +++ 头）")
    if paths:
        declared = {_rel_under_harness(p)[len("harness/"):] for p in paths}
        missing = sorted(declared - set(touched))
        if missing:
            raise PatchInvalidError(f"declared touched_files 与实际 diff 不符：{missing}")
    return tuple(sorted(touched))


def _hunk_old_block(body: Sequence[str]) -> list[str]:
    """hunk 的「原文块」= 上下文行 + 删除行（不含 ``+`` 新增行），用于定位。"""
    out: list[str] = []
    for line in body:
        if line.startswith(("@@", "\\")):
            continue
        tag, text = (line[:1], line[1:]) if line else (" ", "")
        if tag in (" ", "-"):
            out.append(text)
    return out


def _locate_hunk(original: Sequence[str], declared: int, block: Sequence[str], *,
                 name: str) -> int:
    """在声明位置附近找到原文块**逐字**匹配处，返回 0-based 起始行（F39）。

    标准 ``patch``/``git apply`` 都容忍 **offset**（hunk 头声明的行号偏了，但上下文在附近
    逐字命中）；本函数复刻该语义，但**不做 fuzz**（绝不忽略上下文行本身的差异）。

    F39 现场：``deepseek-flash`` 产出的 hunk 声明 ``@@ -44,6 +44,10 @@``，其 4 行上下文
    全部**逐字存在**于第 53–56 行（偏移 +9）——模型抄对了原文，只是不会数行号。原实现
    只认声明行号，于是把每一个真实候选都判成 ``E_EVOLVE_PATCH_INVALID``。

    歧义处理：由近及远逐行试探，同一距离上**两侧都命中**即拒绝（宁可判无效，也不赌一处）。
    """
    if not block:
        return declared

    def matches_at(pos: int) -> bool:
        return (pos >= 0 and pos + len(block) <= len(original)
                and list(original[pos:pos + len(block)]) == list(block))

    if matches_at(declared):
        return declared
    for delta in range(1, _MAX_HUNK_OFFSET + 1):
        hits = [pos for pos in (declared - delta, declared + delta) if matches_at(pos)]
        if len(hits) > 1:
            raise PatchInvalidError(
                f"hunk 上下文在 ±{delta} 行内歧义匹配（{name}）：无法确定落点",
                hint="INV-4/FR-07-4：宁可判无效，也不把补丁应用到不确定的位置")
        if hits:
            return hits[0]
    raise PatchInvalidError(
        f"hunk 上下文不匹配（{name} 第 {declared + 1} 行）："
        f"已在其 ±{_MAX_HUNK_OFFSET} 行内按逐字匹配搜索原文块，未命中",
        hint="上下文行必须与目标文件逐字符一致（proposer 的 prompt 已内联文件正文）")


def _apply_hunks(base: Path, target: Path, hunks: Sequence[tuple[int, list[str]]]) -> None:
    if not target.is_file():
        raise PatchInvalidError(f"diff 目标文件不存在：{target.name}",
                                hint="harness 工件只能被修改，不能被新建（FR-07-4）")
    original = target.read_text(encoding="utf-8").split("\n")
    if original and original[-1] == "":
        original.pop()
    out: list[str] = []
    cursor = 0 # 已消费的 original 行数（0-based）
    for start, body in sorted(hunks, key=lambda h: h[0]):
        # F39：声明行号只是**起点**，实际落点由原文块逐字匹配决定（容忍 offset）。
        offset = _locate_hunk(original, max(0, int(start) - 1), _hunk_old_block(body),
                              name=target.name)
        if offset < cursor:
            raise PatchInvalidError(f"hunk 定位重叠：@@ -{start}")
        out.extend(original[cursor:offset])
        cursor = offset
        for line in body:
            if line.startswith("@@"):
                continue
            if line.startswith("\\"):
                continue
            tag, text = (line[:1], line[1:]) if line else (" ", "")
            if tag == " ":
                if cursor >= len(original) or original[cursor] != text:
                    raise PatchInvalidError(
                        f"hunk 上下文不匹配（{target.name} 第 {cursor + 1} 行）")
                out.append(original[cursor])
                cursor += 1
            elif tag == "-":
                if cursor >= len(original) or original[cursor] != text:
                    raise PatchInvalidError(
                        f"hunk 删除行不匹配（{target.name} 第 {cursor + 1} 行）")
                cursor += 1
            elif tag == "+":
                out.append(text)
            else:
                raise PatchInvalidError(f"无法识别的 hunk 行：{line!r}")
    out.extend(original[cursor:])
    target.write_text("\n".join(out) + "\n", encoding="utf-8")


# =====================================================================
# propose（FR-07-1 / FR-07-5 / NFR-07-1）
# =====================================================================

def parse_proposal_payload(payload: Any) -> list[dict[str, Any]]:
    """把 LLM 的 JSON 产物规整为候选字典列表；形状非法 → ``ProposeFailedError``。"""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ProposeFailedError(f"propose 输出不是合法 JSON：{exc}") from exc
    if isinstance(payload, Mapping):
        items = payload.get("candidates", payload.get("proposals"))
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        items = payload
    else:
        items = None
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        raise ProposeFailedError(f"propose 输出缺少 candidates 数组：{type(payload).__name__}")
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise ProposeFailedError(f"候选不是 JSON 对象：{item!r}")
        out.append(dict(item))
    return out


def normalize_generated_candidate(raw: Mapping[str, Any], *, gen_id: str) -> Candidate:
    """LLM 候选 → ``Candidate``（形状问题一律 ``ProposeFailedError``）。"""
    kind = str(raw.get("kind", ""))
    diff = str(raw.get("diff", ""))
    rationale = " ".join(str(raw.get("rationale", "")).split())
    effect = ExpectedEffect.from_any(raw.get("expected_effect"))
    touched = tuple(_rel_under_harness(f) for f in (raw.get("touched_files") or ()))
    if not touched:
        touched = tuple(_rel_under_harness(p) for p in parse_diff_paths(diff))
    return Candidate.create(kind=kind, diff=diff, rationale=rationale, expected_effect=effect,
                            touched_files=touched, gen_id=gen_id)


def dedupe_candidates(candidates: Iterable[Candidate]
                      ) -> tuple[tuple[Candidate, ...], int]:
    """按归一化 diff 的 sha256 去重（FR-07-5），保留先出现的那个。"""
    seen: dict[str, int] = {}
    out: list[Candidate] = []
    dropped = 0
    for cand in candidates:
        key = diff_digest(cand.diff)
        if key in seen:
            dropped += 1
            continue
        seen[key] = len(out)
        out.append(cand)
    return tuple(out), dropped


def order_candidates(candidates: Iterable[Candidate]) -> tuple[Candidate, ...]:
    """按 ``expected_effect.priority`` 降序、同优先级 ``candidate_id`` 升序（FR-07-5）。"""
    return tuple(sorted(candidates,
                        key=lambda c: (-int(c.expected_effect.priority), c.candidate_id)))


def proposal_inputs(bundle: EvidenceBundle, cfg: Any) -> dict[str, Any]:
    """提案的**输入指纹面**（F42-B 检查点用）。

    与 ``propose`` 内部渲染调用**共用同一句**（``render_proposer_prompt`` + ``candidate_schema_dict``），
    避免「检查点按另一份参数算指纹」这类静默漂移。返回的 ``system`` / ``user`` / ``schema``
    就是真正送进 LLM 的三段。
    """
    system, user = render_proposer_prompt(bundle, cfg)
    return {"system": system, "user": user, "schema": candidate_schema_dict()}


def propose(bundle: EvidenceBundle, *, cfg: Any, rng_seed: int, client: Any = None,
            run_id: str = "", generation: int = 0, protocol: Any = None,
            runs_root: Any = None, app_config: Any = None,
            inputs: Mapping[str, Any] | None = None) -> ProposalResult:
    """候选生成（FR-07-1）。

    失败**绝不静默退化**：返回 ``ProposalResult(candidates=[], error_code=…)``。

    ``cfg`` 是 **``EvolveConfig`` 段**（渲染/重试/护栏都用它）；``app_config`` 是完整
    ``AppConfig``，**只**用于回退解析 proposer 客户端（见 ``_resolve_client`` 的 F36）。

    ``inputs``（F42-B）：调用方已用 ``proposal_inputs()`` 渲染过同一份三段时直接传入，
    避免「检查点算指纹」与「真正送进 LLM」两次渲染不一致（默认内部渲染）。
    """
    gen_id = f"g{int(generation):03d}"
    try:
        assert_no_holdout_leak(bundle, cfg=cfg, protocol=protocol)
    except HoldoutLeakError:
        raise # INV-5 致命：交由上层中止本代 + audit 告警（PRD §10）
    rendered = dict(inputs) if inputs is not None else proposal_inputs(bundle, cfg)
    system, user, schema = rendered["system"], rendered["user"], rendered["schema"]
    retries = max(0, int(getattr(cfg, "propose_max_retries", 2)))
    last_error = ""

    resolved = _resolve_client(app_config if app_config is not None else cfg,
                               run_id=run_id, client=client, runs_root=runs_root)
    if resolved is None:
        events.emit("evolve.propose", gen=int(generation), n_candidates=0,
                    error_code=str(E.E_EVOLVE_PROPOSE_FAILED), reason="no_client")
        return ProposalResult(error_code=str(E.E_EVOLVE_PROPOSE_FAILED),
                              reason="no_client")

    usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "latency_ms": 0}
    payload: Any = None
    for attempt in range(retries + 1):
        try:
            payload = _call_generate_json(resolved, system=system, user=user, schema=schema,
                                          seed=_attempt_seed(rng_seed, attempt),
                                          usage_out=usage)
        except InfraError as exc:
            last_error = f"{exc.code}"
            payload = None
        except Exception as exc: # noqa: BLE001 - LLM 异常一律降级为显式失败
            last_error = f"{type(exc).__name__}: {exc}"
            payload = None
        if payload is not None:
            break
    if payload is None:
        events.emit("evolve.propose", gen=int(generation), n_candidates=0,
                    error_code=str(E.E_EVOLVE_PROPOSE_FAILED), reason=last_error[:200],
                    **usage)
        return ProposalResult(error_code=str(E.E_EVOLVE_PROPOSE_FAILED),
                              reason=last_error[:200])

    try:
        raw_items = parse_proposal_payload(payload)
    except ProposeFailedError as exc:
        events.emit("evolve.propose", gen=int(generation), n_candidates=0,
                    error_code=str(E.E_EVOLVE_PROPOSE_FAILED), reason=str(exc)[:200],
                    **usage)
        return ProposalResult(error_code=str(E.E_EVOLVE_PROPOSE_FAILED),
                              reason=str(exc)[:200])

    try:
        candidates = [normalize_generated_candidate(item, gen_id=gen_id)
                      for item in raw_items]
    except (InfraError, ProposeFailedError) as exc:
        events.emit("evolve.propose", gen=int(generation), n_candidates=0,
                    error_code=str(E.E_EVOLVE_PROPOSE_FAILED), reason=str(exc)[:200],
                    **usage)
        return ProposalResult(error_code=str(E.E_EVOLVE_PROPOSE_FAILED),
                              reason=str(exc)[:200])

    # ---- 合法性校验：非法候选直接丢弃（该候选 reject 由上层落盘）---------------------
    violations_seen: list[str] = []

    def _validate_batch(batch: Sequence[Candidate]) -> list[Candidate]:
        kept_local: list[Candidate] = []
        for cand in batch:
            result = validate_candidate(cand, cfg=cfg)
            if result.ok:
                kept_local.append(cand)
            else:
                events.emit("evolve.validate", candidate_id=cand.candidate_id, status="fail",
                            violations=list(result.violations),
                            diff_lines=result.diff_lines,
                            touched_files=list(result.touched_files))
                violations_seen.append(
                    f"{cand.kind}: {'; '.join(str(v) for v in result.violations)}"[:300])
        return kept_local

    kept = _validate_batch(candidates)

    # ---- 全部未过静态校验 → 用**同一份约束**重提（F-M3-4，2026-10-18）------------------
    # 修复前：一次格式错误（例如把 config.json 写成嵌套对象）就让整代 no_candidate，
    # 而训练局已经花掉。``propose_max_retries`` 原先只覆盖「API 调用失败」，现在也覆盖
    # 「候选全灭」。重提只追加**本轮校验器给出的拒绝原因**与固定提醒；两者都只含候选
    # 自身的路径/键名，不注入证据束之外的上下文（INV-3）。
    for attempt in range(1, retries + 1):
        if kept:
            break
        retry_note = _RETRY_NOTE
        if violations_seen:
            retry_note += "\n上一次被拒绝的原因：\n" + "\n".join(
                f"- {item}" for item in violations_seen[-3:])
        try:
            retry_payload = _call_generate_json(
                resolved, system=system, user=user + retry_note, schema=schema,
                seed=_attempt_seed(rng_seed, retries + attempt), usage_out=usage)
            retry_items = parse_proposal_payload(retry_payload)
            batch = [normalize_generated_candidate(item, gen_id=gen_id)
                     for item in retry_items]
        except Exception: # noqa: BLE001 - 重提失败按「仍无候选」处理，不掩盖首个错误
            continue
        candidates.extend(batch)
        kept = _validate_batch(batch)

    # ---- 去重（FR-07-5）→ 排序（优先级降序、同优先级 id 升序）→ 预算截断（NFR-07-5）---
    deduped_all, n_deduped = dedupe_candidates(kept)
    ordered = order_candidates(deduped_all)
    limit = max(1, int(getattr(cfg, "n_candidates", 1)))
    deduped = ordered[:limit]

    events.emit("evolve.propose", gen=int(generation), n_candidates=len(deduped),
                n_deduped=int(n_deduped),
                kinds=[c.kind for c in deduped],
                prompt_tokens=int(usage["prompt_tokens"]),
                latency_ms=int(usage["latency_ms"]))
    if not deduped:
        return ProposalResult(candidates=(), error_code=str(E.E_EVOLVE_NO_CANDIDATE),
                              n_generated=len(candidates), n_deduped=int(n_deduped),
                              reason="no_candidate")
    return ProposalResult(candidates=deduped, n_generated=len(candidates),
                          n_deduped=int(n_deduped))


def _attempt_seed(rng_seed: int, attempt: int) -> int:
    """重试的确定性派生 seed（NFR-07-1：同 ``(run_id, gen, seed)`` 必得同结果）。"""
    if attempt <= 0:
        return int(rng_seed)
    digest = hashlib.sha256(f"{int(rng_seed)}:{int(attempt)}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def _call_generate_json(client: Any, *, system: str, user: str,
                        schema: Mapping[str, Any], seed: int,
                        usage_out: dict[str, int] | None = None) -> Any:
    """按 MOD-03 的**统一同步契约**调用 ``generate_json``（Issue #1 §2）。

    契约形参是 ``generate_json(messages, schema, *, mode, sampling, seed)``
    （``inference/client.py::InferenceClient``）。此前这里用的是
    ``generate_json(messages, schema, seed=…, temperature=0.0)`` —— 组合根注入的真实
    ``BackendInferenceClient`` 不接受该形状（缺 ``mode``/``sampling``、多 ``temperature``），
    于是 MOD-07 的提案路径在**真实客户端**下必然 ``TypeError`` → ``E_EVOLVE_PROPOSE_FAILED``；
    单测用的宽松 ``**kwargs`` 假件掩盖了它。现在按契约调用（假件因接受 ``**kwargs`` 同样可用），
    异步实现仍由 ``inspect.isawaitable`` 兜住。

    ``sampling`` 只给 ``temperature=0.0``：提案是**结构化判定输入**，必须走评估口径
    （B-14），token 上限沿用配置里的 ``inference.max_tokens``。

    ``usage_out``（F-M3-4，2026-10-18）：把本次调用的 prompt/completion/latency **累加**
    进调用方的字典。修复前 ``evolve.propose`` 事件把 ``prompt_tokens``/``latency_ms``
    写死为 0，实际 10,654 tok / 2.2 s 的提案在事件流里看起来像没花钱。``generate_json``
    的返回若是 MOD-03 的 ``GenerationResult``（带 ``.usage``）就取真实值；测试假件返回
    dict 时保持 0（不猜）。
    """
    import asyncio
    import inspect

    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    result = client.generate_json(messages, schema, mode="eval",
                                  sampling={"temperature": 0.0}, seed=int(seed),
                                  max_tokens=_PROPOSE_MAX_TOKENS)
    if inspect.isawaitable(result):
        result = asyncio.run(result)
    if usage_out is not None:
        usage = getattr(result, "usage", None)
        if usage is not None:
            for key in ("prompt_tokens", "completion_tokens", "latency_ms"):
                value = getattr(usage, key, 0)
                usage_out[key] = int(usage_out.get(key, 0) or 0) + int(value or 0)
    return _extract_json(result)


def _extract_json(result: Any) -> Any:
    """MOD-03 的返回可能是对象/字典/JSON 文本；统一取到 payload。"""
    if isinstance(result, (Mapping, list, str)):
        return result
    for attr in ("data", "value", "parsed", "json"):
        value = getattr(result, attr, None)
        if value is not None:
            return value
    text = getattr(result, "text", None)
    if text is not None:
        return text
    raise ProposeFailedError(f"无法从生成结果取出 JSON：{type(result).__name__}")


# =====================================================================
# 作者侧 prompt（``evolve propose`` CLI 的只读预览用）
# =====================================================================

def authoring_prompt(*, gen: int, now: Any = None) -> str:
    """``evolve propose --dry-run`` 的说明文本（不触网、不调模型）。"""
    stamp = utc_now(now)
    return (f"[{stamp}] MOD-07 propose（gen={int(gen)}）：需注入 MOD-03 客户端；"
            f"证据束=traj:train+memory:train+reflect:train+harness:current（INV-3）")


def candidate_id_of_raw(kind: str, diff: str, rationale: str) -> str:
    """便捷入口（测试与 CLI 用）：直接算 ``candidate_id``。"""
    return hashlib.sha256("\x00".join((str(kind), str(diff), str(rationale)))
                          .encode("utf-8")).hexdigest()[:12]
