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
from dice_ratchet.infra.errors import E, InfraError
from dice_ratchet.infra.io import read_json

from .errors import HoldoutLeakError, PatchInvalidError, ProposeFailedError
from .paths import harness_dir as default_harness_dir
from .paths import utc_now
from .schemas import (CANDIDATE_SCHEMA, Candidate, EvidenceBundle, ExpectedEffect,
                      HarnessSnapshot, ProposalResult, ValidationResult, _plain,
                      canonical_json, count_diff_lines, diff_digest, parse_diff_paths)

__all__ = [
    "ALLOWED_TOOL_EXT", "CONFIG_FILE", "DECISION_PROMPT", "EVIDENCE_SCHEMA_VERSION",
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
_FORBIDDEN_PATH_PATTERNS: Final[tuple[str, ...]] = (
    "harness/version.json", "harness/versions", "harness/.git", "version.json",
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
    if client is not None:
        return client
    if _CLIENT_PROVIDER is None:
        return None
    kwargs: dict[str, Any] = {"run_id": run_id}
    if runs_root is not None:
        kwargs["runs_root"] = Path(runs_root)
    try:
        return _CLIENT_PROVIDER(cfg, **kwargs)
    except TypeError:
        try:
            return _CLIENT_PROVIDER(cfg)
        except TypeError:
            return _CLIENT_PROVIDER()


# =====================================================================
# 证据束构造（FR-07-2）
# =====================================================================

def harness_snapshot(harness: Path | str | None = None) -> HarnessSnapshot:
    """扫描 ``harness/`` 得到 ``version`` + 各文件 sha256（``source_tags`` 的 ``harness:current``）。

    只读**生效工件**（``prompts/`` / ``tools/`` / ``config.json`` / ``version.json``），
    不进 ``versions/``（历史快照不进证据束）。
    """
    root = default_harness_dir(harness)
    files: list[tuple[str, str]] = []
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if rel.startswith("versions/") or rel.startswith("."):
                continue
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError: # pragma: no cover - 读失败不阻塞证据束
                continue
            files.append((rel, digest))
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
    return HarnessSnapshot(version=version, files=tuple(files), artifact_hash=artifact_hash)


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
            payload = dict(payload)
            payload["split"] = "train" # MOD-05 检索面只可能来自 train 局
            out.append(payload)
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
        payload["split"] = "train"
        out.append(payload)
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
    """解析 ``"heldout:2000-2039"`` / ``"2000-2003,2008"`` 形态的 seed 规格。"""
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
7. 只输出一个 JSON 对象，字段见 user 消息的输出契约。

你看不到留出集数据，也不需要它们。"""

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
    """
    allowed_files = "、".join(sorted(_allowed_file_specs()))
    allowlist = getattr(cfg, "tool_import_allowlist", _DEFAULT_IMPORT_ALLOWLIST)
    system = _SYSTEM_TEMPLATE.format(
        allowed_files=allowed_files,
        max_lines=int(getattr(cfg, "max_diff_lines", 40)),
        max_chars=int(getattr(cfg, "max_diff_chars", 4000)),
        max_files=int(_MAX_FILES),
        allowed_imports="、".join(sorted(str(a) for a in allowlist)),
        frozen=FROZEN_SIGNATURE)

    task = _TASK_BLOCK.format(n_candidates=int(getattr(cfg, "n_candidates", 1)))
    memory_block = _render_items(bundle.memory_items, empty="（无可用策略记忆）")
    traj_block = _render_items(bundle.trajectory_summaries, empty="（无可用轨迹摘要）")
    reflect_block = _render_items(bundle.reflections, empty="（无可用反思）")
    harness_block = _render_harness(bundle.harness_snapshot)

    # 占位符与证据束一一对应：observation→traj 摘要，memory→记忆，harness→快照，task→任务
    user = "\n\n".join((
        task,
        "## observation（最近 train 轨迹摘要）\n" + traj_block,
        "## memory（train 策略记忆）\n" + memory_block,
        "## reflections（train 反思）\n" + reflect_block,
        "## harness（当前生效工件的 sha256）\n" + harness_block,
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
    lines = [f"- version: {int(snap.version)}",
             f"- artifact_hash: {snap.artifact_hash or '(未计算)'}"]
    for name, digest in snap.files:
        if name == "version.json":
            continue
        lines.append(f"- {name}: sha256:{digest[:16]}")
    return "\n".join(lines)


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


def _allowed_file_specs() -> tuple[str, ...]:
    return (DECISION_PROMPT, REFLECTION_PROMPT, CONFIG_FILE, TOOLS_GLOB)


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


def _validate_config_patch(cand: Candidate, cfg: Any, violations: list[str]) -> None:
    """``config_patch`` 只能改 ``evolve.evolvable_keys`` 声明的键（FR-07-4）。"""
    evolvable = {str(k) for k in getattr(cfg, "evolvable_keys", ())}
    if not evolvable:
        violations.append("config_patch_without_evolvable_keys")
        return
    text = "\n".join(_added_lines(cand.diff))
    keys = {m.group(1) for m in _JSON_KEY_RE.finditer(text)}
    if not keys:
        # 非 JSON 形态的 diff（例如整文件替换）→ 退化为子串匹配
        keys = {k for k in evolvable if k in text}
        if not keys:
            violations.append("config_patch_no_evolvable_key")
            return
    illegal = sorted(k for k in keys if not _key_is_evolvable(k, evolvable))
    if illegal:
        violations.append(f"config_key_not_evolvable:{illegal}")


def _key_is_evolvable(key: str, evolvable: set[str]) -> bool:
    """``config.json`` 的键允许两种写法：完整点分路径，或末段短名。

    ``evolvable_keys`` 用 Spec §5 的点分路径（``retrieval.top_k``），而 harness 的
    ``config.json`` 可能就写成 ``{"retrieval": {"top_k": 12}}`` 里的裸 ``top_k``——
    只要它是某个可演化路径的末段即视为合法（``sampling.seed`` 不是任何可演化路径的
    末段，因此仍被拒）。
    """
    if key in evolvable:
        return True
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
    if len(str(cand.rationale)) > 300:
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
        offset = max(0, int(start) - 1)
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


def propose(bundle: EvidenceBundle, *, cfg: Any, rng_seed: int, client: Any = None,
            run_id: str = "", generation: int = 0, protocol: Any = None,
            runs_root: Any = None) -> ProposalResult:
    """候选生成（FR-07-1）。

    失败**绝不静默退化**：返回 ``ProposalResult(candidates=[], error_code=…)``。
    """
    gen_id = f"g{int(generation):03d}"
    try:
        assert_no_holdout_leak(bundle, cfg=cfg, protocol=protocol)
    except HoldoutLeakError:
        raise # INV-5 致命：交由上层中止本代 + audit 告警（PRD §10）
    system, user = render_proposer_prompt(bundle, cfg)
    schema = candidate_schema_dict()
    retries = max(0, int(getattr(cfg, "propose_max_retries", 2)))
    last_error = ""

    resolved = _resolve_client(cfg, run_id=run_id, client=client, runs_root=runs_root)
    if resolved is None:
        events.emit("evolve.propose", gen=int(generation), n_candidates=0,
                    error_code=str(E.E_EVOLVE_PROPOSE_FAILED), reason="no_client")
        return ProposalResult(error_code=str(E.E_EVOLVE_PROPOSE_FAILED),
                              reason="no_client")

    payload: Any = None
    for attempt in range(retries + 1):
        try:
            payload = _call_generate_json(resolved, system=system, user=user, schema=schema,
                                          seed=_attempt_seed(rng_seed, attempt))
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
                    error_code=str(E.E_EVOLVE_PROPOSE_FAILED), reason=last_error[:200])
        return ProposalResult(error_code=str(E.E_EVOLVE_PROPOSE_FAILED),
                              reason=last_error[:200])

    try:
        raw_items = parse_proposal_payload(payload)
    except ProposeFailedError as exc:
        events.emit("evolve.propose", gen=int(generation), n_candidates=0,
                    error_code=str(E.E_EVOLVE_PROPOSE_FAILED), reason=str(exc)[:200])
        return ProposalResult(error_code=str(E.E_EVOLVE_PROPOSE_FAILED),
                              reason=str(exc)[:200])

    try:
        candidates = [normalize_generated_candidate(item, gen_id=gen_id)
                      for item in raw_items]
    except (InfraError, ProposeFailedError) as exc:
        events.emit("evolve.propose", gen=int(generation), n_candidates=0,
                    error_code=str(E.E_EVOLVE_PROPOSE_FAILED), reason=str(exc)[:200])
        return ProposalResult(error_code=str(E.E_EVOLVE_PROPOSE_FAILED),
                              reason=str(exc)[:200])

    # ---- 合法性校验：非法候选直接丢弃（该候选 reject 由上层落盘）---------------------
    kept: list[Candidate] = []
    for cand in candidates:
        result = validate_candidate(cand, cfg=cfg)
        if result.ok:
            kept.append(cand)
        else:
            events.emit("evolve.validate", candidate_id=cand.candidate_id, status="fail",
                        violations=list(result.violations),
                        diff_lines=result.diff_lines,
                        touched_files=list(result.touched_files))

    # ---- 去重（FR-07-5）→ 排序（优先级降序、同优先级 id 升序）→ 预算截断（NFR-07-5）---
    deduped_all, n_deduped = dedupe_candidates(kept)
    ordered = order_candidates(deduped_all)
    limit = max(1, int(getattr(cfg, "n_candidates", 1)))
    deduped = ordered[:limit]

    events.emit("evolve.propose", gen=int(generation), n_candidates=len(deduped),
                n_deduped=int(n_deduped),
                kinds=[c.kind for c in deduped], prompt_tokens=0, latency_ms=0)
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
                        schema: Mapping[str, Any], seed: int) -> Any:
    """调用 MOD-03 ``generate_json``（同步客户端 / 异步后端两种形态都接受）。"""
    import asyncio
    import inspect

    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    result = client.generate_json(messages, schema, seed=int(seed),
                                  temperature=0.0)
    if inspect.isawaitable(result):
        result = asyncio.run(result)
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
