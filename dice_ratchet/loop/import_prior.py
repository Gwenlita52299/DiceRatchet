"""把**上一轮 run** 的训练数据接入新一轮 run（用户口径：gen-000 的 30 局 train 直接复用）。

**为什么需要这个模块**（2026-10-17，一轮"以 P1 为起点"的新实验）：

* 新一轮的第一代（cycle 0）本应"用起点 harness 跑 30 局 train 得到 P1"——而 P1 已经由**上一轮
  三代补丁的语义去重**人工得到，这 30 局训练局**已经跑过**（`runs/m3-formal/episodes/train/`
  的 seed 1000–1029、反思、`memory_v1.jsonl`）。重跑一遍既花钱又与"复用"的指示相悖。
* 但旧轨迹是 **F64 修复前**落的：`episode_key` 与行内 `generation` 都标成 `state.generation`
  （=0），而当前代码要求训练局标"**它服务的那一代**"（第一代 = 1）。直接拷贝 ⇒
  `evals.harness.load_episode_results(generation=1)` 一局都取不到（`train.source="insufficient"`）。
* 跨 run 的**臂归档**复用闸（`ARM_PROVENANCE_KEYS`）还会因 `config_hash` / `harness_play_hash`
  等身份键与旧归档对不上而 fail-closed。本模块**不**碰臂归档——它只管训练局的**输入侧**
  （轨迹 + 反思 + 记忆快照），这些是下一代的**学习状态**，不需要过评估复用闸。

**只读旧 run、只写新 run**：旧 run 一个字节都不改（它的归档与产物必须保持可审计）。

**本轮只用它导入 gen-000 的训练块**（`--generations 0` → 新 run 的 cycle 0 学习状态）；
`--generations 0-2` 这种多代导入保留给以后的"从 gen-002 状态续做"场景。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..infra.errors import E, InfraError
from ..infra.io import read_json, read_jsonl, write_json_atomic

__all__ = ["ImportPlan", "ImportReport", "plan_import", "apply_import", "verify_import",
           "TRAINING_SPLIT", "TRAINING_ARM"]

TRAINING_SPLIT = "train"
TRAINING_ARM = "train"

#: 导入报告的文件名（落在新 run 根下，作为"这批学习状态从哪来"的可审计证据）。
REPORT_NAME = "import_prior.json"


@dataclass
class GenerationImport:
    """一代训练块的导入计划（逐局 + 反思 + 记忆快照）。"""

    generation: int                      #: 它服务的那一代（F64 口径；第一代 = 1）
    seeds: list[int] = field(default_factory=list)
    episodes: list[str] = field(default_factory=list)      #: 相对 episodes/train/ 的文件名
    reflections: list[str] = field(default_factory=list)   #: 相对 reflections/ 的文件名
    missing_episodes: list[int] = field(default_factory=list)
    missing_reflections: list[int] = field(default_factory=list)
    memory_version: int = 0
    memory_snapshot: str = ""            #: 旧 run 的 memory_v{N}.jsonl（空 = 没有快照）
    counters: dict[str, int] = field(default_factory=dict)
    legacy_generation: int | None = None  #: 旧轨迹里写的 generation 标签（= 服务代数 − 1）

    def as_dict(self) -> dict[str, Any]:
        return {
            "generation": int(self.generation),
            "seeds": [int(s) for s in self.seeds],
            "n_seeds": len(self.seeds),
            "episodes": list(self.episodes),
            "reflections": list(self.reflections),
            "missing_episodes": [int(s) for s in self.missing_episodes],
            "missing_reflections": [int(s) for s in self.missing_reflections],
            "memory_version": int(self.memory_version),
            "memory_snapshot": str(self.memory_snapshot),
            "counters": dict(self.counters),
            "legacy_generation": (None if self.legacy_generation is None
                                  else int(self.legacy_generation)),
        }


@dataclass
class ImportPlan:
    """一次导入的全部计划（`plan_import` 的产物；`--dry-run` 就打印它）。"""

    source_run: str
    target_run: str
    source_root: Path
    target_root: Path
    generations: list[GenerationImport] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_run": self.source_run,
            "target_run": self.target_run,
            "source_root": str(self.source_root),
            "target_root": str(self.target_root),
            "generations": [g.as_dict() for g in self.generations],
            "state": dict(self.state),
            "warnings": list(self.warnings),
        }


@dataclass
class ImportReport:
    """`apply_import` 的结果（落盘 `runs/<target>/import_prior.json`）。"""

    plan: ImportPlan
    written: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "kind": "prior_run_training_import",
            "source_run": self.plan.source_run,
            "target_run": self.plan.target_run,
            "generations": [g.as_dict() for g in self.plan.generations],
            "state": dict(self.plan.state),
            "warnings": list(self.plan.warnings),
            "files": list(self.written),
            "note": ("只导入训练局的**输入侧**（轨迹 + 反思 + 记忆快照）到新 run；"
                     "旧 run 未改动。跨 run 的臂归档**不**复用（评估复用闸按测量身份 fail-closed）。"),
        }


# =====================================================================
# 计划阶段（只读）
# =====================================================================

def _episode_name(seed: int, legacy_generation: int) -> str:
    """旧 run 里的轨迹文件名：``e-{seed:06d}-g{gen:03d}-train``（F64 前口径）。"""
    return f"e-{int(seed):06d}-g{int(legacy_generation):03d}-{TRAINING_ARM}.jsonl"


def _reflection_name(seed: int, legacy_generation: int) -> str:
    return f"e-{int(seed):06d}-g{int(legacy_generation):03d}-{TRAINING_ARM}.json"


def _rows(path: Path) -> list[dict[str, Any]]:
    rows, _truncated = read_jsonl(path)
    return [dict(r) for r in rows if isinstance(r, Mapping)]


def _counters_for(episode_paths: Iterable[Path],
                  reflection_paths: Iterable[Path]) -> dict[str, int]:
    """这一批训练局对 `state.counters` 的贡献（从旧轨迹/反思实测，不猜）。

    * `episodes_total` / `episodes_truncated` 取自**轨迹首行**（MOD-06 落盘的逐局事实）；
    * `illegal_actions` 取自**反思条目**——轨迹行里没有这个字段（实测首行
      `illegal_attempts` 缺失），而反思条目带上了同一局的计数（`loop_reflection.schema`）。
      两条都取不到就记 0（宁可少记，不编数）。
    """
    episodes_total = 0
    episodes_truncated = 0
    for path in episode_paths:
        rows = _rows(path)
        if not rows:
            continue
        episodes_total += 1
        if rows[0].get("truncation_reason"):
            episodes_truncated += 1
    reflections_ok = 0
    illegal_actions = 0
    for path in reflection_paths:
        data = read_json(path)
        entry = data.get("entry") if isinstance(data, Mapping) else None
        if isinstance(entry, Mapping):
            reflections_ok += 1
            illegal_actions += int(entry.get("illegal_attempts") or 0)
    return {
        "episodes_total": episodes_total,
        "episodes_truncated": episodes_truncated,
        "reflections_ok": reflections_ok,
        "reflections_skipped": 0,
        "illegal_actions": illegal_actions,
        "env_errors": 0,
        "llm_timeouts": 0,
        "io_retries": 0,
    }


def _add_counters(total: dict[str, int], part: Mapping[str, int]) -> None:
    for key, value in part.items():
        total[str(key)] = int(total.get(str(key), 0)) + int(value)


def plan_import(*, source_root: Path | str, target_root: Path | str, generations: Sequence[int],
                k: int, eval_episodes_cap: int = 120, harness_version: int = 0,
                require_reflections: bool = True,
                strict_target: bool = True) -> ImportPlan:
    """算出"要拷什么"（**只读**）。任何 fail-closed 条件都抛 ``InfraError``。

    校验项（任一不过 → 抛错，绝不静默半导入）：
    1. 源 run 存在且有 ``state.json``（凭它拿 `k` 与 seed 归属）；
    2. 每一代的训练块 seed = `[g*k + offset, (g+1)*k)`（gen-000 的 k=30 ⇒ 1000–1029）在源 run
       里**都有轨迹**（缺一局就抛；`allow_missing_episodes` 不提供——半套训练数据比没有更糟）；
    3. 每一局都有对应的反思文件（`require_reflections=True` 时）；
    4. 目标 run 目录的 ``state.json`` **不存在**（只在空 run 上导入，避免覆盖进行中的状态）。
    """
    src = Path(source_root)
    dst = Path(target_root)
    if not src.is_dir():
        raise InfraError(E.E_IO_WRITE, f"源 run 不存在：{src}", hint="检查 --from-run")
    src_state_path = src / "state.json"
    if not src_state_path.is_file():
        raise InfraError(E.E_IO_STATE_MISSING, f"源 run 缺 state.json：{src_state_path}")
    src_state = read_json(src_state_path)
    if not isinstance(src_state, Mapping):
        raise InfraError(E.E_IO_RUN_STATE, f"源 state.json 不是对象：{src_state_path}")

    int_gens = sorted({int(g) for g in generations})
    if not int_gens:
        raise InfraError(E.E_IO_RUN_STATE, "没有指定要导入的代数", hint="--generations 0")
    if any(g < 0 for g in int_gens):
        raise InfraError(E.E_IO_RUN_STATE, f"代数必须 ≥ 0：{int_gens}")

    if strict_target and (dst / "state.json").is_file():
        raise InfraError(
            E.E_IO_RUN_STATE,
            f"目标 run 已有 state.json：{dst / 'state.json'}",
            hint="导入只在空 run 上做；要重来请先删掉该 run 目录")

    plan = ImportPlan(source_run=src.name, target_run=dst.name, source_root=src, target_root=dst)
    episode_dir = src / "episodes" / TRAINING_SPLIT
    reflection_dir = src / "reflections"
    memory_dir = src / "memory"

    counters: dict[str, int] = {}
    completed: list[int] = []
    memory_version = 0
    for gen in int_gens:
        if gen < 1:
            raise InfraError(
                E.E_IO_RUN_STATE,
                f"训练块代数必须 ≥ 1（gen-000 不消耗训练 seed）：{gen}",
                hint="D-M3-7 的 cycle 0 训练局服务**第一代**（generation=1）")
        legacy_gen = gen - 1          # F64 修复前：轨迹里的标签 = 服务代数 − 1
        # seed 块**只经** MOD-08 的唯一出口取（INV-5：不自己写区间，改 seeds.yaml 也不会漂）
        from ..evals.seeds import coerce_protocol, take, validate_protocol

        proto = validate_protocol(coerce_protocol(None))
        block = list(take(TRAINING_SPLIT, generation=gen, protocol=proto))
        if len(block) != int(k):
            raise InfraError(
                E.E_EVAL_SEED_EXHAUSTED,
                f"gen-{gen:03d} 的训练块是 {len(block)} 局，与 k={int(k)} 不符",
                hint="seeds.yaml 的 per_generation 必须与 loop.k 一致")
        seeds = [int(s) for s in block]
        item = GenerationImport(generation=gen, seeds=list(seeds), legacy_generation=legacy_gen)
        ep_paths: list[Path] = []
        for seed in seeds:
            name = _episode_name(seed, legacy_gen)
            path = episode_dir / name
            if not path.is_file():
                item.missing_episodes.append(int(seed))
                continue
            rows = _rows(path)
            if not rows:
                item.missing_episodes.append(int(seed))
                continue
            row = rows[0]
            if str(row.get("arm") or "") != TRAINING_ARM or str(row.get("split") or "") != TRAINING_SPLIT:
                raise InfraError(
                    E.E_EVAL_SPLIT_VIOLATION,
                    f"{path.name} 的 split/arm 不是 {TRAINING_SPLIT}/{TRAINING_ARM}："
                    f"{row.get('split')!r}/{row.get('arm')!r}",
                    hint="训练口只接 train/train 轨迹；其余一律拒绝（INV-5）")
            if int(row.get("seed") or -1) != int(seed):
                raise InfraError(E.E_IO_RUN_STATE,
                                 f"{path.name} 的 seed 与文件名不符：{row.get('seed')!r}")
            item.episodes.append(name)
            ep_paths.append(path)
            ref_name = _reflection_name(seed, legacy_gen)
            ref_path = reflection_dir / ref_name
            if ref_path.is_file():
                item.reflections.append(ref_name)
            else:
                item.missing_reflections.append(int(seed))
        if item.missing_episodes:
            raise InfraError(
                E.E_IO_RUN_STATE,
                f"gen-{gen:03d} 的训练块不完整：缺 {len(item.missing_episodes)} 局轨迹"
                f"（seed {item.missing_episodes[:5]}…）",
                hint="半套训练数据会让 train 段 low_confidence；先补齐或改用 --generations 换代数")
        if require_reflections and item.missing_reflections:
            raise InfraError(
                E.E_IO_RUN_STATE,
                f"gen-{gen:03d} 缺 {len(item.missing_reflections)} 份反思"
                f"（seed {item.missing_reflections[:5]}…）",
                hint="没有反思就没有学习状态；要么补跑反思，要么显式 --allow-missing-reflections")
        snapshot = memory_dir / f"memory_v{gen}.jsonl"
        if snapshot.is_file():
            item.memory_snapshot = snapshot.name
            item.memory_version = int(gen)
            memory_version = int(gen)
        else:
            plan.warnings.append(
                f"gen-{gen:03d} 的 memory_v{gen}.jsonl 不存在：记忆快照不导入"
                f"（新一轮的记忆从空开始；训练局与反思仍导入）")
        part = _counters_for(ep_paths, [reflection_dir / n for n in item.reflections])
        item.counters = part
        _add_counters(counters, part)
        completed.extend(seeds)
        plan.generations.append(item)

    plan.state = {
        "run_id": dst.name,
        "phase": "pending",
        "generation": len(int_gens),
        "harness_version": int(harness_version),
        "k": int(k),
        "tick": len(completed),
        "completed_train_seeds": [int(s) for s in _dedup(completed)],
        "pending_queue": [],
        "hitl_pending": None,
        "last_completed_cycle": len(int_gens),
        "evolving_cycle": None,
        "memory_version": int(memory_version),
        "resume_count": 0,
        "counters": counters,
        "budget": {"eval_episodes_used": 0, "eval_episodes_cap": int(eval_episodes_cap),
                   "candidates_used": 0},
        "interrupted_at": None,
        "source_run": str(src.name),
    }
    return plan


def _dedup(values: Sequence[int]) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for value in values:
        if int(value) not in seen:
            seen.add(int(value))
            out.append(int(value))
    return out


# =====================================================================
# 执行阶段（只写新 run）
# =====================================================================

def _write_bytes(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def _migrate_reflection(src_path: Path, dst_path: Path, *, run_id: str, generation: int) -> None:
    """反思条目：**只改** `run_id` / `generation`（F64 口径）/ `episode_id`，其余逐字节保持。"""
    data = read_json(src_path)
    entry = dict(data.get("entry") or {})
    entry["run_id"] = str(run_id)
    entry["generation"] = int(generation)
    entry["episode_id"] = _relabel_episode_key(str(entry.get("episode_id") or ""), generation)
    payload = dict(data)
    payload["entry"] = entry
    if "tick" in payload:
        payload["tick"] = int(payload.get("tick") or entry.get("tick") or 0)
    write_json_atomic(dst_path, payload)


def _export_reflections_jsonl(src_path: Path, dst_path: Path, *, run_id: str,
                              wanted: Mapping[int, int]) -> dict[str, int]:
    """把旧 run 的 `reflections/reflections.jsonl` 里属于被导入局的那些行**原样**导出。

    ``wanted``：``seed → 服务代数``（只导这些 seed/代数组合的行；旧 run 的其余 60 局
    **不**进新 run 的反思台账）。行内只改 `run_id`/`generation`/`episode_id`/`tick`——
    与逐局反思文件的改写口径逐字一致，避免"每局一份 `.json` 与聚合台账说的不是同一件事"。
    """
    wanted = {int(seed): int(gen) for seed, gen in wanted.items()}
    kept: list[str] = []
    seen: set[str] = set()
    if Path(src_path).is_file():
        for row in _rows(src_path):
            seed = int(row.get("seed") or -1)
            if seed not in wanted:
                continue
            legacy = int(row.get("generation") or 0)
            target_gen = wanted[seed]
            if legacy != int(target_gen) - 1:
                # 同一 seed 在多代各有一条（旧 run 每代都跑了不同 seed 块，正常不会撞上）；
                # 撞上就只认与该代匹配的旧标签，不猜。
                continue
            new_row = dict(row)
            new_row["run_id"] = str(run_id)
            new_row["generation"] = int(target_gen)
            new_row["episode_id"] = _relabel_episode_key(
                str(new_row.get("episode_id") or ""), target_gen)
            if new_row.get("tick") is not None:
                new_row["tick"] = int(target_gen - 1) * 30 + (seed % 30)
            rid = str(new_row.get("reflection_id") or "")
            if rid in seen:
                continue
            seen.add(rid)
            kept.append(json.dumps(new_row, ensure_ascii=False, separators=(",", ":")))
    Path(dst_path).parent.mkdir(parents=True, exist_ok=True)
    Path(dst_path).write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    return {"n": len(kept)}


def _migrate_memory_snapshot(src_path: Path, dst_path: Path, *, run_id: str,
                             config_hash: str) -> None:
    """记忆快照：重写 header 的 `run_id`/`config_hash`，**条目行逐字节不变**
    （`content_sha256` 只覆盖条目行，所以改 header 不破坏校验）。"""
    text = Path(src_path).read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines:
        raise InfraError(E.E_IO_MEMORY_CORRUPT, f"记忆快照为空：{src_path}")
    header = json.loads(lines[0])
    if not isinstance(header, Mapping):
        raise InfraError(E.E_IO_MEMORY_CORRUPT, f"记忆快照 header 不是对象：{src_path}")
    new_header = dict(header)
    new_header["run_id"] = str(run_id)
    new_header["config_hash"] = str(config_hash)
    body = "\n".join([json.dumps(new_header, ensure_ascii=False, separators=(",", ":"))]
                     + lines[1:]) + "\n"
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    dst_path.write_text(body, encoding="utf-8")


def _relabel_episode(src_path: Path, dst_path: Path, *, generation: int) -> None:
    """轨迹行按 F64 口径**重标** `generation`（旧 run 写的是一代前的号）。

    行内 `harness_version` 保持原值（起点 harness 的版本号就是 0）；`episode_key` 与文件名
    同步改成服务代数的标签——否则 `load_episode_results(generation=1)` 取不到这一局。

    另外写入一份**只读的原地标签记录**（`legacy_generation`）：导出方与审计方都能看出
    "这一行曾经标成 0"，不是无声改写。
    """
    rows = _rows(src_path)
    if not rows:
        raise InfraError(E.E_IO_RUN_STATE, f"轨迹为空：{src_path}")
    legacy = int(rows[0].get("generation") or 0)
    out_lines: list[str] = []
    for index, row in enumerate(rows):
        new_row = dict(row)
        old_key = str(new_row.get("episode_key") or "")
        new_key = _relabel_episode_key(old_key, generation)
        new_row["generation"] = int(generation)
        new_row["episode_key"] = new_key
        if index == 0:
            new_row["legacy_generation"] = legacy
            new_row["imported_from_generation"] = int(generation)
        out_lines.append(json.dumps(new_row, ensure_ascii=False, separators=(",", ":")))
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    dst_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


def _relabel_episode_key(key: str, generation: int) -> str:
    """``e-001000-g000-train`` → ``e-001000-g001-train``（只替换 ``g{NNN}`` 段）。"""
    parts = str(key).split("-")
    for index, part in enumerate(parts):
        if len(part) == 4 and part.startswith("g") and part[1:].isdigit():
            parts[index] = f"g{int(generation):03d}"
            break
    return "-".join(parts)


def _memory_config_hash(config: Any) -> str:
    """与 `MemoryStore._config_hash` **同一枚**哈希（否则新 run 的 index.json 会自我改口径）。"""
    from dataclasses import asdict

    from ..memory.schemas import content_sha256_of

    try:
        payload = asdict(config) if hasattr(config, "__dataclass_fields__") else dict(config)
    except Exception:  # noqa: BLE001 - 非 dataclass 配置：退化为 repr 摘要
        payload = {"repr": repr(config)}
    return content_sha256_of({"memory": payload})[:16]


def apply_import(plan: ImportPlan, *, memory_config: Any = None) -> ImportReport:
    """按计划落盘（只写 `plan.target_root`）。返回可审计的 `ImportReport`。"""
    dst = Path(plan.target_root)
    dst.mkdir(parents=True, exist_ok=True)
    report = ImportReport(plan=plan)
    ep_dir = dst / "episodes" / TRAINING_SPLIT
    ref_dir = dst / "reflections"
    mem_dir = dst / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)

    for item in plan.generations:
        for name in item.episodes:
            src_path = Path(plan.source_root) / "episodes" / TRAINING_SPLIT / name
            seed = int(name.split("-")[1])
            dst_name = _episode_name(seed, int(item.generation))
            dst_path = ep_dir / dst_name
            _relabel_episode(src_path, dst_path, generation=int(item.generation))
            report.written.append(str(dst_path.relative_to(dst)))
        for name in item.reflections:
            src_path = Path(plan.source_root) / "reflections" / name
            seed = int(name.split("-")[1])
            dst_name = _reflection_name(seed, int(item.generation))
            _migrate_reflection(src_path, ref_dir / dst_name, run_id=plan.target_run,
                                generation=int(item.generation))
            report.written.append(str((ref_dir / dst_name).relative_to(dst)))
        if item.memory_snapshot:
            src_path = Path(plan.source_root) / "memory" / item.memory_snapshot
            active = mem_dir / "memory_active.jsonl"
            config_hash = (_memory_config_hash(memory_config) if memory_config is not None
                           else "")
            _migrate_memory_snapshot(src_path, active, run_id=plan.target_run,
                                     config_hash=config_hash)
            report.written.append(str(active.relative_to(dst)))
            snapshot_copy = mem_dir / item.memory_snapshot
            _write_bytes(snapshot_copy, active.read_bytes())
            report.written.append(str(snapshot_copy.relative_to(dst)))
            # index.json 也要写：`MemoryStore` 的 `memory_version` 只从它读（没有它 ⇒ 0），
            # 而新一轮的代际快照文件名与 rollback 目标都建在这个号上。条目本身仍由 store
            # 从 `memory_active.jsonl` 重建，这里只钉住「这是第几版记忆 + run_id 是谁」。
            index_payload = {
                "store_schema_version": "1.0",
                "entry_schema_version": "1.1",
                "memory_version": int(item.memory_version),
                "parent_memory_version": max(0, int(item.memory_version) - 1),
                "harness_version": int(plan.state.get("harness_version", 0)),
                "generation": int(item.generation),
                "run_id": str(plan.target_run),
                "config_hash": config_hash,
                "last_tick": int(plan.state.get("tick", 0)),
                "lineage": [{"generation": int(item.generation),
                             "harness_version": int(plan.state.get("harness_version", 0)),
                             "memory_version": int(item.memory_version)}],
            }
            write_json_atomic(mem_dir / "index.json", index_payload)
            report.written.append(str((mem_dir / "index.json").relative_to(dst)))

    # 反思聚合台账（`memory.store` 在构造时读它；缺了它，逐局反思文件就只是摆设）
    # 放在 state.json 之前：报告里要带上「导入了多少条反思」这一条事实。
    wanted: dict[int, int] = {}
    for item in plan.generations:
        for seed in item.seeds:
            wanted[int(seed)] = int(item.generation)
    n_reflections = _export_reflections_jsonl(
        Path(plan.source_root) / "reflections" / "reflections.jsonl",
        ref_dir / "reflections.jsonl", run_id=plan.target_run, wanted=wanted)["n"]
    report.written.append(str(Path("reflections") / "reflections.jsonl"))
    plan.state["imported_reflections"] = int(n_reflections)

    # state.json：**用 MOD-06 自己的入参形状**落盘并过 schema 校验（不手写 JSON）
    from ..infra.run import RunPaths
    from .state import RunCounters, RunBudget, init_state, save_state

    state_payload = dict(plan.state)
    state_payload.pop("source_run", None)          # 扩展键不进 canonical state.json
    state = init_state(plan.target_run, k=int(state_payload["k"]),
                       seeds=(), harness_version=int(state_payload["harness_version"]),
                       budget_cap=int((state_payload.get("budget") or {})
                                      .get("eval_episodes_cap", 0)),
                       memory_version=int(state_payload["memory_version"]))
    state.phase = str(state_payload["phase"])
    state.generation = int(state_payload["generation"])
    state.tick = int(state_payload["tick"])
    state.completed_train_seeds = [int(s) for s in state_payload["completed_train_seeds"]]
    state.pending_queue = []
    state.last_completed_cycle = int(state_payload["last_completed_cycle"])
    state.evolving_cycle = None
    state.hitl_pending = None
    state.counters = RunCounters.from_dict(state_payload.get("counters"))
    state.budget = RunBudget.from_dict(state_payload.get("budget"))
    save_state(RunPaths(dst), state)
    report.written.append("state.json")

    write_json_atomic(dst / REPORT_NAME, report.as_dict())
    report.written.append(REPORT_NAME)
    return report


# =====================================================================
# 导入后校验（只读；用**生产代码自己的读路径**核对，而不是自己再数一遍）
# =====================================================================

def verify_import(plan: ImportPlan, *, memory_config: Any = None) -> dict[str, Any]:
    """核对已导入的 run：训练局能被 `evals` 的**生产读路径**取到、记忆能 load、状态自洽。

    **为什么必须用生产读路径**：导入的全部意义是"下一代的 train 段拿得到这 30 局"。
    自己再写一遍"数文件"的校验会与 `load_episode_results` 的实际过滤条件（代际标签 +
    `arm="train"` + seed 块）脱钩——那正是 F64 当初坏掉的地方。
    """
    from ..evals.harness import load_episode_results
    from ..evals.seeds import coerce_protocol, take, validate_protocol

    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})

    dst = Path(plan.target_root)
    proto = validate_protocol(coerce_protocol(None))

    # 1) evals 的生产读路径：每一代都能按 `take("train", g)` + `arm="train"` 取到 K 局
    for item in plan.generations:
        want = take(TRAINING_SPLIT, generation=int(item.generation), protocol=proto)
        got = load_episode_results(dst, TRAINING_SPLIT, generation=int(item.generation),
                                   harness_version=int(plan.state.get("harness_version", 0)),
                                   seeds=want, arm=TRAINING_ARM)
        seeds = sorted(int(r.seed) for r in got)
        add(f"train 段可读（gen-{item.generation:03d}）",
            len(got) == len(item.seeds) and seeds == sorted(int(s) for s in item.seeds),
            f"evals 读到 {len(got)}/{len(item.seeds)} 局，seed 集合"
            f"{'一致' if seeds == sorted(int(s) for s in item.seeds) else '不一致'}")

    # 2) state.json：completed/completed_train_seeds 与 0 局 pending
    from ..infra.run import RunPaths
    from .state import load_state

    state = load_state(RunPaths(dst))
    want_completed = [int(s) for item in plan.generations for s in item.seeds]
    add("state.completed_train_seeds", state.completed_train_seeds == want_completed,
        f"{len(state.completed_train_seeds)} 个（期望 {len(want_completed)}）")
    add("state.pending_queue 为空", not state.pending_queue,
        f"pending={len(state.pending_queue)}（起跑时由 --seeds 重算）")
    add("state.last_completed_cycle", state.last_completed_cycle == len(plan.generations),
        f"last_completed_cycle={state.last_completed_cycle}（期望 {len(plan.generations)}）")

    # 3) 反思台账 + 记忆库：能被生产代码 load 出来（不是"文件在那儿"）
    refs_path = dst / "reflections" / "reflections.jsonl"
    n_refs = sum(1 for line in refs_path.read_text(encoding="utf-8").splitlines() if line.strip()) \
        if refs_path.is_file() else 0
    want_refs = sum(len(item.reflections) for item in plan.generations)
    add("reflections.jsonl 行数", n_refs == want_refs, f"{n_refs} 行（期望 {want_refs}）")

    if memory_config is None:
        try:                       # 生产读路径：与起跑用的 configs/ 同一份 memory 段
            from ..infra.config import load_config

            memory_config = load_config(config_dir=Path("configs")).memory
        except Exception:  # noqa: BLE001 - 配置读不到就跳过这条（其余校验仍照做）
            memory_config = None
    if memory_config is not None:
        from ..memory.store import MemoryStore

        store = MemoryStore(dst, memory_config, run_id=plan.target_run)
        add("MemoryStore 可构造", True,
            f"entries={len(store.entries())} memory_version={store.memory_version} "
            f"run_id={store.run_id}")
        add("memory_version 与 state 一致", int(store.memory_version) == int(state.memory_version),
            f"store={store.memory_version} state={state.memory_version}")

    # 4) 报告落盘
    add("import_prior.json 已落盘", (dst / REPORT_NAME).is_file(), str(dst / REPORT_NAME))
    return {"checks": checks, "ok": all(c["ok"] for c in checks)}
