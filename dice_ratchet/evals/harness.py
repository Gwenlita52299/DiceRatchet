"""MOD-08 评估执行器与 MOD-07 门面（PRD §6/§7/§8，Spec §4.7）。

对外公开（Spec §4.7 / FR-08-25）::

    run_eval(spec, *, runner, writer, protocol) -> EvalResult          # 执行器
    evaluate_arm(harness_dir, *, split, basket, memory_version, arm, run_id) -> ArmResult
    compare_paired(a, b) -> PairedTest                                # 唯一统计口径
    evaluate_generation(gen, parent, winner, *, run_id) -> dict       # report 指标段

关键纪律：

- **seed 只从** ``evals.seeds.take/resolve_basket`` 来（INV-5）；
- **dropped 计 n 且按 lose 计**，``degraded`` 剔出 n 并单列（Spec v1.3 §8.5）；
- **单局预算真被消费**（F20）：``SandboxRunner`` 逐步检查 deadline，``run_eval`` 另有一道
  硬看门狗；超时 → ``dropped`` + ``E_EVAL_TIMEOUT``；
- **逐局增量落盘**（F18）：``on_result`` 钩子让调用方每局落一次 ``episodes/<split>/``，
  长跑崩溃只丢当前局；
- 结果**按 seed 升序**归一化聚合，``n_workers=1`` 与 ``n>1`` 必须逐字节一致（G3/T-15）；
- 报告落盘 ``report.json`` + ``report.sha256``；**G3 按臂分层执行**（2026-10-11 D-M3-3 裁决 A）：
  **本地臂**同参数重跑 sha 不一致 → ``E_EVAL_NONDETERMINISTIC``；**API 臂**（``api_supports_seed=false``，
  实测字节相同的请求 9 次返回 9 个不同响应）端到端逐字节可复现**物理上不成立** → 跳过硬门并发
  ``eval.nondeterministic``，不抛异常。生产执行点是 ``enforce_report_determinism``——调用方若不显式
  调用它，G3 就只是一句文档（这正是 2026-10-11 之前的状态：``check_report_determinism``
  从 M1 起没有任何生产调用点）。
"""

from __future__ import annotations

import concurrent.futures
import csv
import hashlib
import io
import json
import re
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import (Any, Callable, Final, Iterable, Literal, Mapping, Protocol, Sequence,
                    runtime_checkable)

from dice_ratchet.infra import plotting as plotting_mod
from dice_ratchet.infra.errors import E, InfraError
from dice_ratchet.infra.io import read_json, read_jsonl, sha256_file, write_json_atomic

from . import seeds as seeds_mod
from .metrics import (MAIN_METRICS, EpisodeResult, GainVerdict, PairedTest, SplitMetrics,
                      Split, BasketId, aggregate, bootstrap_seed, judge_gain,
                      metric_value, mcnemar_exact_p, paired_bootstrap_gap, power_note,
                      render_report_sections)
from .diversity import CollapseConfig, collapse_check, js_divergence, kind_distribution, \
    slot_distribution, distinct_sequence_ratio

__all__ = [
    "EvalSpec", "EvalResult", "TraceWriter", "EpisodeRunner", "ArmResult",
    "run_eval", "evaluate_arm", "compare_paired", "compare_all_metrics",
    "evaluate_generation", "SandboxRunner", "episode_result_from_rows",
    "load_episode_results", "gather_results", "read_episode_protocols",
    "restore_arm_from_archive", "arms_dir", "arm_archive_path", "load_arm_archive",
    "generation_report_path",
    "report_sha_path", "write_generation_report", "check_report_determinism",
    "enforce_report_determinism", "reproducibility_of",
    "append_generations_csv", "generations_rows_from_report", "ledger_and_plots",
    "generations_row_from_report", "write_ext_metrics", "write_plots_data",
    "write_episode_summaries", "PARENT_ARM", "PARENT_SMOKE_ARM", "arm_of_row",
    "GENERATIONS_COLUMNS", "rng_for", "GainVerdictDiversity",
]

#: MOD-07 写进 ``generations/gen-NNN/report.json`` 的**真实判决**（§6.3 决策段）。
#: 用于「重算报告 / 台账不得覆盖已裁决的代」（F43）：``eval report`` 重算出的
#: ``decision`` 恒为 ``baseline``/``pending``，不能盖掉这几个。
#: ``error_reject`` 是 F47-D 新增的**基础设施失败**判决（accept/rollback 抛
#: ``InfraError``：git 提交失败、版本号冲突…）：它与「候选不达标」必须区分开，
#: 否则一次脏工作区在报告里长得像一次合法拒绝。
VERDICT_DECISIONS: Final[tuple[str, ...]] = ("accept", "reject", "rollback", "error_reject")

#: ``metrics/generations.csv`` 的固定列（PRD §13；**口径**归 MOD-08，B-06）。
#:
#: **2026-10-11（F-M3-3 裁决 A）**：这份清单**不再是独立定义**，而是**直接取**
#: ``infra.plotting.GENERATIONS_CSV_COLUMNS``——修复前两处各写一份，分叉出 4 个列名
#: （``heldout_win_rate`` 等），而绘图侧 ``_g()`` 缺列时静默返回空 ⇒ **5 张空白 PNG**
#: 被「文件存在」的判据当成 M3 交付物。权威定义落在 ``infra``（IO 层）是因为分层门禁
#: 禁止 ``infra → evals``（NFR-09-5），而 ``evals → infra`` 合法。**口径仍归 MOD-08**：
#: 改列要同时改这里的语义说明与 MOD-08 §13 / MOD-09 FR-09-13。
GENERATIONS_COLUMNS: tuple[str, ...] = tuple(plotting_mod.GENERATIONS_CSV_COLUMNS)

#: 起点臂名（F57，2026-10-12）。代际台账与判据 1 只读**起点臂**的行：这一代的
#: ``heldout`` 数字是「起点在哪」，候选自己的测量在 ``metrics/arms/`` 与
#: ``candidates/*.json``。``arm`` 列缺失的旧行按起点处理（那时每代只有一行）。
PARENT_ARM: Final[str] = "parent"
#: 起点臂的冒烟臂名（``evolve/judge.py::CandidateEvaluator.parent_arms`` 同口径）。
PARENT_SMOKE_ARM: Final[str] = "parent-smoke"


def arm_of_row(row: Mapping[str, Any]) -> str:
    """台账行的臂名；**缺列/空值一律归为 ``PARENT_ARM``**（F57 兼容旧台账）。

    F57 之前的台账没有 ``arm`` 列，而那时每代每 split 恰好一行、写的就是起点臂——
    按起点处理既向后兼容，也避免旧行被当成「未知臂」而在判据里消失。
    """
    return str(row.get("arm") or "").strip() or PARENT_ARM

#: 单局硬看门狗的宽限（秒）。协同预算（``SandboxRunner`` 里的 deadline）已是第一道闸；
#: 这里只兜住「worker 完全不消费 ``timeout_s``」的极端情形，宽限留给已进入的单次
#: LLM 调用收尾（MOD-03 自身有 ``generate_timeout_ms`` / ``total_timeout_ms`` 上限）。
_TIMEOUT_SLACK_S: Final[float] = 60.0
#: 硬看门狗的巡检粒度（秒）。只影响「放弃一个卡死 worker」的判定延迟。
_WATCHDOG_POLL_S: Final[float] = 0.5

#: 逐局摘要的「测量协议」标识（随行落盘到 ``episodes/<split>/*.jsonl``）。
#:
#: 只要**单局语义**变了（推理粒度、超时语义、指标口径、prompt 契约……）就必须递增它：
#: ``baseline --resume`` 会拒绝与不同协议的局混进同一份聚合，否则长跑（M3 ≈114 h）跨代码
#: 改动续跑时，会把两套语义的成绩拼成一个「没有物理含义的数字」（M1 验收 F18 的隐患：
#: F22 修复前的局是逐 env 步推理，修复后是逐决策点，二者不可混算）。
#: v2 = F22（决策点粒度）+ F20（单局预算真被执行）之后的语义。
EPISODE_PROTOCOL: Final[str] = "episode-protocol-v2"


# =====================================================================
# 执行器（FR-08-07…10）
# =====================================================================

@dataclass(frozen=True)
class EvalSpec:
    """一次评估的不可变规格（PRD §6）。

    ``temperature`` 恒为 0.0（Spec §8.1 B-14，MOD-03 强制），所以**不设配置键**。
    """

    run_id: str
    generation: int
    harness_version: int
    split: Split
    basket: BasketId | tuple[int, ...]
    seeds: tuple[int, ...]
    agent_kind: str = "llm"
    max_steps: int | None = None
    n_rep: int = 1
    n_workers: int = 1
    # 单局预算（秒）。2026-10-04 实测回填；2026-10-05（M1 验收 F20）起**真正被消费**：
    # ``SandboxRunner`` 在每个决策点前检查 deadline（协同预算），``run_eval`` 另有一道
    # 硬看门狗（``seed_timeout_s + _TIMEOUT_SLACK_S``）兜住「worker 完全不看预算」的情形。
    # 超时的局按 Spec §8.5 记 ``dropped=True`` + ``E_EVAL_TIMEOUT``，计 n 并按 lose 计。
    seed_timeout_s: int = 14400
    budget_seconds: int | None = None
    t_max: int = 120
    min_support: int = 3
    drop_rate_max: float = 0.10
    arm: str = ""
    memory_version: int | None = None
    episode_config_hash: str | None = None

    def __post_init__(self) -> None:
        if self.n_workers < 1:
            raise InfraError(E.E_EVAL_SCHEMA, "n_workers 必须 >= 1")
        if self.n_rep < 1:
            raise InfraError(E.E_EVAL_SCHEMA, "n_rep 必须 >= 1")

    @property
    def basket_id(self) -> str:
        return self.basket if isinstance(self.basket, str) else "explicit"

    @property
    def rng_seed(self) -> int:
        return bootstrap_seed(self.run_id, self.generation, self.basket_id)

    def as_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "generation": self.generation,
                "harness_version": self.harness_version, "split": self.split,
                "basket": self.basket_id, "seeds": list(self.seeds),
                "agent_kind": self.agent_kind, "n_rep": self.n_rep,
                "n_workers": self.n_workers, "seed_timeout_s": self.seed_timeout_s,
                "budget_seconds": self.budget_seconds, "arm": self.arm,
                "memory_version": self.memory_version,
                "episode_config_hash": self.episode_config_hash}


@dataclass(frozen=True)
class EvalResult:
    """一次评估的汇总（``run_eval`` 的返回）。"""

    spec: EvalSpec
    results: tuple[EpisodeResult, ...]
    metrics: SplitMetrics
    dur_s: float
    rng_seed: int
    error_codes: tuple[str, ...] = ()
    budget_exceeded: bool = False

    @property
    def low_confidence(self) -> bool:
        return bool(self.metrics.low_confidence)

    def raise_if_low_confidence(self) -> None:
        """显式把低置信升格为异常（FR-08-10 的"抛 E_EVAL_INSUFFICIENT_N"）。

        ``eval report`` 不调用它——§10 要求低置信仍出报告；测试与严格调用方可调用。
        """
        if self.low_confidence:
            raise InfraError(E.E_EVAL_INSUFFICIENT_N,
                             f"有效 n={self.metrics.n}，dropped={self.metrics.n_dropped}"
                             f"（超 drop_rate_max），结论不可采信",
                             hint="报告已标 low_confidence=true，verdict 只能为 null/insufficient/regress")

    def as_dict(self) -> dict[str, Any]:
        return {"spec": self.spec.as_dict(), "metrics": self.metrics.as_dict(),
                "dur_s": self.dur_s, "rng_seed": self.rng_seed,
                "error_codes": list(self.error_codes),
                "budget_exceeded": self.budget_exceeded,
                "n_results": len(self.results)}


@runtime_checkable
class TraceWriter(Protocol):
    """轨迹/summary 落盘出口（MOD-06 的 ``JsonlWriter`` 满足此协议）。"""

    def write(self, row: Mapping[str, Any]) -> None: ...


@runtime_checkable
class EpisodeRunner(Protocol):
    """局执行器（MOD-06 提供 ``run_episode``；本模块只消费 ``EpisodeResult``）。"""

    def run_episode(self, seed: int, *, split: Split, arm: str, generation: int,
                    harness_version: int, episode_config: Any = None,
                    timeout_s: float | None = None) -> EpisodeResult: ...


def _emit(event: str, **fields: Any) -> None:
    try:
        from dice_ratchet.infra import events

        events.emit(event, **fields)
    except Exception: # 埋点失败不得影响测量结果（NFR-08-04）
        pass


def _dropped_result(spec: EvalSpec, seed: int, error_code: str) -> EpisodeResult:
    """超时/崩溃的局：**计入 n 并按 lose 计**（Spec v1.3 §8.5，防幸存者偏差）。"""
    return EpisodeResult(
        seed=int(seed), split=spec.split, generation=spec.generation,
        harness_version=spec.harness_version, outcome="lose", truncation_reason=None,
        stages_cleared=0, hearts_left=0, battles_won=0, decisions=0, steps=0,
        illegal_attempts=0, fallback_decisions=0,
        kind_counts=tuple([0] * 16), slot_counts={},
        seq_hash=hashlib.sha256(f"dropped:{seed}".encode("utf-8")).hexdigest(),
        dropped=True, error_code=error_code, arm=spec.arm)


def _majority(results: Sequence[EpisodeResult]) -> EpisodeResult:
    """``n_rep`` 多数票（后端非确定时的降级路径，FR-08-09）。"""
    if not results:
        raise InfraError(E.E_EVAL_INSUFFICIENT_N, "多数票需要至少一次重复")
    wins = sum(1 for r in results if r.win)
    target = "win" if wins * 2 > len(results) else "lose"
    for result in results:
        if result.outcome == target and not result.dropped:
            return result
    return results[0]


def rng_for(spec: EvalSpec) -> Any:
    """按 NFR-08-01 生成 bootstrap rng（``np.random.default_rng(rng_seed)``）。"""
    import numpy as np

    return np.random.default_rng(spec.rng_seed)


def run_eval(spec: EvalSpec, *, runner: EpisodeRunner, writer: TraceWriter | None = None,
             protocol: seeds_mod.SeedProtocol | Any = None,
             resume_results: Sequence[EpisodeResult] = (),
             on_result: Callable[[EpisodeResult], None] | None = None) -> EvalResult:
    """执行一次评估：并行跑局 → 超时/崩溃记 dropped → 按 seed 升序聚合（FR-08-07…10）。

    两个可选钩子支撑长跑（M1 验收 F18/F19/F20）：

    - ``resume_results``：``--resume`` 时**已完成**的局（不重跑，直接并入聚合，INV-5）；
    - ``on_result``：每完成一局**立刻**回调（MOD-08 的 ``baseline --resume`` 用它逐局增量
      落盘 ``episodes/<split>/*.jsonl``）——进程崩溃/断电只丢当前那一局，不会废掉整个 run。
    """
    started = time.perf_counter()
    cpu_started = time.process_time()
    proto = seeds_mod.validate_protocol(seeds_mod.coerce_protocol(protocol))
    seeds_mod.assert_split_isolation(spec.split, spec.seeds, proto)
    _emit("eval.start", split=spec.split, basket=spec.basket_id,
          generation=spec.generation, harness_version=spec.harness_version,
          n=len(spec.seeds), n_workers=spec.n_workers, resumed_n=len(resume_results),
          seed_timeout_s=spec.seed_timeout_s, spec=spec.as_dict())

    results: dict[int, EpisodeResult] = {int(r.seed): r for r in resume_results}
    budget_exceeded = False
    timed_out = False
    abandoned = False
    error_codes: list[str] = []

    started_at: dict[int, float] = {}
    _lock = threading.Lock()

    def _one(seed: int) -> EpisodeResult:
        with _lock:
            started_at[int(seed)] = time.monotonic()
        reps: list[EpisodeResult] = []
        for _ in range(max(1, spec.n_rep)):
            reps.append(runner.run_episode(
                seed, split=spec.split, arm=spec.arm, generation=spec.generation,
                harness_version=spec.harness_version, timeout_s=spec.seed_timeout_s))
        return _majority(reps) if len(reps) > 1 else reps[0]

    def _store(seed: int, result: EpisodeResult) -> None:
        results[int(seed)] = result
        begun = started_at.get(int(seed))
        _emit("eval.seed_done", seed=int(seed), outcome=result.outcome,
              steps=result.steps, decisions=result.decisions,
              decisions_llm=result.decisions_llm,
              dur_s=round(time.perf_counter() - started, 4),
              episode_s=(None if begun is None else round(time.monotonic() - begun, 4)),
              cpu_s=round(time.process_time() - cpu_started, 4),
              dropped=result.dropped, degraded=result.degraded,
              error_code=result.error_code)
        if on_result is not None: # F18：逐局增量落盘（崩溃只丢当前局）
            on_result(result)
        if writer is not None:
            writer.write({"type": "episode_end", "split": spec.split,
                          "arm": spec.arm, **result.as_dict()})

    # F20 的硬看门狗：协同预算归 runner；这里只兜住「worker 无视 timeout_s」。
    # 宽限 _TIMEOUT_SLACK_S 留给「已进入单次 LLM 调用」的收尾（MOD-03 单调用自带上限）；
    # ``n_rep`` 次重复是**串行**跑在同一 worker 上，所以上限也要乘 n_rep，否则会把
    # 正常执行的多数票局误判成超时。
    hard_limit = (None if spec.seed_timeout_s is None
                  else float(spec.seed_timeout_s) * max(1, spec.n_rep) + _TIMEOUT_SLACK_S)
    workers = max(1, spec.n_workers)
    pools: list[concurrent.futures.ThreadPoolExecutor] = []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    pools.append(pool)
    futures: dict[concurrent.futures.Future[EpisodeResult], int] = {}
    # F42-A：``resume_results`` 的契约是「已完成的局**不重跑**」。这里必须**过滤队列**，
    # 而不是只把它们并进聚合字典：只并入的话 worker 仍会把同一个 seed 跑一遍，最后被
    # ``_store`` 覆盖 —— 钱已经花了，而且 `n_workers>1` 时还会与已完成的局交错。
    queued = [int(s) for s in spec.seeds if int(s) not in results]
    abandoned_workers = 0

    def _pump() -> None:
        """把排队中的 seed 提交到当前池，最多 ``workers`` 个在飞（与旧行为等价）。"""
        while queued and len(futures) < workers:
            seed = queued.pop(0)
            _emit("eval.seed_start", seed=seed, split=spec.split, basket=spec.basket_id)
            futures[pool.submit(_one, seed)] = seed

    try:
        _pump()
        while futures:
            done, _ = concurrent.futures.wait(
                list(futures), timeout=_WATCHDOG_POLL_S,
                return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                seed = futures.pop(future)
                if spec.budget_seconds is not None and \
                        (time.perf_counter() - started) > float(spec.budget_seconds):
                    budget_exceeded = True
                    _store(seed, _dropped_result(spec, seed, str(E.E_EVAL_BUDGET_EXCEEDED)))
                    continue
                try:
                    _store(seed, future.result())
                except Exception as exc: # 崩溃/TLE/沙箱异常 → dropped，不静默
                    _store(seed, _dropped_result(spec, seed, _error_code_of(exc)))
            if hard_limit is not None:
                now = time.monotonic()
                for future, seed in list(futures.items()):
                    begun = started_at.get(seed)
                    if begun is None or (now - begun) <= hard_limit:
                        continue
                    # 看门狗接管：不再等这个 worker。``future.cancel()`` 对已运行的线程无效，
                    # 该线程由 MOD-03 的调用级超时收尾；它占掉的 worker 名额用一个新池补回来，
                    # 否则 n_workers=1 时「一个卡死的 worker」会让其余 seed 永远排不上队
                    # （那正是 F20 描述的「eval 永久挂起」）。
                    futures.pop(future)
                    future.cancel()
                    abandoned = True
                    timed_out = True
                    abandoned_workers += 1
                    _store(seed, _dropped_result(spec, seed, str(E.E_EVAL_TIMEOUT)))
                    pool.shutdown(wait=False)
                    pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
                    pools.append(pool)
                    break # 每次只放弃一个，避免对同一个池反复 shutdown
            _pump()
    finally:
        # 有被放弃的 worker 时**不能** wait=True：那正是「一个卡住的 worker 让 eval 永久挂起」
        for item in pools:
            item.shutdown(wait=not abandoned)

    ordered = tuple(results[seed] for seed in sorted(results))
    metrics = aggregate(ordered, split=spec.split, basket=spec.basket_id, t_max=spec.t_max,
                        min_support=spec.min_support, drop_rate_max=spec.drop_rate_max)
    if metrics.low_confidence:
        error_codes.append(str(E.E_EVAL_INSUFFICIENT_N))
    if budget_exceeded:
        error_codes.append(str(E.E_EVAL_BUDGET_EXCEEDED))
    if timed_out:
        error_codes.append(str(E.E_EVAL_TIMEOUT))
    dur_s = round(time.perf_counter() - started, 4)
    _emit("eval.report", generation=spec.generation, n=metrics.n,
          n_dropped=metrics.n_dropped, degraded_n=metrics.degraded_n, dur_s=dur_s,
          cpu_s=round(time.process_time() - cpu_started, 4),
          report_sha256=None, low_confidence=metrics.low_confidence,
          abandoned_workers=abandoned_workers, error_codes=error_codes)
    return EvalResult(spec=spec, results=ordered, metrics=metrics, dur_s=dur_s,
                      rng_seed=spec.rng_seed, error_codes=tuple(error_codes),
                      budget_exceeded=budget_exceeded)


def _error_code_of(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    return str(code) if code else type(exc).__name__


# =====================================================================
# 轨迹 → EpisodeResult（eval report 复用；MOD-06 落盘格式，Spec §4.5）
# =====================================================================

def episode_result_from_rows(rows: Sequence[Mapping[str, Any]], *, seed: int, split: Split,
                             generation: int | None = None,
                             harness_version: int = 0,
                             arm: str = "") -> EpisodeResult | None:
    """从 ``episodes/<split>/*.jsonl`` 的 ``episode_end`` 行还原一局（无则 ``None``）。

    逐局汇总字段优先读 Spec §4.5 的裸键（``write_episode_summaries`` 的写法），缺失时
    回落到 ``x_`` 前缀扩展位——MOD-06 的真实轨迹按 ``trajectory_line`` schema 的
    ``EpisodeEnd`` 分支落盘（``additionalProperties:false``），只能把 MOD-08 复算多样性
    所需的 ``kind_counts/decision_kinds/slot_counts/seq_hash`` 放在 schema 预留的
    ``^x_`` 扩展位里。两条来源因此产出同一份 ``EpisodeResult``。

    ``generation``（F46-A）：**行里的** ``generation`` 是权威值；``generation`` 形参只在
    行里缺该字段时兜底（旧工件的调用方通常已从文件名解析出真值）。修复前这里恒用形参，
    于是 ``eval report --generation 1`` 会把 g000 的局贴上 ``generation=1`` 的标签 ——
    「跨代混算」正是靠这个覆盖行为逃过所有过滤的。
    """
    end: Mapping[str, Any] | None = None
    for row in rows:
        if row.get("type") == "episode_end":
            end = row
    if end is None:
        return None

    def _pick(name: str, default: Any = None) -> Any:
        if name in end:
            return end[name]
        return end.get(f"x_{name}", default)

    resolved_generation = end.get("generation")
    if resolved_generation is None:
        resolved_generation = generation
    outcome = "win" if str(end.get("outcome")) == "win" else "lose"
    truncation = end.get("truncation_reason")
    kind_raw = list(_pick("kind_counts") or [])
    return EpisodeResult(
        seed=int(end.get("seed", seed)), split=split,
        generation=int(resolved_generation or 0),
        harness_version=int(harness_version), outcome=outcome,
        truncation_reason=(str(truncation) if truncation else None),
        stages_cleared=int(end.get("stages_cleared") or 0),
        hearts_left=int(end.get("hearts_left") or 0),
        battles_won=int(end.get("battles_won") or 0),
        decisions=int(end.get("decisions") or 0), steps=int(_pick("steps") or 0),
        illegal_attempts=int(_pick("illegal_attempts") or 0),
        fallback_decisions=int(_pick("fallback_decisions") or 0),
        kind_counts=tuple(int(x) for x in kind_raw)[:16]
        + tuple([0] * max(0, 16 - len(kind_raw))),
        slot_counts=dict(_pick("slot_counts") or {}),
        seq_hash=str(_pick("seq_hash") or ""), dropped=bool(_pick("dropped", False)),
        degraded=bool(end.get("degraded", False)),
        decision_kinds=tuple(int(x) for x in (_pick("decision_kinds") or [])),
        arm=arm, decisions_llm=int(_pick("decisions_llm") or 0))


#: ``episodes/<split>/e-{seed:06d}-g{gen:03d}-{arm}.jsonl`` 的文件名（Spec §4.5 B-16）。
#: F46-A：行里缺 ``generation`` 的旧工件退化为按文件名解析。
_EPISODE_FILE_RE: Final[re.Pattern[str]] = re.compile(r"-g(\d{3,})-")


def _generation_from_name(path: Path) -> int | None:
    """从 ``e-002000-g001-parent.jsonl`` 解析 ``generation``（判不出 → ``None``）。"""
    match = _EPISODE_FILE_RE.search(path.stem)
    return int(match.group(1)) if match else None


def _resolve_generation(rows: Sequence[Mapping[str, Any]], path: Path) -> int | None:
    """一局文件所属的代数：**行里的 ``generation`` 优先**，缺失时解析文件名（F46-A）。

    两者都判不出 → ``None``：调用方必须**跳过**（不得静默混入别的代）。
    """
    for row in rows:
        if row.get("type") == "episode_end":
            value = row.get("generation")
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    break
            break
    return _generation_from_name(path)


def load_episode_results(run_dir: Path | str, split: Split, *,
                         generation: int | None = None,
                         harness_version: int = 0,
                         seeds: Sequence[int] | None = None,
                         arm: str | Sequence[str] = "",
                         exclude_arms: Sequence[str] = ()) -> list[EpisodeResult]:
    """读取 ``runs/<run_id>/episodes/<split>/*.jsonl``（``eval report`` 的数据来源）。

    ``arm`` / ``exclude_arms`` 是**真过滤**（不是打标签）：``episodes/<split>/`` 下同时
    住着 ``parent`` / ``candidates`` / ``smoke`` / ``baseline-*`` 四类局，混臂聚合会得出
    一个没有任何物理含义的数字（M1 验收 F5：实测 ``heldout.n=120``、``win_rate=0.30``）。
    返回的 ``EpisodeResult.arm`` 取**行里的** ``arm`` 字段（缺省时回落到请求值）。

    ``generation``（F46-A）同样是**真过滤**：给定 N 时只保留第 N 代的局（行里的
    ``generation`` 优先，缺失时按文件名 ``-g{gen:03d}-`` 解析；两者都判不出 → 跳过并
    发 ``evals.generation_unresolved`` 可观测事件）。**不再覆盖**
    ``EpisodeResult.generation``——修复前 ``eval report --generation 1`` 会把
    g000-parent(40) + g001-parent(40) + g001-cand(37) 聚成一个 ``n=117``
    （``win_rate=0.6496``），而真实分布是 parent 40 + 候选 37 两套口径。
    """
    root = Path(run_dir) / "episodes" / split
    if not root.is_dir():
        return []
    wanted = {int(s) for s in seeds} if seeds else None
    if isinstance(arm, str):
        want_arms: set[str] | None = {arm} if arm else None
        label = arm
    else:
        want_arms = {str(a) for a in arm} or None
        label = ""
    excluded = {str(a) for a in exclude_arms}
    out: list[EpisodeResult] = []
    unresolved: list[str] = []
    for path in sorted(root.glob("*.jsonl")):
        rows, _truncated = read_jsonl(path)
        if not rows:
            continue
        file_generation = _resolve_generation(rows, path)
        if generation is not None and file_generation != int(generation):
            if file_generation is None:
                unresolved.append(path.name)
            continue
        row_arm = str(rows[0].get("arm") or "")
        if want_arms is not None and row_arm not in want_arms:
            continue
        if excluded and row_arm in excluded:
            continue
        seed = int(rows[0].get("seed") or 0)
        if wanted is not None and seed not in wanted:
            continue
        result = episode_result_from_rows(
            rows, seed=seed, split=split, generation=file_generation,
            harness_version=int(harness_version), arm=row_arm or label)
        if result is not None:
            out.append(result)
    if unresolved: # 不静默：判不出代数的旧工件被跳过（F46-A）
        _emit("evals.generation_unresolved", split=split, generation=int(generation or 0),
              n=len(unresolved), files=sorted(unresolved)[:10])
    return sorted(out, key=lambda r: r.seed)


def gather_results(run_dir: Path | str, split: Split, *, generation: int | None = None,
                   harness_version: int = 0, seeds: Sequence[int] | None = None) -> tuple[EpisodeResult, ...]:
    return tuple(load_episode_results(run_dir, split, generation=generation,
                                      harness_version=harness_version, seeds=seeds))


def read_episode_protocols(run_dir: Path | str, split: Split, *,
                           generation: int | None = None,
                           arm: str = "") -> set[str]:
    """已落盘各局的「测量协议」标识集合（``baseline --resume`` 的一致性检查用）。

    旧版（F22 修复前）的局没有 ``protocol`` 字段，会以 ``""`` 出现——正是要靠它拦住的混算。

    ``generation`` 是**真过滤**（F46-A），口径与 ``load_episode_results`` 一致：行里的
    ``generation`` 优先，缺失时解析文件名；判不出代数的文件在给定 ``generation`` 时被跳过。
    """
    root = Path(run_dir) / "episodes" / split
    if not root.is_dir():
        return set()
    found: set[str] = set()
    for path in sorted(root.glob("*.jsonl")):
        rows, _truncated = read_jsonl(path)
        if not rows:
            continue
        if arm and str(rows[0].get("arm") or "") != arm:
            continue
        if generation is not None and _resolve_generation(rows, path) != int(generation):
            continue
        found.add(str(rows[0].get("protocol") or ""))
    return found


# =====================================================================
# MOD-07 门面（FR-08-25 / Spec §4.7）
# =====================================================================

@dataclass(frozen=True)
class ArmResult:
    """单臂评估结果（Spec §4.7 ``evaluate_arm`` 的返回）。"""

    arm: str
    split: Split
    basket: str
    harness_version: int
    memory_version: int
    run_id: str
    results: tuple[EpisodeResult, ...]
    metrics: SplitMetrics
    model_hash: str = ""
    error_codes: tuple[str, ...] = ()
    dur_s: float = 0.0
    # ---- 跑批事实（F6 溯源 / F18 续跑；由 ``run_baseline`` 填写，其它调用方留空）----
    resumed_n: int = 0
    pending_seeds: tuple[int, ...] = ()
    model_backend: str = ""
    seed_timeout_s: int | None = None
    archive_path: str = ""

    @property
    def seeds(self) -> tuple[int, ...]:
        return tuple(sorted(r.seed for r in self.results))

    def by_seed(self) -> dict[int, EpisodeResult]:
        return {r.seed: r for r in self.results}

    @property
    def win_rate(self) -> float:
        return self.metrics.win_rate

    def as_dict(self) -> dict[str, Any]:
        return {"arm": self.arm, "split": self.split, "basket": self.basket,
                "harness_version": self.harness_version,
                "memory_version": self.memory_version, "run_id": self.run_id,
                "model_hash": self.model_hash, "metrics": self.metrics.as_dict(),
                "error_codes": list(self.error_codes), "dur_s": self.dur_s,
                "seeds": list(self.seeds), "resumed_n": self.resumed_n,
                "pending_seeds": list(self.pending_seeds),
                "model_backend": self.model_backend,
                "seed_timeout_s": self.seed_timeout_s,
                "archive_path": self.archive_path}


def _align(a: ArmResult, b: ArmResult) -> tuple[list[EpisodeResult], list[EpisodeResult]]:
    """按 seed 取交集并排序（配对检验要求同一 seed 集合）。"""
    b_map = b.by_seed()
    left: list[EpisodeResult] = []
    right: list[EpisodeResult] = []
    for result in sorted(a.results, key=lambda r: r.seed):
        match = b_map.get(result.seed)
        if match is not None:
            left.append(result)
            right.append(match)
    return left, right


def compare_paired(a: ArmResult, b: ArmResult, metric: str = "win_rate", *,
                   alpha: float = 0.05, B: int = 10000, rng: Any = None) -> PairedTest:
    """配对检验（Spec §4.7）：胜率用 McNemar 精确检验，连续量用配对 bootstrap。

    ``a`` 为候选、``b`` 为 parent；``delta = metric(a) - metric(b)``。
    """
    left, right = _align(a, b)
    if not left:
        raise InfraError(E.E_EVAL_INSUFFICIENT_N,
                         "配对检验没有共同 seed：两个 ArmResult 的 seed 集不相交",
                         hint="parent 与候选必须在同一篮子（H1）上评估（Spec §8.6）")
    if metric == "win_rate":
        p = mcnemar_exact_p([r.win for r in left], [r.win for r in right], "two-sided")
        p_up = mcnemar_exact_p([r.win for r in left], [r.win for r in right], "greater")
        delta = metric_value(left, "win_rate") - metric_value(right, "win_rate")
        method = "mcnemar"
    elif metric in MAIN_METRICS or metric in ("mean_battles_won", "mean_decisions", "illegal_rate"):
        if rng is None:
            rng = rng_for(EvalSpec(run_id=a.run_id, generation=0,
                                   harness_version=a.harness_version, split=a.split,
                                   basket=a.basket, seeds=a.seeds))
        values_a = [_metric_of(r, metric) for r in left]
        values_b = [_metric_of(r, metric) for r in right]
        delta, ci = paired_bootstrap_gap(values_a, values_b, B=B, rng=rng)
        # 双侧近似：CI 不含 0 ⇒ 显著（NFR-08-02 的"点估计 + 区间 + 检验"）
        p = 0.0 if (ci[0] > 0.0 or ci[1] < 0.0) else 1.0
        p_up = p
        method = "paired_bootstrap"
    else:
        raise InfraError(E.E_EVAL_SCHEMA, f"未知指标 {metric!r}",
                         hint=f"合法值：win_rate｜{'｜'.join(MAIN_METRICS)}")
    significant = p < alpha and abs(delta) > 0.0
    direction: Literal["up", "down", "flat"]
    if not significant or delta == 0.0:
        direction = "flat"
    else:
        direction = "up" if delta > 0 else "down"
    return PairedTest(method=method, metric=metric, p=p, delta=delta,
                      significant=significant, direction=direction, n_pairs=len(left))


def _metric_of(result: EpisodeResult, metric: str) -> float:
    table = {
        "mean_stages_cleared": result.stages_cleared,
        "mean_hearts_left": result.hearts_left,
        "mean_battles_won": result.battles_won,
        "mean_decisions": result.decisions,
        "illegal_rate": (result.illegal_attempts /
                         max(1, result.steps + result.illegal_attempts)),
        "win_rate": 1.0 if result.win else 0.0,
    }
    if metric not in table:
        raise InfraError(E.E_EVAL_SCHEMA, f"未知指标 {metric!r}")
    return float(table[metric])


def compare_all_metrics(a: ArmResult, b: ArmResult, *, alpha: float = 0.05,
                        B: int = 10000, rng: Any = None) -> dict[str, PairedTest]:
    """主指标全量配对检验（MOD-07 C2 逐指标取用；MOD-07 不做统计）。"""
    out: dict[str, PairedTest] = {}
    for metric in ("win_rate",) + tuple(m for m in MAIN_METRICS if m != "win_rate"):
        out[metric] = compare_paired(a, b, metric, alpha=alpha, B=B, rng=rng)
    return out


def _default_runner(*, harness_dir: Path | str, arm: str, memory_version: int | None,
                    config: Any, model_hash: str, run_id: str,
                    llm: Any = None, memory: Any = None,
                    episode_config: Any = None) -> EpisodeRunner:
    """构造默认执行器：MOD-04 ``PolicyAgent`` + MOD-02 ``SandboxEnv``（懒惰 import）。

    - 评估一律经 MOD-02 门面（INV-1），温度固定 ``0.0``（B-14，由 MOD-03 强制）；
    - 未注入 MOD-03 客户端（``llm is None``）时，按 Spec §8.5 显式**降级**为
      ``baseline_heuristic`` 策略（不静默假装是 LLM 局），该路径在报告中可由
      ``agent_kind`` 分辨。
    """
    def factory(seed: int, arm_name: str) -> Any:
        from dataclasses import replace

        from dice_ratchet.agent.policy import PolicyAgent

        policy_cfg = getattr(getattr(config, "agent", config), "policy", None)
        if policy_cfg is None:
            from dice_ratchet.infra.config import PolicyConfig

            policy_cfg = PolicyConfig()
        if llm is None:
            policy_cfg = replace(policy_cfg, mode="baseline_heuristic",
                                 temperature=0.0)
        return PolicyAgent(policy_cfg, llm=llm, memory=memory,
                           harness_dir=Path(harness_dir))

    env_cfg = getattr(config, "env", None) if config is not None else None
    return SandboxRunner(agent_factory=factory, run_id=run_id, memory=memory,
                         config=config,
                         episode_config=(episode_config if episode_config is not None
                                         else (getattr(env_cfg, "episode", None)
                                               if env_cfg is not None else None)))


def evaluate_arm(harness_dir: Path | str, *, split: Split, basket: BasketId | Sequence[int],
                 memory_version: int, arm: str, run_id: str,
                 protocol: seeds_mod.SeedProtocol | Any = None,
                 runner: EpisodeRunner | None = None,
                 generation: int = 0, harness_version: int = 0,
                 config: Any = None, model_hash: str = "",
                 n_workers: int = 1, n_rep: int = 1,
                 episode_config: Any = None,
                 resume_results: Sequence[EpisodeResult] = (),
                 on_result: Callable[[EpisodeResult], None] | None = None,
                 run_dir: Path | str | None = None,
                 archive: bool = True) -> ArmResult:
    """单臂评估（Spec §4.7；FR-08-25）。

    ``runner`` 未注入时用 MOD-04/02 的默认执行器（MOD-06 会显式注入自己的 runner）。

    F42-A/B（长跑检查点）：

    - ``resume_results`` / ``on_result`` **薄转发**给 ``run_eval``（不复制任何执行逻辑）；
    - **臂归档**：``run_id`` 在 ``runs_root``（或显式 ``run_dir``）下已存在时，每次评估落一份
      ``metrics/arms/gen{gen:03d}-{split}-{basket}-{arm}.json``（``ArmResult.as_dict()`` +
      provenance + 逐局明细），并**先**尝试复用：

      * provenance（``ARM_PROVENANCE_KEYS``）**全等**且覆盖完整 seed 列表 → **整臂复用**
        （一局都不跑），发 ``evals.arm_reused``；
      * 只覆盖一部分 → 把已完成的作为 ``resume_results`` 传入，只跑缺的局；
      * 任一 provenance 不一致 / 归档损坏 / run 目录不存在 → **不复用**，照常重算（fail-closed）。

    """

    proto = seeds_mod.validate_protocol(seeds_mod.coerce_protocol(protocol))
    seed_list = tuple(seeds_mod.resolve_basket(basket, generation=generation, protocol=proto))
    eval_cfg = getattr(config, "evals", config)
    spec = EvalSpec(
        run_id=run_id, generation=int(generation), harness_version=int(harness_version),
        split=split, basket=(basket if isinstance(basket, str) else seed_list),
        seeds=seed_list, agent_kind=arm, n_rep=int(n_rep), n_workers=int(n_workers),
        seed_timeout_s=int(getattr(eval_cfg, "seed_timeout_s", 14400)),
        budget_seconds=getattr(eval_cfg, "budget_seconds", None),
        t_max=int(getattr(eval_cfg, "t_max", 120)),
        min_support=int(getattr(eval_cfg, "min_support", 3)),
        drop_rate_max=float(getattr(eval_cfg, "drop_rate_max", 0.10)),
        arm=arm, memory_version=memory_version,
        episode_config_hash=_config_digest(episode_config))
    archive_dir = _arm_archive_dir(run_dir=run_dir, config=config, run_id=run_id) \
        if archive else None
    wanted = _arm_provenance_for(spec=spec, proto=proto, config=config, run_id=run_id)
    cached = load_arm_archive(arm_archive_path(
        archive_dir, generation=spec.generation, split=spec.split, basket=spec.basket_id,
        arm=arm)) if archive_dir is not None else None
    reused: list[EpisodeResult] = []
    reusable = bool(cached) and provenance_matches(dict(cached.get("provenance") or {}), wanted)
    if reusable and cached is not None:
        reused, missing = reuse_from_archive(cached, seeds=spec.seeds, split=split,
                                             generation=spec.generation,
                                             harness_version=spec.harness_version, arm=arm)
        if reused and not missing:
            # 按**篮子顺序**还原（与 `run_eval` 的聚合顺序一致，保证复用不改测量结果）
            by_seed = {int(r.seed): r for r in reused}
            ordered = tuple(by_seed[int(s)] for s in spec.seeds if int(s) in by_seed)
            metrics = aggregate(ordered, split=split, basket=spec.basket_id,
                                t_max=spec.t_max, min_support=spec.min_support,
                                drop_rate_max=spec.drop_rate_max)
            _emit("evals.arm_reused", generation=spec.generation, split=split,
                  basket=spec.basket_id, arm=arm, n=metrics.n,
                  archive=str(arm_archive_path(archive_dir, generation=spec.generation,
                                               split=split, basket=spec.basket_id, arm=arm)))
            return ArmResult(arm=arm, split=split, basket=spec.basket_id,
                             harness_version=int(harness_version),
                             memory_version=int(memory_version or 0), run_id=run_id,
                             results=ordered, metrics=metrics, model_hash=model_hash,
                             dur_s=0.0, resumed_n=len(ordered),
                             pending_seeds=(), archive_path=str(arm_archive_path(
                                 archive_dir, generation=spec.generation, split=split,
                                 basket=spec.basket_id, arm=arm)))
    # 归档只覆盖一部分：已完成局并入 ``resume_results``（调用方显式给的也要保留），
    # 引擎按 ``results`` **过滤队列** ⇒ 已完成的 seed 不会再跑一次。
    already = {int(r.seed): r for r in resume_results}
    already.update({int(r.seed): r for r in reused})
    missing = tuple(s for s in spec.seeds if int(s) not in already)
    # 局部完成的收益必须落到**臂归档**上：给每次新完成的局补一次归档写。
    persist = on_result
    if archive_dir is not None:
        persist = _archive_on_result(on_result, run_root=archive_dir, spec=spec, proto=proto,
                                     config=config, run_id=run_id, model_hash=model_hash,
                                     already=already)
    executor = runner or _default_runner(harness_dir=harness_dir, arm=arm,
                                         memory_version=memory_version, config=config,
                                         model_hash=model_hash, run_id=run_id,
                                         episode_config=episode_config)
    result = run_eval(spec, runner=executor, writer=None, protocol=proto,
                      resume_results=tuple(already.values()), on_result=persist)
    completed = {int(r.seed) for r in result.results}
    outcome = ArmResult(arm=arm, split=split, basket=spec.basket_id,
                        harness_version=int(harness_version),
                        memory_version=int(memory_version or 0), run_id=run_id,
                        results=result.results, metrics=result.metrics,
                        model_hash=model_hash, error_codes=result.error_codes,
                        dur_s=result.dur_s, resumed_n=len(already),
                        pending_seeds=tuple(s for s in spec.seeds
                                            if int(s) not in completed))
    if archive_dir is not None:
        provenance = arm_provenance(
            generation=spec.generation, split=split, basket=spec.basket_id, arm=arm,
            harness_version=int(harness_version), memory_version=int(memory_version or 0),
            n_seeds=len(spec.seeds), n_dropped=int(result.metrics.n_dropped),
            config_hash=wanted.get("config_hash"), seeds_hash=_seed_hash(proto),
            protocol_id=str(getattr(proto, "version", "") or ""),
            episode_config_hash=spec.episode_config_hash)
        path = write_arm_archive(archive_dir, generation=spec.generation, split=split,
                                 basket=spec.basket_id, arm=arm, result=outcome,
                                 provenance=provenance)
        outcome = replace(outcome, archive_path=str(path))
    return outcome


def _arm_archive_dir(*, run_dir: Path | str | None, config: Any, run_id: str) -> Path | None:
    """臂归档根目录；run 目录不存在 → ``None``（不归档，也不读归档）。

    ``run_dir`` 显式给出时以它为准；否则按 ``config.infra.runs_root / run_id`` 推导。
    生产路径下 run 目录总是存在（``init_run_dir`` 先建）；单测里的哑 ``run_id`` 因此
    天然不产归档文件。
    """
    if not run_id:
        return None
    if run_dir is not None:
        root = Path(run_dir)
    else:
        runs_root = getattr(getattr(config, "infra", None), "runs_root", None) or "runs"
        root = Path(runs_root) / str(run_id)
    return root if root.is_dir() else None


def _arm_provenance_for(*, spec: EvalSpec, proto: Any, config: Any, run_id: str
                        ) -> dict[str, Any]:
    """本次评估的 provenance（**不含** ``created_at``，用于与归档逐字段对拍）。"""
    from dice_ratchet.infra.config import config_hash

    return {"generation": spec.generation, "split": spec.split, "basket": spec.basket_id,
            "arm": spec.arm, "harness_version": spec.harness_version,
            "memory_version": int(spec.memory_version or 0),
            "config_hash": (config_hash(config) if config is not None else None),
            "seeds_hash": str(getattr(proto, "seeds_hash", "") or ""),
            "episode_config_hash": spec.episode_config_hash}


def _archive_on_result(on_result: Callable[[EpisodeResult], None] | None, *,
                       run_root: Path, spec: EvalSpec, proto: Any, config: Any,
                       run_id: str, model_hash: str,
                       already: Mapping[int, EpisodeResult],
                       ) -> Callable[[EpisodeResult], None]:
    """把「每完成一局」接到**臂归档**上（F42-A）。

    局部完成时，光把 ``resume_results`` 传下去只能省当事进程的重复劳动；进程再挂一次，
    那些已完成的局就丢了。因此每局完成时用「已完成 + 本局」重写一次归档（原子写）。
    """
    def _persist(result: EpisodeResult) -> None:
        if on_result is not None:
            on_result(result)
        done = dict(already)
        done[int(result.seed)] = result
        ordered = tuple(done[s] for s in sorted(done))
        metrics = aggregate(ordered, split=spec.split, basket=spec.basket_id,
                            t_max=spec.t_max, min_support=spec.min_support,
                            drop_rate_max=spec.drop_rate_max)
        partial = ArmResult(arm=spec.arm, split=spec.split, basket=spec.basket_id,
                            harness_version=spec.harness_version,
                            memory_version=int(spec.memory_version or 0), run_id=run_id,
                            results=ordered, metrics=metrics, model_hash=model_hash,
                            resumed_n=len(already))
        provenance = arm_provenance(
            generation=spec.generation, split=spec.split, basket=spec.basket_id,
            arm=spec.arm, harness_version=spec.harness_version,
            memory_version=int(spec.memory_version or 0), n_seeds=len(spec.seeds),
            n_dropped=int(metrics.n_dropped), config_hash=_safe_config_hash(config),
            seeds_hash=_seed_hash(proto),
            protocol_id=str(getattr(proto, "version", "") or ""),
            episode_config_hash=spec.episode_config_hash)
        write_arm_archive(run_root, generation=spec.generation, split=spec.split,
                          basket=spec.basket_id, arm=spec.arm, result=partial,
                          provenance=provenance)

    return _persist


def _seed_hash(proto: Any) -> str:
    """种子协议的 ``seeds_hash``（缺失/``None`` → ``""``）。

    注意 ``getattr(proto, "seeds_hash", "") or ""`` 对 ``None`` **无效**（``str(None)`` → ``"None"``），
    这正是 F42-A 复用闸第一次写完就永远对不上的原因（归档写 ``"None"``、对拍算 ``""``）。
    """
    value = getattr(proto, "seeds_hash", None)
    return "" if value is None else str(value)


def _safe_config_hash(config: Any) -> str | None:
    if config is None:
        return None
    try:
        from dice_ratchet.infra.config import config_hash

        return config_hash(config)
    except Exception: # noqa: BLE001 - 归档是可观测性产物，不得阻塞测量
        return None


def _config_digest(obj: Any) -> str | None:
    if obj is None:
        return None
    if hasattr(obj, "__dataclass_fields__"):
        from dataclasses import asdict
        payload = asdict(obj)
    elif isinstance(obj, Mapping):
        payload = dict(obj)
    else:
        payload = {"repr": repr(obj)}
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# =====================================================================
# 臂归档与复用（F42-A/B：每代评估的检查点）
# =====================================================================
#
# 现场：MOD-07 的 resume 单元只有**整代**（`loop/generation.py::_cycle_artifacts_exist` 看
# `generations/gen-NNN/{report,decision}.json`），不在就把 phase 重置为 playing **重跑整代**。
# 一代 84 局评估 ≈ 3.75 h / ≈¥47（实测 `m2-s32b-smoke-c1-medium6` 的 gen-1），中断即再付一次。
# 引擎早就有长跑能力（`run_eval` 的 `resume_results` / `on_result`，F18/F19/F20），缺的是
# 「中间那层」：`evaluate_arm` 不转发它们。本段补上「按臂落盘 + fail-closed 复用」。

#: provenance 里必须**逐字段相等**才允许复用的键（fail-closed：任一不同就重算）。
ARM_PROVENANCE_KEYS: Final[tuple[str, ...]] = (
    "generation", "split", "basket", "arm", "harness_version", "memory_version",
    "config_hash", "seeds_hash", "episode_config_hash",
)

#: 臂归档里逐局需要还原的字段（``win`` / ``degraded_flag`` 是 property，见 ``EpisodeResult``）。
#: 含 ``arm``：它是逐局事实，归档必须**原样还原**（否则复用会把 ``arm`` 换成请求值，
#: 让「复用」悄悄改变了测量结果 —— 测试替身返回的 arm 与请求值不同时就会暴露）。
_EPISODE_FIELDS: Final[tuple[str, ...]] = (
    "seed", "outcome", "truncation_reason", "stages_cleared", "hearts_left", "battles_won",
    "decisions", "decisions_llm", "steps", "illegal_attempts", "fallback_decisions",
    "kind_counts", "slot_counts", "seq_hash", "dropped", "degraded", "decision_kinds",
    "error_code", "arm",
)


def arms_dir(run_root: Path | str) -> Path:
    """``runs/<run_id>/metrics/arms/``（F42-A 的臂归档目录）。"""
    return Path(run_root) / "metrics" / "arms"


def arm_archive_path(run_root: Path | str, *, generation: int, split: str, basket: str,
                     arm: str) -> Path:
    """``metrics/arms/gen{gen:03d}-{split}-{basket}-{arm}.json``（PRD §13）。"""
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(arm)) or "arm"
    return arms_dir(run_root) / f"gen{int(generation):03d}-{split}-{basket}-{safe}.json"


def _episode_to_dict(result: EpisodeResult) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in _EPISODE_FIELDS:
        value = getattr(result, name, None)
        out[name] = list(value) if isinstance(value, tuple) else value
    return out


def _episode_from_dict(data: Mapping[str, Any], *, split: Split, generation: int,
                       harness_version: int, arm: str) -> EpisodeResult:
    return EpisodeResult(
        split=split, generation=int(generation), harness_version=int(harness_version),
        arm=str(data.get("arm") or arm), seed=int(data.get("seed") or 0),
        outcome=str(data.get("outcome") or "lose"),
        truncation_reason=(str(data["truncation_reason"])
                           if data.get("truncation_reason") else None),
        stages_cleared=int(data.get("stages_cleared") or 0),
        hearts_left=int(data.get("hearts_left") or 0),
        battles_won=int(data.get("battles_won") or 0),
        decisions=int(data.get("decisions") or 0),
        decisions_llm=int(data.get("decisions_llm") or 0),
        steps=int(data.get("steps") or 0),
        illegal_attempts=int(data.get("illegal_attempts") or 0),
        fallback_decisions=int(data.get("fallback_decisions") or 0),
        kind_counts=tuple(int(x) for x in (data.get("kind_counts") or ()))[:16],
        slot_counts=dict(data.get("slot_counts") or {}),
        seq_hash=str(data.get("seq_hash") or ""),
        dropped=bool(data.get("dropped", False)),
        degraded=bool(data.get("degraded", False)),
        decision_kinds=tuple(int(x) for x in (data.get("decision_kinds") or ())),
        error_code=(str(data["error_code"]) if data.get("error_code") else None))


def arm_provenance(*, generation: int, split: str, basket: str, arm: str,
                   harness_version: int, memory_version: int, n_seeds: int,
                   n_dropped: int = 0, config_hash: str | None = None,
                   seeds_hash: str = "", protocol_id: str = "",
                   episode_config_hash: str | None = None) -> dict[str, Any]:
    """臂归档的 provenance（F42-A；``evaluate_arm`` 与调用方共用同一构造）。"""
    return {"generation": int(generation), "split": str(split), "basket": str(basket),
            "arm": str(arm), "harness_version": int(harness_version),
            "memory_version": int(memory_version), "n_seeds": int(n_seeds),
            "n_dropped": int(n_dropped), "config_hash": config_hash,
            "seeds_hash": str(seeds_hash), "protocol_id": str(protocol_id),
            "episode_config_hash": episode_config_hash, "created_at": utc_now()}


def provenance_matches(archived: Mapping[str, Any], wanted: Mapping[str, Any]) -> bool:
    """provenance **逐字段**相等（``ARM_PROVENANCE_KEYS``）；任一不同即不复用（fail-closed）。"""
    return all(archived.get(key) == wanted.get(key) for key in ARM_PROVENANCE_KEYS)


def load_arm_archive(path: Path | str) -> dict[str, Any] | None:
    """读回臂归档（缺失/损坏 → ``None``，按「不复用」处理）。"""
    path = Path(path)
    if not path.is_file():
        return None
    try:
        data = read_json(path)
    except InfraError:
        return None
    return data if isinstance(data, dict) else None


def restore_arm_from_archive(archive: Mapping[str, Any] | None, *,
                             generation: int, harness_version: int, arm: str,
                             required_seeds: Sequence[int] = (),
                             t_max: int = 120, min_support: int = 3,
                             drop_rate_max: float = 0.10) -> ArmResult | None:
    """把臂归档**还原**成 ``ArmResult``（**不跑任何局**；F57 跨代继承用）。

    与 ``evaluate_arm`` 的**复用**分支（``evals.arm_reused``）的区别在于**代际**：
    复用分支要求 provenance 里的 ``generation`` 与本次相同（同代重跑），而跨代继承要的
    正是**另一代**的同一臂测量——它的 ``harness_version`` 与当前 parent 逐字节相同，
    只是当年写在 ``gen-00X`` 的归档里。

    因此这里**只**校验「测量身份」而与代际无关的三件事，任一不满足返回 ``None``
    （fail-closed，调用方据此**重测**）：

    - ``archive["harness_version"] == harness_version``（同一个 harness 才有资格当起点）；
    - ``archive["basket"] == archive["provenance"]["basket"]``（自洽；外部 basket 由调用方在
      **选文件时**比对——``required_seeds`` 也覆盖了它）；
    - ``required_seeds ⊆ 归档的 seed 集合``（**不少于**要用的局；篮子是子集时可裁剪）。

    ``arm`` 覆写为请求值：候选臂名是 ``cand-<id>``，作为下一代的 parent 时它扮演的是
    ``parent``——这正是「接受后自动成为起点」的语义。
    """
    if not archive:
        return None
    if _int_or_none_field(archive.get("harness_version")) != int(harness_version):
        return None
    provenance = archive.get("provenance") or {}
    if not isinstance(provenance, Mapping):
        return None
    if str(archive.get("basket") or "") != str(provenance.get("basket") or ""):
        return None
    seeds = [int(s) for s in (required_seeds or ())]
    results, missing = reuse_from_archive(archive, seeds=seeds,
                                          split=str(archive.get("split") or "heldout"),
                                          generation=int(generation),
                                          harness_version=int(harness_version), arm=arm)
    if missing:
        return None
    by_seed = {int(r.seed): r for r in results}
    ordered = tuple(by_seed[int(s)] for s in seeds if int(s) in by_seed)
    metrics = aggregate(ordered, split=str(archive.get("split") or "heldout"),
                        basket=str(archive.get("basket") or ""),
                        t_max=int(t_max), min_support=int(min_support),
                        drop_rate_max=float(drop_rate_max))
    return ArmResult(arm=str(arm), split=str(archive.get("split") or "heldout"),
                     basket=str(archive.get("basket") or ""),
                     harness_version=int(harness_version),
                     memory_version=int(archive.get("memory_version") or 0),
                     run_id=str(archive.get("run_id") or ""),
                     results=ordered, metrics=metrics, dur_s=0.0,
                     resumed_n=len(ordered), pending_seeds=(),
                     archive_path=str(archive.get("archive_path") or ""))


def _int_or_none_field(value: Any) -> int | None:
    """归档字段 → ``int``；缺失/非数字 → ``None``（``None != harness_version`` ⇒ 不复用）。"""
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def write_arm_archive(run_root: Path | str, *, generation: int, split: str, basket: str,
                      arm: str, result: ArmResult,
                      provenance: Mapping[str, Any]) -> Path:
    """落一份臂归档：``ArmResult.as_dict()`` + provenance + 逐局明细（F42-A）。"""
    path = arm_archive_path(run_root, generation=generation, split=split, basket=basket,
                            arm=arm)
    payload = dict(result.as_dict())
    payload["provenance"] = dict(provenance)
    payload["episodes"] = [_episode_to_dict(r) for r in result.results]
    write_json_atomic(path, _round_floats(payload))
    return path


def reuse_from_archive(archive: Mapping[str, Any], *, seeds: Sequence[int], split: Split,
                       generation: int, harness_version: int, arm: str
                       ) -> tuple[list[EpisodeResult], tuple[int, ...]]:
    """从归档里挑出**仍然有效**的已完成局，返回 ``(results, missing_seeds)``。

    只接受 seed 在当前篮子内的逐局记录（形状异常/重复/超范围 → 丢弃）。
    """
    episodes = archive.get("episodes") or []
    wanted = {int(s) for s in seeds}
    out: dict[int, EpisodeResult] = {}
    for item in episodes:
        if not isinstance(item, Mapping):
            continue
        seed = int(item.get("seed") or 0)
        if seed not in wanted or seed in out:
            continue
        out[seed] = _episode_from_dict(item, split=split, generation=generation,
                                       harness_version=harness_version, arm=arm)
    missing = tuple(sorted(wanted - set(out)))
    return [out[s] for s in sorted(out)], missing


@dataclass(frozen=True)
class GainVerdictDiversity:
    """``evaluate_generation`` 内部用的中间量（坍缩 + 增益）。"""

    gain: GainVerdict
    diversity: Any


def _diversity_for_generation(run_id: str, generation: int, *, parent: ArmResult | None,
                              winner: ArmResult | None = None,
                              config: Any = None,
                              run_dir: Path | str | None = None) -> Any:
    """按历史曲线做坍缩判定（PRD §8.3）。

    历史来源：``runs/<run_id>/metrics/plots_data/gen-*_entropy.json``（若存在）；
    否则退化为「parent 曲线 + 当前曲线」两点。
    """
    cfg = CollapseConfig.from_eval_config(getattr(config, "evals", config) or CollapseConfig())
    curves: list[list[float | None]] = []
    win_rates: list[float] = []
    jsds: list[float | None] = []
    root = Path(run_dir) if run_dir is not None else Path("runs") / run_id
    plots = root / "metrics" / "plots_data"
    if plots.is_dir():
        for path in sorted(plots.glob("gen-*_entropy.json")):
            try:
                data = read_json(path)
            except InfraError:
                continue
            buckets = data.get("buckets") or []
            curves.append([b.get("h_norm") for b in buckets])
            win_rates.append(float(data.get("win_rate") or 0.0))
    if winner is not None:
        current = list(winner.metrics.action_entropy_curve)
        if not curves or len(current) != len(curves[-1]):
            curves.append(current)
            win_rates.append(winner.metrics.win_rate)
        else:
            curves[-1] = current
            win_rates[-1] = winner.metrics.win_rate
    elif parent is not None:
        curves.append(list(parent.metrics.action_entropy_curve))
        win_rates.append(parent.metrics.win_rate)
    base = getattr(getattr(config, "evals", config), "entropy", None)
    base_value = float(getattr(base, "base", 2.0) or 2.0)
    # 落盘的 kind 分布（供下一代算 `jsd_kind`，也供 `eval report` 的重算路径读回）
    current_dist: dict[str, float] = {}
    if winner is not None:
        current_dist = kind_distribution(winner.results)
    elif parent is not None:
        current_dist = kind_distribution(parent.results)
    if current_dist:
        write_kind_distribution(root, int(generation), current_dist)

    # **`jsd_kind` 与 `win_rates` 必须对齐到同一个 pair**（2026-10-11 修 pair 错位）。
    # 修复前是 `jsds = [None] + [jsd(parent, winner)]` 配
    # `win_rates = [..., parent.win_rate, winner.win_rate]` ⇒ `c3` 在 `i=1` 上拿
    # 「父→子的分布漂移」配「子相对父的胜率变化」，**不是同一对**。
    # 现在两条路径都用 `aligned_jsd_kinds`：`jsds[i]` 恒为 `jsd(g_{i-1}, g_i)`，
    # 且 `win_rates[i]` 恒为第 i 代的胜率。
    gens = _generations_of_curves(root, curves)
    jsds = aligned_jsd_kinds(root, gens, base=base_value) if gens else [None] * len(curves)
    if len(jsds) != len(curves):   # 兜底：曲线来源与分布来源不一致时不要错位
        jsds = [None] * len(curves)
    seq_ratios: list[float] = []
    if winner is not None:
        seq_ratios.append(distinct_sequence_ratio(winner.results))
    return collapse_check(curves=curves, jsd_kind=jsds, win_rates=win_rates, cfg=cfg,
                          seq_ratios=seq_ratios or None, generation=generation)


def _generations_of_curves(root: Path, curves: Sequence[Sequence[Any]]) -> list[int]:
    """``curves`` 是按代升序的熵曲线；这里回推对应的代数。

    来源按优先级：`metrics/kind_distribution/` 里已落盘的代号（权威、单调），
    兜底为「从 0 开始的连续代」（`_diversity_for_generation` 在缺 plots_data 时正是
    这样补齐的）。**长度必须与 `curves` 一致**——不一致时调用方会把 `jsd_kind`
    整体置 `None`（宁可不算，也不要错位）。
    """
    recorded = sorted(int(p.stem.split("-")[-1])
                      for p in (Path(root) / "metrics" / "kind_distribution").glob("gen-*.json")
                      if p.stem.split("-")[-1].isdigit())
    if len(recorded) == len(curves):
        return recorded
    return list(range(len(curves)))


def evaluate_generation(generation: int, parent: ArmResult, winner: ArmResult | None, *,
                        run_id: str, run_dir: Path | str | None = None,
                        config: Any = None,
                        protocol: seeds_mod.SeedProtocol | Any = None,
                        heldout_source: str = "measured",
                        retained_arm: ArmResult | None = None,
                        retained_version: int | None = None,
                        play_hash: str = "") -> dict[str, Any]:
    """产出 report 的指标段（Spec §4.7；decision 段由 MOD-07 合并）。

    **``heldout`` 段 = 本代结束时在役那份 harness 的测量**（D-M3-7，2026-10-14 用户裁决；
    取代 F57 的「恒取 parent」）：

    - 补丁被拒 ⇒ 在役 harness 就是起点那份 ⇒ ``heldout`` 取 ``parent``（其测量可能由
      ``heldout_source="inherited:gen-00X"`` 标明是**复用**来的，本代 0 局）；
    - 补丁被接受 ⇒ 在役 harness 是**新版本** ⇒ ``heldout`` 取 ``retained_arm``（即那个
      候选自己的臂）。这正是用户的步骤 3/5：「后续比较的 heldout 是步骤 3 的 heldout」。

    F57 的**原口径为什么必须改**：判据 1 比的是「相邻两代在役 harness 的 heldout」。
    若 ``heldout`` 恒取 parent，则「接受了一代」这件事在台账里**永远不可见**（接受后的
    新测量没有落点），判据 1 就成了「拿起点与起点比」的恒真命题。F57 当初选 parent 是为了
    让两个写入方（``loop run`` 与 ``eval report``）写同一个数——那个**症状**现在由 F60
    的「报告是唯一权威」解决，而不是靠牺牲语义。

    - ``retained_version``：本代结束时在役的版本号（拒绝时 = parent+1，D2=A）。缺省回落
      到 ``winner or parent`` 的版本。
    - ``train`` 段取 ``take("train", generation)`` 那个 seed 块（F64）：训练局属于**它服务
      的那一代**。原先取 ``train_window(generation)`` 且按 ``generation`` 过滤，而循环把
      第 g 代的训练局标成了 ``generation=g-1`` ⇒ 交集只剩 4 局冒烟局（实测 gen-001：
      ``train.n=4, low_confidence=False``，``win_rate_gap`` 建在垃圾上）。取不到足量训练局
      时**不静默**：记 ``train.source="insufficient"`` + ``low_confidence=True``。
    """
    proto = seeds_mod.validate_protocol(seeds_mod.coerce_protocol(protocol))
    eval_cfg = getattr(config, "evals", config)
    served = retained_arm if retained_arm is not None else parent
    heldout = served.metrics
    # F64：训练段取 ``take("train", generation)`` —— 训练局属于**它服务的那一代**。
    # gen-000 是基线：**没有**训练块服务它（``take("train", 0)`` 按契约抛
    # ``E_EVAL_SEED_EXHAUSTED``：「gen-000 不消耗训练 seed」），故显式取空，标 unavailable。
    gen_index = int(generation)
    train_seeds = (seeds_mod.take("train", generation=gen_index, protocol=proto)
                   if gen_index >= 1 else ())
    root = Path(run_dir) if run_dir is not None else Path("runs") / run_id
    # ``arm="train"`` 是**真过滤**：同一代的冒烟局（``parent-smoke`` / ``cand-*-smoke``）
    # 用的正是本代训练块的**末尾 seed**（FR-07-6），不过滤会把它们混进训练段
    # （实测：30 局训练块 + 1 局 parent-smoke = n=31）。
    train_results = load_episode_results(root, "train", generation=gen_index,
                                         harness_version=parent.harness_version,
                                         seeds=train_seeds, arm="train")
    train_metrics = aggregate(train_results, split="train", basket="train_window",
                              t_max=int(getattr(eval_cfg, "t_max", 120)),
                              min_support=int(getattr(eval_cfg, "min_support", 3)),
                              drop_rate_max=float(getattr(eval_cfg, "drop_rate_max", 0.10)))
    # F64 fail-closed：训练段必须真的覆盖本代的训练块。不足时**显式标注**（而不是像
    # 现场那样让 n=4 的冒烟局冒充训练段、还把 low_confidence 记成 False）。
    min_support = int(getattr(eval_cfg, "min_support", 3))
    if gen_index < 1:
        train_source = "unavailable"
        train_metrics = replace(train_metrics, low_confidence=True)
    elif len(train_results) >= min_support:
        train_source = "measured"
    else:
        train_source = "insufficient"
        train_metrics = replace(train_metrics, low_confidence=True)
    if train_source != "measured":
        _emit_train_insufficient(generation=gen_index, run_id=str(run_id),
                                 n=len(train_results), expected=len(train_seeds))
    # F57：坍缩/多样性也走**同一个臂**（parent）——否则报告里 heldout 块是 parent 的
    # 熵曲线、diversity 块是候选的，读者会以为它们是同一次测量。
    diversity = _diversity_for_generation(run_id, generation, parent=parent,
                                          winner=None,
                                          config=config, run_dir=root)
    spec = EvalSpec(run_id=run_id, generation=int(generation),
                    harness_version=heldout_harness_version(parent, winner),
                    split=heldout.split, basket=heldout.basket, seeds=parent.seeds)
    gain = judge_gain(
        train=train_metrics, heldout=heldout,
        gap_threshold=float(getattr(eval_cfg, "gap_threshold", 0.15)),
        alpha=float(getattr(eval_cfg, "alpha", 0.05)),
        collapse_flag=diversity.collapse_flag,
        candidate=parent.results, parent=parent.results,
        low_confidence=heldout.low_confidence,
        rng=rng_for(spec), B=int(getattr(eval_cfg, "bootstrap_B", 10000)))
    report = render_report_sections(
        generation=generation,
        # ``harness_version`` = 本代**结束**时在役的版本（accept 时即新版本；拒绝时按
        # D2=A 也推进一格）：台账的连续性靠它。
        harness_version=(int(retained_version) if retained_version is not None
                         else heldout_harness_version(parent, winner)),
        # ``parent_version`` = 本代的**起点**版本（D-M3-7 起语义收紧为「起点」，不再兼任
        # 「heldout 测在哪份 harness 上」——那个信息由 ``heldout.source`` 与
        # ``harness_version`` 共同表达）。
        parent_version=parent.harness_version,
        heldout=heldout, train=train_metrics, gain=gain, diversity=diversity,
        ablation=None, decision="pending", decided_by="auto", reason="",
        episode_config_hash=_config_digest(
            getattr(getattr(config, "env", None), "episode", None) if config else None),
        heldout_source=str(heldout_source or "measured"),
        play_hash=str(play_hash or ""), train_source=train_source,
        extra={"power_note": power_note(heldout.n)})
    return report


def _emit_train_insufficient(*, generation: int, run_id: str, n: int, expected: int) -> None:
    """F64：训练段取不到足量训练局（**必须可在事件流里看到**，不得静默）。"""
    _emit("evals.train_insufficient", generation=int(generation), run_id=str(run_id),
          n=int(n), expected=int(expected))


def heldout_harness_version(parent: ArmResult, winner: ArmResult | None) -> int:
    return int(winner.harness_version if winner is not None else parent.harness_version)


# =====================================================================
# 报告落盘（FR-08-17 / FR-08-22 / FR-08-23/24）
# =====================================================================

def _generations_row(*, generation: int, harness_version: Any, parent_version: Any,
                     split: str, metrics: Mapping[str, Any], diversity: Mapping[str, Any],
                     gain: Mapping[str, Any], decision: Any, config_hash_value: Any,
                     git_commit: Any, ts: str, arm: str) -> dict[str, Any]:
    """一行台账（``split`` 维度在**调用方**决定，本函数只做字段映射）。

    ``arm``（F57，2026-10-12）：这一行是**哪个臂**的测量。缺了它，
    ``(generation, split)`` 在「同代有 parent 与候选两套测量」时是**不够的键**——
    后写的会无声覆盖先写的（实测 `m3-rehearsal` gen-001：parent `0.10` 被判「单调性
    不成立」，20 分钟后被候选的 `0.35` 覆盖，同一批产物给出相反结论）。
    """
    ci = list(metrics.get("win_rate_ci95") or (0.0, 0.0))
    return {
        "generation": int(generation or 0),
        "harness_version": harness_version,
        "parent_version": parent_version,
        "split": str(split),
        "basket": metrics.get("basket"),
        "arm": str(arm or PARENT_ARM),
        "n": metrics.get("n"),
        "win_rate": metrics.get("win_rate"),
        "ci_lo": ci[0],
        "ci_hi": ci[1],
        "mean_stages_cleared": metrics.get("mean_stages_cleared"),
        "mean_hearts_left": metrics.get("mean_hearts_left"),
        "mean_battles_won": metrics.get("mean_battles_won"),
        "mean_decisions": metrics.get("mean_decisions"),
        "illegal_rate": metrics.get("illegal_rate"),
        "fallback_rate": metrics.get("fallback_rate"),
        "truncation_rate": metrics.get("truncation_rate"),
        "entropy_mean_norm": metrics.get("entropy_mean_norm"),
        # entropy_slope / collapse_flag 是**代级**属性，只挂 heldout 行（避免 train 行与
        # heldout 行给出两个互相矛盾的坍缩判定）。
        "entropy_slope": diversity.get("entropy_slope") if split == "heldout" else None,
        "jsd_kind": diversity.get("jsd_kind") if split == "heldout" else None,
        "collapse_flag": diversity.get("collapse_flag") if split == "heldout" else None,
        # win_rate_gap 是 heldout−train 的**一个**数，只挂 heldout 行。
        "win_rate_gap": gain.get("win_rate_gap") if split == "heldout" else None,
        "verdict": gain.get("verdict") if split == "heldout" else None,
        "decision": decision if split == "heldout" else None,
        "bootstrap_seed": None,
        "config_hash": config_hash_value,
        "git_commit": git_commit,
        "ts": ts,
    }


def generations_rows_from_report(report: Mapping[str, Any], *, cfg: Any = None,
                                 now: Any = None, arm: str = PARENT_ARM
                                 ) -> list[dict[str, Any]]:
    """从**报告字典**构造台账行：``heldout`` 一行 + ``train`` 一行（F-M3-3，2026-10-11）。

    返回**列表**（按 ``heldout`` → ``train`` 排序）。**为什么必须有两行**：
    ``train_heldout_gap.png`` 与 ``win_rate.png`` 的 train 对照曲线都要 train 一侧的数，
    而 ``generations.csv`` 原先**只有 heldout 行**（实测 8 个归档 run 的 train 行数全为 0）
    ⇒ `gain_realism.win_rate_gap`（M3 判据 3 的量）在台账里**没有可核验的来源**，
    绘图只能画出空白。

    F43 的语义不变：本函数由**写报告的一方**（MOD-07 ``_finish_report``）调用，
    列集与 ``evals/cli.py`` 共用 ``GENERATIONS_COLUMNS``。

    ``arm``（F57，2026-10-12）：本行属于哪个臂。缺省 ``"parent"``——报告里的 ``heldout``
    段自 F57 起**恒为起点臂**（``evaluate_generation`` 不再回落到 winner），所以由报告
    派生出的台账行天然是起点臂。``eval report --arm cand-…`` 显式申报别的臂时，
    ``ledger_and_plots`` 会把 ``report["heldout"]["arm"]`` 传进来。
    """
    from datetime import datetime, timezone

    from dice_ratchet.infra.config import config_hash
    from dice_ratchet.infra.run import git_short_sha

    heldout = dict(report.get("heldout") or {})
    train = dict(report.get("train") or {})
    diversity = dict(report.get("diversity") or {})
    gain = dict(report.get("gain_realism") or {})
    moment = now or datetime.now(tz=timezone.utc)
    ts = moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    common = {
        "generation": int(report.get("generation") or 0),
        "harness_version": report.get("harness_version"),
        "parent_version": report.get("parent_version"),
        "diversity": diversity, "gain": gain,
        "decision": report.get("decision"),
        "config_hash_value": config_hash(cfg) if cfg is not None else None,
        "git_commit": git_short_sha(), "ts": ts, "arm": str(arm or PARENT_ARM),
    }
    rows: list[dict[str, Any]] = []
    # F57：``heldout`` **继承自上一代**时代不新增 heldout 行——本代没有真实测量，
    # 写行会在图上多出一个与上一代**重合**的点（看起来像测了两次）。
    # 与 F48 的先例同一条纪律：``train.n == 0`` 也不写行（没有测量就不写行）。
    # 出处记在 ``report.json`` 的 ``heldout.source``（``"inherited:gen-00X"``）。
    if not _is_inherited_source(heldout.get("source")):
        rows.append(_generations_row(split="heldout", metrics=heldout, **common))
    # 只在**真的有训练局**时写 train 行。判据是 ``n > 0`` 而**不是**「train 段非空」：
    # 引擎在无训练局时写的是 ``{"n": 0, "win_rate": 0.0, "low_confidence": true}``
    # ——非空 dict（实测 gen-000 基线：``train.n == 0``），写成行会被绘图与统计
    # 当成一个真实的「win_rate=0」数据点，在图上是**零点的假信号**。
    # train 行**不受继承影响**：本代确实跑了本代的训练局（继承的只有 parent 的 heldout）。
    if int(train.get("n") or 0) > 0:
        rows.append(_generations_row(split="train", metrics=train, **common))
    return rows


def _is_inherited_source(value: Any) -> bool:
    """``heldout.source`` 是否表明「本段是继承来的、本代没有测」（F57）。"""
    return str(value or "").strip().lower().startswith("inherited:")


def generations_row_from_report(report: Mapping[str, Any], *, cfg: Any = None,
                                now: Any = None) -> dict[str, Any]:
    """**heldout** 那一行（旧签名，保留给单测/调用方逐个字段核对的场景）。

    新代码请用 ``generations_rows_from_report``：台账现在按 ``(generation, split)`` 双键
    upsert，只写 heldout 行会让 train 一侧永远缺席（F-M3-3）。
    """
    return generations_rows_from_report(report, cfg=cfg, now=now)[0]


def generation_report_path(run_dir: Path | str, generation: int) -> Path:
    return Path(run_dir) / "generations" / f"gen-{int(generation):03d}" / "report.json"


def report_sha_path(run_dir: Path | str, generation: int) -> Path:
    return generation_report_path(run_dir, generation).with_suffix(".sha256")


def write_generation_report(run_dir: Path | str, generation: int, report: Mapping[str, Any],
                            *, validate: bool = True) -> tuple[Path, str]:
    """校验 schema → 原子写 ``report.json`` → 写 ``report.sha256``（FR-08-17/23）。

    校验失败**不写正式路径**，保留 ``report.invalid.json`` 供排查（PRD §10，不静默丢弃）。
    """
    path = generation_report_path(run_dir, generation)
    if validate:
        from dice_ratchet.infra.io import validate as validate_schema

        try:
            validate_schema("generation_report", dict(report))
        except InfraError as exc:
            invalid = path.with_name("report.invalid.json")
            write_json_atomic(invalid, dict(report))
            raise InfraError(E.E_EVAL_SCHEMA,
                             f"report 校验失败（已保留 {invalid}）：{exc.message}") from exc
    sha = write_json_atomic(path, dict(report))
    from dice_ratchet.infra.io import write_bytes_atomic

    write_bytes_atomic(report_sha_path(run_dir, generation), sha.encode("utf-8"))
    return path, sha


def check_report_determinism(run_dir: Path | str, generation: int,
                             report: Mapping[str, Any]) -> str:
    """同参数重跑一致性（FR-08-23/24 / G3）：不一致 → ``E_EVAL_NONDETERMINISTIC``。"""
    payload = json.dumps(_round_floats(dict(report)), ensure_ascii=False, indent=2,
                         sort_keys=True, default=str)
    digest = "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
    sha_path = report_sha_path(run_dir, generation)
    if not sha_path.is_file():
        return digest
    previous = sha_path.read_text(encoding="utf-8").strip()
    if previous != digest:
        raise InfraError(
            E.E_EVAL_NONDETERMINISTIC,
            f"gen-{int(generation):03d} 重跑 sha256 不一致：{previous} != {digest}",
            hint="同 (run_id, generation, split, seeds) 必须逐字节一致（G3）；"
                 "检查是否引入了未显式 seed 的随机源（INV-6）")
    return digest


#: G3 的执行口径（2026-10-11 D-M3-3 裁决 A，Spec v2.9 / B-65）。
#:
#: 现场：``check_report_determinism`` 从 M1 起就**没有任何生产调用点**（只有单测调用），
#:   所以「同参数重跑 `report.sha256` 必须一致，否则抛 `E_EVAL_NONDETERMINISTIC`」
#:   这条承诺**在真实 run 里从未被执行过**——它只是一句文档。
#:
#: 更关键的是：对 **API 臂**（当前主力臂 `C1` = `deepseek-flash`，注册表标注
#:   ``api_supports_seed=false``，实测「字节相同的请求 9 次返回 9 个不同响应」）
#:   *端到端*逐字节可复现**在物理上不成立**。若把它当硬门接进生产，第一个 API run 就会
#:   假报 `E_EVAL_NONDETERMINISTIC`。
#:
#: 裁决 A：把可复现性承诺**精确化到能兑现的层级**——
#:   · **可复现层**（代码 / 配置 / seed / 聚合口径 / 并发归一化）⇒ **仍然硬执行**；
#:   · **LLM 层** ⇒ 不承诺逐字节；采样照记、响应按 request hash 归档、变化即标
#:     `llm.nondeterministic`（MOD-03 既有口径）；
#:   · 臂内含任何 ``provider=api`` 的角色 ⇒ G3 记 ``api_nondeterministic`` 并发
#:     ``eval.nondeterministic`` 事件，**不抛异常**。
#:
#: 诚实边界：这**降低了「可复现」的强度**，必须在报告里显式声明；不得据此宣称
#:   「API 臂的 M3 结果可逐字节复现」。
_DETERMINISM_ENFORCED: Final[str] = "enforced"
_DETERMINISM_API_NONDETERMINISTIC: Final[str] = "api_nondeterministic"


def reproducibility_of(app_cfg: Any, *,
                       bindings: Mapping[str, Any] | None = None) -> tuple[bool, str]:
    """该配置的评估结果能否承诺**端到端逐字节可复现**。

    返回 ``(reproducible, reason)``。``reason`` 是给审计看的短标签
    （``no_api_roles`` / ``api_role:<role>=<model_id>`` / ``registry_unavailable``）。

    **fail-closed 方向**：配置不可解析时判 ``False``（不承诺可复现）——因为「误判为可复现」
    会让 API run 假报 `E_EVAL_NONDETERMINISTIC`，而「误判为不可复现」只是少执行一次检查。
    """
    if bindings is None:
        try:
            from dice_ratchet.inference.roles import resolve_role_bindings

            bindings = resolve_role_bindings(app_cfg)
        except Exception as exc:  # noqa: BLE001 - 任何解析失败都不得让报告落盘失败
            return False, f"registry_unavailable:{type(exc).__name__}"
    for role in sorted(bindings):
        spec = bindings[role]
        provider = getattr(spec, "provider", None)
        if provider is None and isinstance(spec, Mapping):  # pragma: no cover - 兼容映射形态
            provider = spec.get("provider")
        if str(provider) == "api":
            model_id = getattr(spec, "model_id", None)
            if model_id is None and isinstance(spec, Mapping):  # pragma: no cover
                model_id = spec.get("model_id")
            return False, f"api_role:{role}={model_id}"
    return True, "no_api_roles"


def enforce_report_determinism(run_dir: Path | str, generation: int,
                               report: Mapping[str, Any], *, app_cfg: Any,
                               bindings: Mapping[str, Any] | None = None) -> str:
    """**生产路径**的 G3 执行（D-M3-3 裁决 A）。

    与 ``check_report_determinism`` 的区别只有一条：先问「这一跑的臂能不能承诺端到端
    逐字节可复现」。能 ⇒ 硬执行（行为与旧函数完全一致，含抛 ``E_EVAL_NONDETERMINISTIC``）；
    不能 ⇒ 跳过硬门、发 ``eval.nondeterministic`` 事件，并把口径写回 ``report.sha256``
    旁边（调用方负责把 ``determinism`` 记进 ext / 事件）。

    返回 sha256（与 ``write_generation_report`` 的返回值同形，便于串联）。
    """
    reproducible, reason = reproducibility_of(app_cfg, bindings=bindings)
    if reproducible:
        return check_report_determinism(run_dir, generation, report)
    try:  # 事件失败绝不阻断报告落盘（与 `_emit` 的既有纪律一致）
        from dice_ratchet.infra.events import emit

        emit("eval.nondeterministic", generation=int(generation),
             determinism=_DETERMINISM_API_NONDETERMINISTIC, reason=str(reason),
             repro_scope="code/config/seed only (LLM layer archived by request hash)")
    except Exception:  # noqa: BLE001 - pragma: no cover
        pass
    # 返回**本次**报告的 sha（不比对既有值：API 臂上比对必然不等，
    # 而「不等」在这里是**预期行为**，不是缺陷）。
    payload = json.dumps(_round_floats(dict(report)), ensure_ascii=False, indent=2,
                         sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _round_floats(obj: Any) -> Any:
    if isinstance(obj, float):
        return round(obj, 4)
    if isinstance(obj, dict):
        return {k: _round_floats(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round_floats(v) for v in obj]
    return obj


def _csv_split_key(row: Mapping[str, Any]) -> str:
    """台账行的 split 键；**空值一律归一为 ``heldout``**。

    历史台账（F-M3-3 之前）没有 ``train`` 行，且早期归档里有 ``split`` 列缺失/为空的行
    （实测 `runs/g6-eval-smoke` 同代同值 5 行 = 单键 upsert 在空 split 上反复追加）。
    归一化后这些旧行会被**正确替换**，而不是继续堆积。
    """
    return str(row.get("split") or "").strip() or "heldout"


def _csv_row_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    """台账行的三键：``(generation, split, arm)``（F57）。

    三键缺一都会造成「不同测量互相覆盖」：少 ``split`` ⇒ train/heldout 互吃（F-M3-3），
    少 ``arm`` ⇒ parent/候选互吃（F57）。
    """
    return (str(row.get("generation") if row.get("generation") is not None else ""),
            _csv_split_key(row), arm_of_row(row))


def append_generations_csv(run_dir: Path | str, row: Mapping[str, Any]) -> Path:
    """写一行台账到 ``metrics/generations.csv``（FR-08-22；列固定见 §13）。

    **按 ``(generation, split, arm)`` 三键 upsert**（F57，2026-10-12；此前是
    ``(generation, split)`` 双键）。为什么非加 ``arm`` 不可：同一代**同时存在**起点臂与
    候选臂两套测量，而双键**不足以区分它们** ⇒ 后写的**无声覆盖**先写的。实测现场
    （`runs/_sweep/m3-rehearsal` gen-001 heldout）：`loop run` 15:34 写 parent 的 `0.10`
    （据此判「单调性不成立」），`eval report` 16:05 把它改成候选的 `0.35`
    （据此判 `monotonic_ok=true`）——**同一批局，两个相反结论**，而 27 列的台账里
    没有任何一列能看出发生过这件事（B-77）。
    三键之后：parent 行与候选行**并列共存**，判据 1 显式读 ``arm="parent"``（代际比的是
    起点），候选数仍可查（``--arm cand-…``）。

    历史沿革：``generation`` 单键（F43 前）→ ``(generation, split)`` 双键（F-M3-3，2026-10-11）
    → ``(generation, split, arm)`` 三键（本版）。**每次加键都是因为「有新的维度在悄悄
    互相覆盖」**，这也是为什么列清单必须与 upsert 键一起评审。

    **已裁决的 ``decision``/``verdict`` 不被覆盖**：MOD-07 写下的判决是判据①的唯一记录，
    而 ``eval report`` 的重算报告 ``decision`` 恒为 ``baseline``/``pending``。若重算在后，
    只更新指标列、保留判决列（与 ``evals/cli.py::_write_report_preserving_verdict`` 同口径）。
    该保留只作用于 heldout 行——train 行的 ``decision``/``verdict`` 本就是空。
    **臂不同 ⇒ 不是同一行**，判决列也各自独立（候选行的判决来自它自己的裁决）。
    """
    path = Path(run_dir) / "metrics" / "generations.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    incoming = {k: _round_floats(row.get(k)) for k in GENERATIONS_COLUMNS}
    incoming["arm"] = arm_of_row(incoming)
    key = _csv_row_key(incoming)

    existing: list[dict[str, Any]] = []
    if path.is_file():
        try:
            with path.open(encoding="utf-8", newline="") as handle:
                existing = [dict(r) for r in csv.DictReader(handle)]
        except (OSError, csv.Error): # pragma: no cover - 坏账本按空重建
            existing = []

    # **读入时按三键折叠历史重复行**（F-M3-3 的第二处缺陷）。
    # 实测：`runs/g6-eval-smoke/metrics/generations.csv` 同一代同值有 **5 行**——
    # 产生原因是 F45-2 之前的行**没有 `split` 列**，而当时的 upsert 比较 `prev.get("split")`
    # 得到的 `None` 与任何值都不等 ⇒ 每次都走 `existing.append`。后果不只是文件脏：
    # 任何按行读取的下游（绘图、汇总、判据判定）都会把同一代数 5 遍。
    # 这里**顺手修好**：后写覆盖先写（保留末条），即「重复的最后一次写入」才代表真实值。
    collapsed: dict[tuple[str, str, str], dict[str, Any]] = {}
    for prev in existing:
        collapsed[_csv_row_key(prev)] = prev
    existing = list(collapsed.values())

    replaced = False
    for index, prev in enumerate(existing):
        if _csv_row_key(prev) != key:
            continue
        if _csv_split_key(incoming) == "heldout" and \
                str(prev.get("decision") or "") in VERDICT_DECISIONS:
            incoming["decision"] = prev.get("decision")
            if prev.get("verdict") not in (None, ""):
                incoming["verdict"] = prev.get("verdict")
        existing[index] = incoming
        replaced = True
        break
    if not replaced:
        existing.append(incoming)
    existing.sort(key=lambda r: (int(r.get("generation") or 0), _csv_split_key(r) != "heldout",
                                 arm_of_row(r) != PARENT_ARM, arm_of_row(r)))

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(GENERATIONS_COLUMNS), extrasaction="ignore")
    writer.writeheader()
    for r in existing:
        writer.writerow({k: r.get(k) for k in GENERATIONS_COLUMNS})

    from dice_ratchet.infra.io import write_bytes_atomic

    write_bytes_atomic(path, buf.getvalue().encode("utf-8"))
    return path


def ledger_and_plots(run_dir: Path | str, generation: int, report: Mapping[str, Any], *,
                     cfg: Any = None, arm: str = PARENT_ARM,
                     write_ledger: bool = True) -> dict[str, Any]:
    """把一代的**台账行**与**5 张 PNG** 一次写全（F56，2026-10-12）。

    **为什么要有这个助手**：这两个产物原先各写一半 —— 台账只由 MOD-07 ``_finish_report``
    与 ``eval report`` 写，绘图只由 ``eval report`` 写。后果在 2026-10-12 的 M3 彩排里
    同时暴露（`loop run` 两条路径都没接）：
      · ``runs/_sweep/m3-rehearsal/plots/`` **全空**（`loop run` 从不调绘图）；
      · ``metrics/generations.csv`` **缺 gen-000 基线行**（`write_gen000_baseline` 不写台账）
        ⇒ 判据 1 判定器拿不到「相邻两代」⇒ ``comparable=0`` ⇒ **彩排的判据 4 必然不合格**。
    「写得全不全」不该由**调用方**记得，所以收敛到一处，两个写入方（MOD-06 基线与
    MOD-07 代末）都走它。

    纪律（沿用 F-M3-3 裁决 A 第 7 条）：附属产物**失败不阻塞**报告/裁决，但必须**非静默**
    （``evolve.ledger`` / ``infra.plot_skipped`` 事件；绘图另写 stderr，便于人看日志）。
    返回 ``{"ledger_rows": n, "ledger_error": str|None, "plots": [文件名], "plot_error": str|None}``。
    """
    import sys

    out: dict[str, Any] = {"ledger_rows": 0, "ledger_error": None,
                           "plots": [], "plot_error": None}
    try:
        rows = generations_rows_from_report(
            report, cfg=cfg,
            arm=str(arm or (report.get("heldout") or {}).get("arm") or PARENT_ARM))
        if write_ledger:
            for row in rows:
                append_generations_csv(run_dir, row)
        out["ledger_rows"] = len(rows) if write_ledger else 0
        out["ledger_skipped"] = not write_ledger
        _emit("evolve.ledger", generation=int(generation),
              status="ok" if write_ledger else "skipped", rows=out["ledger_rows"],
              splits=[str(r.get("split")) for r in rows],
              arms=sorted({arm_of_row(r) for r in rows}))
    except Exception as exc: # noqa: BLE001 - 台账是附属产物，失败不阻塞裁决
        out["ledger_error"] = str(exc)[:200]
        _emit("evolve.ledger", generation=int(generation), status="failed",
              error_code=str(getattr(exc, "code", E.E_IO_WRITE)), detail=str(exc)[:200])
    # 绘图：台账写完之后才画（否则画的是上一版台账）
    csv_path = Path(run_dir) / "metrics" / "generations.csv"
    out_dir = Path(run_dir) / "plots"
    try:
        written = plotting_mod.plot_generations(csv_path, out_dir,
                                                close_after=True)
    except InfraError as exc:
        out["plot_error"] = str(exc.code)
        _emit("infra.plot_skipped", generation=int(generation), code=str(exc.code),
              detail=str(exc)[:200])
        print(f"[{exc.code}] 绘图跳过：{exc.message}", file=sys.stderr)
        return out
    if not written:
        out["plot_error"] = str(E.E_IO_PLOT_UNAVAILABLE)
        _emit("infra.plot_skipped", generation=int(generation),
              code=str(E.E_IO_PLOT_UNAVAILABLE), detail="matplotlib 不可用或没有数据点")
        print(f"[{E.E_IO_PLOT_UNAVAILABLE}] 绘图未产出任何文件（matplotlib 缺失或无数据点）",
              file=sys.stderr)
        return out
    out["plots"] = [p.name for p in written]
    _emit("infra.plot_done", generation=int(generation), files=out["plots"],
          out_dir=str(out_dir))
    return out


def write_ext_metrics(run_dir: Path | str, generation: int, data: Mapping[str, Any]) -> Path:
    """扩展量外置到 ``metrics/gen-XXX.ext.json``（FR-08-17/P-19）。"""
    path = Path(run_dir) / "metrics" / f"gen-{int(generation):03d}.ext.json"
    write_json_atomic(path, dict(data))
    return path


def write_plots_data(run_dir: Path | str, generation: int, curve: Sequence[float | None], *,
                     basket: str = "H1", win_rate: float = 0.0,
                     n: int = 0) -> Path:
    """绘图 tidy 数据 ``metrics/plots_data/gen-XXX_entropy.json``（FR-08-22）。

    与 PRD §7 的示例同形：``{"generation":3,"buckets":[{"turn":1,"h_norm":0.71,"n":40}]}``。
    """
    path = Path(run_dir) / "metrics" / "plots_data" / f"gen-{int(generation):03d}_entropy.json"
    buckets = [{"turn": index + 1, "decision_index": index,
                "h_norm": (None if value is None else round(float(value), 4)),
                "n": int(n)}
               for index, value in enumerate(curve)]
    write_json_atomic(path, {"generation": int(generation), "basket": basket,
                             "win_rate": round(float(win_rate), 4), "n": int(n),
                             "buckets": buckets, "jitter": None})
    return path


def kind_distribution_path(run_dir: Path | str, generation: int) -> Path:
    """``metrics/kind_distribution/gen-XXX.json`` —— 该代的 ``action.kind`` 分布。

    **为什么必须落盘**（2026-10-11，D-M3-5/§3.4 的 `c3` 修复）：`c3` 的判据是
    ``jsd_kind(g, g-1) < freeze`` 且 ``win_rate`` 跳变 ≥0.05，而
    ``eval report`` 是**从轨迹重算**的路径——它手上只有当前代的结果，
    **没有 g−1 的 kind 分布** ⇒ 原实现只能写 ``jsds = [None] * (n_gen - 1)``
    （硬编码 null）⇒ **`c3` 在 `eval report` 路径上永不触发**（实测归档里 17 个代的
    `jsd_kind` 全是空）。落盘之后，重算路径可以读回 g−1 的分布算出真值。
    """
    return Path(run_dir) / "metrics" / "kind_distribution" / f"gen-{int(generation):03d}.json"


def write_kind_distribution(run_dir: Path | str, generation: int,
                            distribution: Mapping[str, float]) -> Path:
    """落该代的 kind 分布（供 g+1 算 ``jsd_kind``）。"""
    path = kind_distribution_path(run_dir, generation)
    write_json_atomic(path, {"generation": int(generation),
                             "distribution": dict(distribution),
                             "total": round(sum(float(v) for v in distribution.values()), 6)})
    return path


def read_kind_distribution(run_dir: Path | str, generation: int) -> dict[str, float] | None:
    """读某代的 kind 分布；缺失/损坏 → ``None``（调用方据此把 jsd 记 ``None``，
    **不是** 0.0——0.0 会被读成「分布完全没变」，从而**误报冻结**）。"""
    path = kind_distribution_path(run_dir, generation)
    if not path.is_file():
        return None
    try:
        raw = read_json(path)
    except InfraError: # pragma: no cover - 损坏文件按缺失处理
        return None
    dist = raw.get("distribution")
    if not isinstance(dist, Mapping) or not dist:
        return None
    return {str(k): float(v) for k, v in dist.items()}


def adjacent_jsd_kind(run_dir: Path | str, generation: int, *,
                      base: float = 2.0) -> float | None:
    """``jsd_kind(g−1, g)``；任一代的 kind 分布缺失 → ``None``。

    **``None`` 不是 0.0**：0.0 会被 `c3` 读成「分布完全没变」⇒ **误报冻结**。
    """
    dist = read_kind_distribution(run_dir, generation)
    prev = read_kind_distribution(run_dir, generation - 1)
    if dist is None or prev is None:
        return None
    from .diversity import js_divergence

    return float(js_divergence(prev, dist, base=base))


def aligned_jsd_kinds(run_dir: Path | str, generations: Sequence[int], *,
                      base: float = 2.0) -> list[float | None]:
    """构造与 ``generations`` **等长**的 ``jsd_kind`` 序列（最后一个元素是相邻两代的真值）。

    `collapse_check` 的 ``jsd_kind`` 必须与 ``curves`` 等长（它按代索引取 ``jsds[i]``）。
    首位没有前驱 ⇒ ``None``；其余按 ``jsd(g-1, g)`` 逐个算（缺分布即 ``None``）。
    """
    gens = [int(g) for g in generations]
    if not gens:
        return []
    out: list[float | None] = [None]
    for gen in gens[1:]:
        out.append(adjacent_jsd_kind(run_dir, gen, base=base))
    return out[: len(gens)]


def utc_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_episode_summaries(run_dir: Path | str, results: Sequence[EpisodeResult], *,
                            split: Split, generation: int, arm: str,
                            protocol: str | None = None) -> list[Path]:
    """把逐局摘要写成 ``episodes/<split>/e-{seed}-g{gen}-{arm}.jsonl``（Spec §4.5）。

    一行 ``{"type":"episode_end", ...}``：主指标与 ``decision_kinds`` 都在其中，
    因此 ``eval report`` / ``eval diversity`` 可以从 run 工件**重新聚合**（G3/T-13）。
    真实轨迹的逐步行仍归 MOD-06；本函数只保证测量层可自洽复算。

    ``protocol``：本局的「测量协议」标识（缺省 = ``EPISODE_PROTOCOL``）。基线会把它细化成
    「协议 + 冻结模板摘要」，使 ``--resume`` 连**模板换过**这件事也拦得住（F18/F23）。
    """
    from dice_ratchet.infra.io import write_bytes_atomic

    root = Path(run_dir) / "episodes" / split
    root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for result in results:
        key = f"e-{int(result.seed):06d}-g{int(generation):03d}-{arm}"
        # 显式的 ``arm`` 形参是权威值（``result.as_dict()`` 里也有一份 ``arm``）：
        # 行里的 ``arm`` 是 ``load_episode_results`` 唯一的过滤依据（F5），必须一致。
        row = {"type": "episode_end", "episode_key": key,
               "generation": int(generation), **result.as_dict(),
               "arm": arm or result.arm,
               "protocol": protocol or EPISODE_PROTOCOL}
        payload = json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
        path = root / f"{key}.jsonl"
        write_bytes_atomic(path, payload.encode("utf-8"))
        written.append(path)
    return written


# =====================================================================
# 真实执行器：SandboxEnv 上跑一局（MOD-01/02 门面，INV-1）
# =====================================================================

class SandboxRunner:
    """在 ``SandboxEnv`` 上跑一局并把结果归一为 ``EpisodeResult``（通用执行器）。

    ``agent_factory(seed, arm)`` 返回一个对象：实现 ``reset(seed)`` + ``act(obs, legal, hints)``
    的基线 agent，**或** MOD-04 ``PolicyAgent``（检测到 ``play_decision`` 时走决策点循环）。
    轨迹落盘归 MOD-06；本执行器只在被显式要求时产出 summary。
    """

    def __init__(self, *, agent_factory: Callable[[int, str], Any],
                 episode_config: Any = None, run_id: str = "",
                 honeypot: bool = True, policy_failure_streak: int = 3,
                 illegal_penalty: float = -0.05,
                 max_decisions_guard: int = 400, memory: Any = None,
                 config: Any = None) -> None:
        self.agent_factory = agent_factory
        self.episode_config = episode_config
        self.run_id = run_id
        self.honeypot = honeypot
        self.policy_failure_streak = int(policy_failure_streak)
        self.illegal_penalty = float(illegal_penalty)
        self.max_decisions_guard = int(max_decisions_guard)
        self.memory = memory
        self.config = config

    # ---- 入口 ---------------------------------------------------------
    def run_episode(self, seed: int, *, split: Split, arm: str, generation: int,
                    harness_version: int, episode_config: Any = None,
                    timeout_s: float | None = None) -> EpisodeResult:
        from dice_ratchet.sandbox.api import EpisodeConfig, make_default_env

        cfg = episode_config or self.episode_config
        if cfg is None:
            # 配置回落（Spec §5 的优先级：configs/default.yaml > 代码默认值）。
            # ``baseline`` / ``play`` 等 MOD-08 入口**不显式传** ``episode_config``，此前会
            # 静默落到 ``EpisodeConfig()`` 的代码默认（hard × 6、toggles 全开），于是
            # ``env.episode.*``（difficulty / stage_count / toggles / character）对这些路径
            # **完全失效**。2026-10-07 补 D-2 的 medium × 6 基线时暴露：带
            # ``DICERATCHET_ENV__EPISODE__DIFFICULTY=medium`` 的局与 hard × 6 逐位相同。
            # M1 的归档未受影响纯属巧合——代码默认恰等于当时的配置默认。
            # MOD-06 的 ``EpisodeRunner`` 不受影响：``loop/cli.py`` 显式传了 ``cfg.env.episode``。
            env_cfg = getattr(self.config, "env", None) if self.config is not None else None
            cfg = getattr(env_cfg, "episode", None)
        if cfg is not None and not isinstance(cfg, EpisodeConfig):
            cfg = EpisodeConfig.from_any(cfg)
        episode_id = f"e-{int(seed):06d}-g{int(generation):03d}-{arm}"
        # F20：``evals.seed_timeout_s`` 真正被消费——每个决策点前检查 deadline，
        # 超预算的局按 Spec §8.5 记 dropped（计 n、按 lose 计），而不是让 eval 永久挂起。
        deadline = None if timeout_s is None else time.monotonic() + float(timeout_s)
        env = make_default_env(episode_config=cfg, honeypot=self.honeypot,
                               policy_failure_streak=self.policy_failure_streak,
                               illegal_penalty=self.illegal_penalty,
                               episode_id=episode_id)
        try:
            agent = self.agent_factory(int(seed), arm)
            if hasattr(agent, "play_decision"):
                return self._policy_loop(env, agent, seed=seed, split=split, arm=arm,
                                         generation=generation,
                                         harness_version=harness_version,
                                         deadline=deadline)
            return self._agent_loop(env, agent, seed=seed, split=split, arm=arm,
                                    generation=generation,
                                    harness_version=harness_version,
                                    deadline=deadline)
        finally:
            env.close()

    # ---- 基线 agent 循环 ----------------------------------------------
    def _agent_loop(self, env: Any, agent: Any, *, seed: int, split: Split, arm: str,
                    generation: int, harness_version: int,
                    deadline: float | None = None) -> EpisodeResult:
        if hasattr(agent, "reset"):
            try:
                agent.reset(seed)
            except TypeError: # PolicyAgent.reset(ctx)：非本循环
                pass
        obs = env.reset(seed)
        steps = illegal = fallback = 0
        kind_counts = [0] * 16
        slot_counts: dict[str, int] = {}
        decision_kinds: list[int] = []
        actions: list[dict[str, Any]] = []
        last_index = -1
        last_step: Any = None
        degraded = _agent_degraded(agent)
        timed_out = False
        while True:
            if deadline is not None and time.monotonic() > deadline:
                timed_out = True
                break
            if obs.get("outcome"):
                break
            legal = env.legal_actions()
            if not legal:
                break
            hints = env.hints()
            action = agent.act(obs, legal, hints)
            degraded = degraded or _agent_degraded(agent)
            if action not in legal:
                action = self._fallback_action(obs, legal, hints)
                fallback += 1
            step = env.step(action)
            info = dict(step.info)
            if info.get("illegal"):
                illegal += 1
                replacement = self._fallback_action(obs, env.legal_actions(), env.hints())
                if replacement in env.legal_actions():
                    step = env.step(replacement)
                    action = replacement
                    info = dict(step.info)
                    fallback += 1
            if not info.get("illegal"):
                steps += 1
                actions.append(dict(action))
                index = int(info.get("decision_index") or 0)
                if index != last_index:
                    kind = kind_index(action)
                    if kind >= 0:
                        decision_kinds.append(kind)
                        kind_counts[kind] += 1
                    last_index = index
                key = slot_key(action, obs)
                if key:
                    slot_counts[key] = slot_counts.get(key, 0) + 1
            obs = step.observation
            last_step = step
            if step.terminated or step.truncated:
                break
        return _build_result(seed=seed, split=split, arm=arm, generation=generation,
                             harness_version=harness_version, obs=obs,
                             info=_step_info(last_step, obs),
                             truncated=bool(getattr(last_step, "truncated", False)),
                             steps=steps, illegal=illegal, fallback=fallback,
                             kind_counts=kind_counts, slot_counts=slot_counts,
                             decision_kinds=decision_kinds, actions=actions,
                             degraded=degraded, timed_out=timed_out)

    def _fallback_action(self, obs: Any, legal: Sequence[Any], hints: Any) -> Any:
        from dice_ratchet.agent.heuristic import heuristic_policy

        action = heuristic_policy(obs, legal, hints)
        if action in legal:
            return action
        return dict(legal[0]) if legal else {}

    # ---- MOD-04 PolicyAgent 循环 --------------------------------------
    def _policy_loop(self, env: Any, agent: Any, *, seed: int, split: Split, arm: str,
                     generation: int, harness_version: int,
                     deadline: float | None = None) -> EpisodeResult:
        from dice_ratchet.agent.policy import EpisodeContext

        ctx = EpisodeContext(run=self.run_id or "r-local",
                             episode=f"e-{int(seed):06d}-g{int(generation):03d}-{arm}",
                             seed=int(seed), split=split, mode="eval", tick=int(seed),
                             generation=int(generation), harness_version=int(harness_version),
                             memory=self.memory)
        agent.reset(ctx)
        obs = env.reset(seed, episode_id=ctx.episode)
        steps = illegal = fallback = 0
        kind_counts = [0] * 16
        slot_counts: dict[str, int] = {}
        decision_kinds: list[int] = []
        actions: list[dict[str, Any]] = []
        degraded = False
        guards = 0
        llm_decisions = 0
        timed_out = False
        last_step: Any = None
        while True:
            guards += 1
            if guards > self.max_decisions_guard:
                break
            if deadline is not None and time.monotonic() > deadline:
                timed_out = True
                break
            if obs.get("outcome"):
                break
            decision = agent.play_decision(env, obs, ctx=ctx)
            if any(str(getattr(rec, "source", "")) == "llm"
                   for rec in (getattr(decision, "steps", []) or [])):
                # F22 的粒度证据：**每个决策点**一次推理（vs 每个 env 步一次）
                llm_decisions += 1
            degraded = degraded or bool(getattr(decision, "degraded", False))
            fallback += 1 if str(getattr(decision, "source", "")) == "fallback" else 0
            for record in getattr(decision, "steps", []) or []:
                if bool(getattr(record, "illegal", False)):
                    illegal += 1
                    continue
                steps += 1
                action = getattr(record, "action", None) or {}
                actions.append(dict(action))
                kind = kind_index(action)
                if kind >= 0:
                    kind_counts[kind] += 1
                    decision_kinds.append(kind)
                key = slot_key(action, obs)
                if key:
                    slot_counts[key] = slot_counts.get(key, 0) + 1
            last = getattr(decision, "last", None)
            obs = getattr(last, "observation", obs) if last is not None else obs
            if last is not None:
                last_step = last
            if last is not None and (getattr(last, "terminated", False)
                                     or getattr(last, "truncated", False)):
                break
        return _build_result(seed=seed, split=split, arm=arm, generation=generation,
                             harness_version=harness_version, obs=obs,
                             info=_step_info(last_step, obs),
                             truncated=bool(getattr(last_step, "truncated", False)),
                             steps=steps, illegal=illegal, fallback=fallback,
                             kind_counts=kind_counts, slot_counts=slot_counts,
                             decision_kinds=decision_kinds, actions=actions,
                             degraded=degraded, decisions_llm=llm_decisions,
                             timed_out=timed_out)


def _agent_degraded(agent: Any) -> bool:
    """基线 agent 的降级标记（Spec §8.5）。

    基线 agent（如 ``build_agent("fixed_prompt", …)`` 返回的 MOD-04 ``PolicyAgent``）
    在拿不到可用的 MOD-03 客户端、或单次调用全局失败时置 ``degraded=True`` 并改用
    启发式动作。``_agent_loop`` / ``_policy_loop`` 必须把它抬进
    ``EpisodeResult.degraded``，否则整局降级会被静默当成真实 LLM 结果入档。
    """
    return bool(getattr(agent, "degraded", False))


def kind_index(action: Mapping[str, Any]) -> int:
    """动作 ``kind`` 在 ``KIND_SPACE`` 里的下标；未登记返回 ``-1``（公开给 MOD-06 复用）。"""
    from .metrics import KIND_INDEX

    return int(KIND_INDEX.get(str(action.get("kind")), -1))


def slot_key(action: Mapping[str, Any], obs: Mapping[str, Any]) -> str | None:
    """``play_face`` 的 ``slotKind×school`` 键（FR-08-20 的 jsd_slot 口径）。"""
    if str(action.get("kind")) != "play_face":
        return None
    slot_id = action.get("slotId")
    slot = next((s for s in (obs.get("slots") or []) if s.get("slotId") == slot_id), None)
    slot_kind = str((slot or {}).get("slotKind") or "unknown")
    die_id = action.get("dieId")
    die = next((d for d in (obs.get("hand") or []) if d.get("dieId") == die_id), None)
    school = str((die or {}).get("school") or "none")
    return f"{slot_kind}×{school}"


def _step_info(step: Any, obs: Mapping[str, Any]) -> dict[str, Any]:
    """最近一次 ``StepResult.info``（公开字段；没有步时退回观测里的 outcome）。

    主指标只从这里来（INV-7 白名单：``info.outcome`` / ``info.truncation_reason`` /
    ``info.stages_cleared`` / ``info.hearts_left`` / ``info.battles_won``）。
    """
    info = getattr(step, "info", None)
    if info:
        return dict(info)
    return {"outcome": obs.get("outcome"), "truncation_reason": None,
            "stages_cleared": 0, "hearts_left": 0, "battles_won": 0}


def _build_result(*, seed: int, split: Split, arm: str, generation: int,
                  harness_version: int, obs: Mapping[str, Any], info: Mapping[str, Any],
                  truncated: bool, steps: int, illegal: int, fallback: int,
                  kind_counts: list[int], slot_counts: Mapping[str, int],
                  decision_kinds: Sequence[int], actions: Sequence[Mapping[str, Any]],
                  degraded: bool = False, decisions_llm: int = 0,
                  timed_out: bool = False) -> EpisodeResult:
    outcome = "win" if str(info.get("outcome")) == "win" else "lose"
    if timed_out:
        # Spec §8.5：超时的局记 dropped，计 n 并按 lose 计（防幸存者偏差）
        outcome = "lose"
    payload = json.dumps([dict(a) for a in actions], sort_keys=True,
                         ensure_ascii=False, separators=(",", ":"))
    return EpisodeResult(
        seed=int(seed), split=split, generation=int(generation),
        harness_version=int(harness_version), outcome=outcome,
        truncation_reason=(str(info.get("truncation_reason")) if truncated
                           and info.get("truncation_reason") else None),
        stages_cleared=int(info.get("stages_cleared") or 0),
        hearts_left=int(info.get("hearts_left") or 0),
        battles_won=int(info.get("battles_won") or 0),
        decisions=int(obs.get("decisionIndex") or 0), steps=int(steps),
        illegal_attempts=int(illegal), fallback_decisions=int(fallback),
        kind_counts=tuple(kind_counts), slot_counts=dict(slot_counts),
        seq_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        dropped=bool(timed_out), degraded=bool(degraded),
        decision_kinds=tuple(decision_kinds), arm=arm,
        error_code=(str(E.E_EVAL_TIMEOUT) if timed_out else None),
        decisions_llm=int(decisions_llm))
