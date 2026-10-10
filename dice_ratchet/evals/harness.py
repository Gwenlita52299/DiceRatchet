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
- 报告落盘 ``report.json`` + ``report.sha256``；同参数重跑 sha 不一致 → ``E_EVAL_NONDETERMINISTIC``。
"""

from __future__ import annotations

import concurrent.futures
import csv
import hashlib
import io
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import (Any, Callable, Final, Iterable, Literal, Mapping, Protocol, Sequence,
                    runtime_checkable)

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
    "generation_report_path",
    "report_sha_path", "write_generation_report", "check_report_determinism",
    "append_generations_csv", "write_ext_metrics", "write_plots_data",
    "write_episode_summaries",
    "GENERATIONS_COLUMNS", "rng_for", "GainVerdictDiversity",
]

#: MOD-07 写进 ``generations/gen-NNN/report.json`` 的**真实判决**（§6.3 决策段）。
#: 用于「重算报告 / 台账不得覆盖已裁决的代」（F43）：``eval report`` 重算出的
#: ``decision`` 恒为 ``baseline``/``pending``，不能盖掉这三个。
VERDICT_DECISIONS: Final[tuple[str, ...]] = ("accept", "reject", "rollback")

#: ``metrics/generations.csv`` 的固定列（PRD §13；列定义归 MOD-08，B-06）
GENERATIONS_COLUMNS: tuple[str, ...] = (
    "generation", "harness_version", "parent_version", "split", "basket", "n", "win_rate",
    "ci_lo", "ci_hi", "mean_stages_cleared", "mean_hearts_left", "mean_battles_won",
    "mean_decisions", "illegal_rate", "fallback_rate", "truncation_rate",
    "entropy_mean_norm", "entropy_slope", "jsd_kind", "collapse_flag", "win_rate_gap",
    "verdict", "decision", "bootstrap_seed", "config_hash", "git_commit", "ts",
)

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
    queued = [int(s) for s in spec.seeds]
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
                             generation: int, harness_version: int,
                             arm: str = "") -> EpisodeResult | None:
    """从 ``episodes/<split>/*.jsonl`` 的 ``episode_end`` 行还原一局（无则 ``None``）。

    逐局汇总字段优先读 Spec §4.5 的裸键（``write_episode_summaries`` 的写法），缺失时
    回落到 ``x_`` 前缀扩展位——MOD-06 的真实轨迹按 ``trajectory_line`` schema 的
    ``EpisodeEnd`` 分支落盘（``additionalProperties:false``），只能把 MOD-08 复算多样性
    所需的 ``kind_counts/decision_kinds/slot_counts/seq_hash`` 放在 schema 预留的
    ``^x_`` 扩展位里。两条来源因此产出同一份 ``EpisodeResult``。
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

    outcome = "win" if str(end.get("outcome")) == "win" else "lose"
    truncation = end.get("truncation_reason")
    kind_raw = list(_pick("kind_counts") or [])
    return EpisodeResult(
        seed=int(end.get("seed", seed)), split=split, generation=int(generation),
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
    for path in sorted(root.glob("*.jsonl")):
        rows, _truncated = read_jsonl(path)
        if not rows:
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
            rows, seed=seed, split=split,
            generation=int(generation if generation is not None
                           else rows[0].get("generation") or 0),
            harness_version=int(harness_version), arm=row_arm or label)
        if result is not None:
            out.append(result)
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
        if generation is not None and int(rows[0].get("generation") or 0) != int(generation):
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
                 episode_config: Any = None) -> ArmResult:
    """单臂评估（Spec §4.7；FR-08-25）。

    ``runner`` 未注入时用 MOD-04/02 的默认执行器（MOD-06 会显式注入自己的 runner）。
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
    executor = runner or _default_runner(harness_dir=harness_dir, arm=arm,
                                         memory_version=memory_version, config=config,
                                         model_hash=model_hash, run_id=run_id,
                                         episode_config=episode_config)
    result = run_eval(spec, runner=executor, writer=None, protocol=proto)
    return ArmResult(arm=arm, split=split, basket=spec.basket_id,
                     harness_version=int(harness_version),
                     memory_version=int(memory_version or 0), run_id=run_id,
                     results=result.results, metrics=result.metrics,
                     model_hash=model_hash, error_codes=result.error_codes,
                     dur_s=result.dur_s)


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
    distributions: list[Mapping[str, float]] = []
    if parent is not None:
        distributions.append(kind_distribution(parent.results))
    if winner is not None:
        distributions.append(kind_distribution(winner.results))
    if len(distributions) >= 2:
        jsds = [None] + [js_divergence(distributions[0], distributions[1], base=base_value)]
    seq_ratios: list[float] = []
    if winner is not None:
        seq_ratios.append(distinct_sequence_ratio(winner.results))
    return collapse_check(curves=curves, jsd_kind=jsds, win_rates=win_rates, cfg=cfg,
                          seq_ratios=seq_ratios or None, generation=generation)


def evaluate_generation(generation: int, parent: ArmResult, winner: ArmResult | None, *,
                        run_id: str, run_dir: Path | str | None = None,
                        config: Any = None,
                        protocol: seeds_mod.SeedProtocol | Any = None) -> dict[str, Any]:
    """产出 report 的指标段（Spec §4.7；decision 段由 MOD-07 合并）。

    - ``winner`` 为候选的 H1 ``ArmResult``；``None`` 表示本代无候选通过（与 parent 同分）。
    - ``train`` 段取 ``train_window(generation)`` 在 run 里的训练局（滚动窗口，默认 60 局）。
    """
    proto = seeds_mod.validate_protocol(seeds_mod.coerce_protocol(protocol))
    eval_cfg = getattr(config, "evals", config)
    heldout = (winner.metrics if winner is not None else parent.metrics)
    window_seeds = seeds_mod.train_window(generation, protocol=proto, window=60)
    root = Path(run_dir) if run_dir is not None else Path("runs") / run_id
    train_results = load_episode_results(root, "train", generation=generation,
                                         harness_version=parent.harness_version,
                                         seeds=window_seeds)
    train_metrics = aggregate(train_results, split="train", basket="train_window",
                              t_max=int(getattr(eval_cfg, "t_max", 120)),
                              min_support=int(getattr(eval_cfg, "min_support", 3)),
                              drop_rate_max=float(getattr(eval_cfg, "drop_rate_max", 0.10)))
    diversity = _diversity_for_generation(run_id, generation, parent=parent, winner=winner,
                                          config=config, run_dir=root)
    spec = EvalSpec(run_id=run_id, generation=int(generation),
                    harness_version=heldout_harness_version(parent, winner),
                    split=heldout.split, basket=heldout.basket, seeds=parent.seeds)
    gain = judge_gain(
        train=train_metrics, heldout=heldout,
        gap_threshold=float(getattr(eval_cfg, "gap_threshold", 0.15)),
        alpha=float(getattr(eval_cfg, "alpha", 0.05)),
        collapse_flag=diversity.collapse_flag,
        candidate=(winner.results if winner is not None else parent.results),
        parent=parent.results, low_confidence=heldout.low_confidence,
        rng=rng_for(spec), B=int(getattr(eval_cfg, "bootstrap_B", 10000)))
    report = render_report_sections(
        generation=generation,
        harness_version=heldout_harness_version(parent, winner),
        parent_version=parent.harness_version,
        heldout=heldout, train=train_metrics, gain=gain, diversity=diversity,
        ablation=None, decision="pending", decided_by="auto", reason="",
        episode_config_hash=_config_digest(
            getattr(getattr(config, "env", None), "episode", None) if config else None),
        extra={"power_note": power_note(heldout.n)})
    return report


def heldout_harness_version(parent: ArmResult, winner: ArmResult | None) -> int:
    return int(winner.harness_version if winner is not None else parent.harness_version)


# =====================================================================
# 报告落盘（FR-08-17 / FR-08-22 / FR-08-23/24）
# =====================================================================

def generations_row_from_report(report: Mapping[str, Any], *, cfg: Any = None,
                                now: Any = None) -> dict[str, Any]:
    """从**报告字典**（MOD-07 ``generations/gen-NNN/report.json`` 形态）构造 csv 行。

    F43：``metrics/generations.csv`` 原先**只有** ``eval report`` 会写，而那条路径会把重算
    结果写进**同一个** ``report.json`` ⇒ 判据②（csv 可复算）只能靠破坏判据①（MOD-07 的
    决策记录）来满足。现在由**写报告的一方**（MOD-07 ``_finish_report``）追写本行，
    列集与 ``evals/cli.py::_csv_row`` 共用 ``GENERATIONS_COLUMNS``。
    """
    from dice_ratchet.infra.config import config_hash
    from datetime import datetime, timezone

    from dice_ratchet.infra.run import git_short_sha

    heldout = dict(report.get("heldout") or {})
    diversity = dict(report.get("diversity") or {})
    gain = dict(report.get("gain_realism") or {})
    ci = list(heldout.get("win_rate_ci95") or (0.0, 0.0))
    moment = now or datetime.now(tz=timezone.utc)
    return {
        "generation": int(report.get("generation") or 0),
        "harness_version": report.get("harness_version"),
        "parent_version": report.get("parent_version"),
        "split": "heldout",
        "basket": heldout.get("basket"),
        "n": heldout.get("n"),
        "win_rate": heldout.get("win_rate"),
        "ci_lo": ci[0],
        "ci_hi": ci[1],
        "mean_stages_cleared": heldout.get("mean_stages_cleared"),
        "mean_hearts_left": heldout.get("mean_hearts_left"),
        "mean_battles_won": heldout.get("mean_battles_won"),
        "mean_decisions": heldout.get("mean_decisions"),
        "illegal_rate": heldout.get("illegal_rate"),
        "fallback_rate": heldout.get("fallback_rate"),
        "truncation_rate": heldout.get("truncation_rate"),
        "entropy_mean_norm": heldout.get("entropy_mean_norm"),
        "entropy_slope": diversity.get("entropy_slope"),
        "jsd_kind": diversity.get("jsd_kind"),
        "collapse_flag": diversity.get("collapse_flag"),
        "win_rate_gap": gain.get("win_rate_gap"),
        "verdict": gain.get("verdict"),
        "decision": report.get("decision"),
        "bootstrap_seed": None,
        "config_hash": config_hash(cfg) if cfg is not None else None,
        "git_commit": git_short_sha(),
        "ts": moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


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


def _round_floats(obj: Any) -> Any:
    if isinstance(obj, float):
        return round(obj, 4)
    if isinstance(obj, dict):
        return {k: _round_floats(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round_floats(v) for v in obj]
    return obj


def append_generations_csv(run_dir: Path | str, row: Mapping[str, Any]) -> Path:
    """写一代一行到 ``metrics/generations.csv``（FR-08-22；列固定见 §13）。

    **每代恰好一行**（F43 的配套）：按 ``generation`` **upsert**，不是无脑追加——因为现在
    有**两个**写入方（MOD-07 ``_finish_report`` 在代末写它，``eval report`` 也写），无脑
    追加会给同一代留下两行互相矛盾的数字。

    **已裁决的 ``decision``/``verdict`` 不被覆盖**：MOD-07 写下的判决是判据①的唯一记录，
    而 ``eval report`` 的重算报告 ``decision`` 恒为 ``baseline``/``pending``。若重算在后，
    只更新指标列、保留判决列（与 ``evals/cli.py::_write_report_preserving_verdict`` 同口径）。
    """
    path = Path(run_dir) / "metrics" / "generations.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    incoming = {k: _round_floats(row.get(k)) for k in GENERATIONS_COLUMNS}
    generation = str(incoming.get("generation"))

    existing: list[dict[str, Any]] = []
    if path.is_file():
        try:
            with path.open(encoding="utf-8", newline="") as handle:
                existing = [dict(r) for r in csv.DictReader(handle)]
        except (OSError, csv.Error): # pragma: no cover - 坏账本按空重建
            existing = []

    replaced = False
    for index, prev in enumerate(existing):
        if str(prev.get("generation")) != generation:
            continue
        if str(prev.get("decision") or "") in VERDICT_DECISIONS:
            incoming["decision"] = prev.get("decision")
            if prev.get("verdict") not in (None, ""):
                incoming["verdict"] = prev.get("verdict")
        existing[index] = incoming
        replaced = True
        break
    if not replaced:
        existing.append(incoming)
    existing.sort(key=lambda r: int(r.get("generation") or 0))

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(GENERATIONS_COLUMNS), extrasaction="ignore")
    writer.writeheader()
    for r in existing:
        writer.writerow({k: r.get(k) for k in GENERATIONS_COLUMNS})

    from dice_ratchet.infra.io import write_bytes_atomic

    write_bytes_atomic(path, buf.getvalue().encode("utf-8"))
    return path


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
