"""MOD-06 loop：L2 进化层的**编排器**（PRD ``docs/prd/MOD-06-loop-PRD.md``）。

它自己不产生策略、不训练模型、不判定改进好坏，只负责让
``play → evaluate → reflect → evolve`` 这条闭环在一局、K 局、一代三个时间尺度上
正确、可复现、可中断、可恢复地跑起来。

模块布局（PRD §6 的三文件 + 三件基础设施）::

    state.py       RunState / Scoreboard / QueueLog —— state.json 的语义所有者
    trajectory.py  episodes/<split>/<key>.jsonl 的逐步落盘（FR-06-2）
    runner.py      EpisodeRunner.run_episode —— 单局时序（FR-06-1）
    reflect.py     确定性摘要 + 反思编排 + 降级（FR-06-3/4）
    generation.py  E_gen 成本闸门 + 周期幂等 + LoopOrchestrator（FR-06-5…15）
    cli.py         loop run|status|smoke（Spec §4.6）

对外面（Spec §4.6/§4.7 与 MOD-08 的 ``EpisodeRunner`` 协议）::

    LoopOrchestrator(run_ctx=..., config=..., runner=..., evolve_fn=...).run(seeds, resume=)
    EpisodeRunner(...).run_episode(seed, split=, arm=, episode_config=, harness_version=)
    episodes_per_generation(cfg) / maybe_run_generation(state=, run_ctx=, evolve_fn=)
    reflect.summarize_episode(rec, window=) / reflect.reflect(summary, client=, template=)

分层（NFR-09-5 / INV-1）：本包只从 ``gamer_rsi.sandbox.api`` 触达环境（经 MOD-04 的
``play_decision``），**不** import ``gamer_rsi.env.*``。
"""

from __future__ import annotations

from .generation import (EvolveOutcome, LoopOrchestrator, RunSummary, SignalGuard,
                         episodes_per_generation, estimated_hours, load_or_init_state,
                         maybe_run_generation, write_gen000_baseline)
from .reflect import (EpisodeSummary, RecentSummary, failure_causes, memory_tags,
                      reason_of, reflect, reflection_output_schema, skipped_record,
                      summarize_episode, ok_record, write_reflection_record)
from .runner import (EpisodeRunner, EvalRunnerAdapter, PolicyProto, RecordedEpisode,
                     RunContext, episode_key, read_harness_version)
from .state import (QueueLog, RunBudget, RunCounters, RunState, Scoreboard, init_state,
                    load_state, save_state, scoreboard_path, utc_now)
from .trajectory import StepMeta, TrajectoryWriter

__all__ = [
    # runner（FR-06-1/2/12）
    "EpisodeRunner", "EvalRunnerAdapter", "PolicyProto", "RecordedEpisode", "RunContext",
    "episode_key", "read_harness_version", "TrajectoryWriter", "StepMeta",
    # reflect（FR-06-3/4）
    "EpisodeSummary", "RecentSummary", "reflect", "summarize_episode",
    "write_reflection_record", "reason_of", "failure_causes", "memory_tags",
    "reflection_output_schema", "skipped_record", "ok_record",
    # generation（FR-06-5…15）
    "LoopOrchestrator", "RunSummary", "EvolveOutcome", "SignalGuard",
    "episodes_per_generation", "estimated_hours", "maybe_run_generation",
    "load_or_init_state", "write_gen000_baseline",
    # state（FR-06-8/9/10）
    "RunState", "RunCounters", "RunBudget", "Scoreboard", "QueueLog",
    "init_state", "load_state", "save_state", "scoreboard_path", "utc_now",
]
