"""MOD-08 evals：L3 测量层（PRD ``docs/prd/MOD-08-evals-PRD.md``；Spec v1.4 §4.7）。

**只读轨迹与报告，不参与决策**（Spec §1）。本包提供四组能力：

- ``seeds``：seed 协议唯一出口 + INV-5 三重护栏 + test 解锁存证；
- ``metrics``：INV-7 主指标、Wilson CI、配对检验、增益判定、报告渲染；
- ``diversity``：动作熵曲线/斜率、JSD、两点确认的 ``collapse_flag``；
- ``ablation``：去反思 / 去自编辑三臂消融与归因；
- ``harness``：执行器与 MOD-07 门面（``evaluate_arm`` / ``compare_paired`` /
  ``evaluate_generation``）；
- ``baselines``：random / heuristic / fixed_prompt 三基线 + 不可覆盖归档。

INV-7 边界：``reward.py``（shaped reward）只供学习与调试，**不被** ``metrics``/``harness``
的判定路径 import。
"""

from __future__ import annotations

from .ablation import (AblationArm, AblationReport, ablation_path, attribution,
                       combine_attribution, run_ablation)
from .baselines import (BASELINE_KINDS, BaselineAgent, HeuristicAgent, RandomAgent,
                        archive_baseline, baseline_arm, baseline_protocol, build_agent,
                        fixed_prompt_policy_config, run_baseline)
from .diversity import (CollapseConfig, collapse_check, distinct_sequence_ratio,
                        entropy_slope, js_divergence, kind_distribution, slot_distribution)
from .harness import (ArmResult, EpisodeRunner, EvalResult, EvalSpec, SandboxRunner,
                      TraceWriter, append_generations_csv, check_report_determinism,
                      compare_all_metrics, compare_paired, episode_result_from_rows,
                      evaluate_arm, evaluate_generation, generation_report_path,
                      enforce_report_determinism, generations_row_from_report,
                      generations_rows_from_report, kind_index, load_episode_results,
                      read_episode_protocols, report_sha_path, reproducibility_of,
                      run_eval, slot_key, utc_now,
                      write_ext_metrics, write_generation_report, write_plots_data)
from .metrics import (EpisodeResult, GainVerdict, CollapseVerdict, PairedTest, SplitMetrics,
                      aggregate, action_entropy_curve, bootstrap_mean_ci, bootstrap_seed,
                      illegal_rate, judge_gain, mcnemar_exact_p, metric_value,
                      normalized_entropy, paired_bootstrap_gap, power_note,
                      render_report_sections, wilson_ci)
from .ablation import ARM_MECHANISM
from . import seeds
from .seeds import (SeedProtocol, assert_split_isolation, default_protocol,
                    guard_memory_write, guard_prompt_context, load_protocol,
                    protocol_hash, resolve_basket, take, test_attestation_path,
                    test_unlock, train_window, validate_protocol)

__all__ = [
    # seeds
    "seeds", "SeedProtocol", "default_protocol", "load_protocol", "validate_protocol",
    "protocol_hash", "resolve_basket", "take", "train_window", "assert_split_isolation",
    "guard_prompt_context", "guard_memory_write", "test_unlock", "test_attestation_path",
    # metrics
    "EpisodeResult", "SplitMetrics", "GainVerdict", "CollapseVerdict", "PairedTest",
    "aggregate", "wilson_ci", "normalized_entropy", "action_entropy_curve",
    "bootstrap_mean_ci", "paired_bootstrap_gap", "mcnemar_exact_p", "judge_gain",
    "render_report_sections", "metric_value", "illegal_rate", "bootstrap_seed",
    "power_note",
    # diversity
    "CollapseConfig", "collapse_check", "js_divergence", "entropy_slope",
    "kind_distribution", "slot_distribution", "distinct_sequence_ratio",
    # harness / MOD-07 门面
    "EvalSpec", "EvalResult", "EpisodeRunner", "TraceWriter", "ArmResult", "SandboxRunner",
    "run_eval", "evaluate_arm", "compare_paired", "compare_all_metrics",
    "evaluate_generation", "load_episode_results", "read_episode_protocols",
    "episode_result_from_rows", "EPISODE_PROTOCOL", "kind_index", "slot_key",
    "write_generation_report", "check_report_determinism",
    "enforce_report_determinism", "reproducibility_of", "append_generations_csv",
    "generations_rows_from_report", "generations_row_from_report",
    "write_ext_metrics", "write_plots_data", "generation_report_path", "report_sha_path",
    # baselines
    "BASELINE_KINDS", "BaselineAgent", "RandomAgent", "HeuristicAgent", "baseline_arm",
    "baseline_protocol",
    "build_agent", "fixed_prompt_policy_config", "archive_baseline", "run_baseline",
    # ablation
    "AblationArm", "AblationReport", "run_ablation", "attribution", "combine_attribution",
    "ARM_MECHANISM", "ablation_path",
    "utc_now",
]

#: ``seeds.SeedProtocol`` 的公开别名（Spec §4.7 签名里写作 ``SeedProtocol``）
SeedProtocol = seeds.SeedProtocol
