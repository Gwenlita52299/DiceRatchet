"""MOD-07 evolve：L2 进化层的**自改写**模块（PRD ``docs/prd/MOD-07-evolve-PRD.md``）。

它负责 RSI 闭环中最危险的一段：让 agent 改写自己的 harness，并由环境 ground-truth
决定改动去留。设计重心不是"生成更聪明的改动"，而是**在结构上掐死自我确认环路**：

- **自我确认环路**：候选生成阶段物理上看不到 heldout/test 数据（INV-5，``proposer``），
  裁判只看环境原生结果（INV-7，``judge`` 全部指标委托 MOD-08），生成者与裁判者上下文不重叠；
- **模型坍缩**：接受判定强制 ``collapse_flag == False`` + 熵斜率下限（C4）；
- **过度优化/指标刷分**：改动面硬上限（``max_diff_lines``/``max_diff_chars``）+ ``win_rate_gap``
  阈值 + 消融对照。

模块布局（PRD §6.1）::

    schemas.py     Candidate / EvidenceBundle / Verdict / GateState + JSON Schema（G5）
    proposer.py    build_evidence_bundle / assert_no_holdout_leak / render_proposer_prompt
                   / propose / validate_candidate（FR-07-1…6）
    judge.py       evaluate_candidate（只编排 MOD-08）/ judge（§8.2 判定表 C0–C5）
    versioning.py  stage_candidate / accept / rollback / load_version / versions/v{N}/
                   + git commit + tag ``harness-v{N}``（FR-07-9/10）
    gate.py        HITL：await_human / resolve_gate / 超时保守回滚（FR-07-11）
    generation.py  run_one_generation —— MOD-06 ``evolve_fn`` 的实现（§8.1）
    cli.py         ``evolve approve|rollback``（Spec §4.6）
    paths.py       harness/ 与 runs/<run_id>/generations/ 的布局（Spec v1.4 B-29/B-32）

对外面（MOD-06 经组合根注入 ``evolve_fn``）::

    run_one_generation(state=..., run_ctx=..., runner=...) -> EvolveOutcome

分层（NFR-09-5 / INV-1）：本包只从 ``dice_ratchet.sandbox`` 触达环境（``guard.gate`` /
``guard.load_harness_tools``），**不** import ``dice_ratchet.env.*``。
"""

from __future__ import annotations

from .errors import (EVOLVE_ERROR_CODES, EvolveError, EvalFailedError, GateTimeoutError,
                     GitFailedError, HoldoutLeakError, JudgeFailedError, LoadFailedError,
                     NoCandidateError, PatchInvalidError, ProposeFailedError,
                     ScanFailedError, SmokeFailedError, VersionConflictError)
from .gate import (await_human, expire_if_needed, gate_expired, load_gate, read_gate,
                   resolve_gate, verdict_from_gate, write_gate)
from .generation import GenerationResult, guard_gate, load_harness_tools_checked, propose_seed, run_one_generation
from .judge import (CandidateEvaluator, better, evaluate_candidate, first_failure, gap_of,
                    judge, metric_of, pick_winner, smoke_fast_reject, smoke_seeds_for,
                    superseded_verdict)
from .paths import GenPaths, harness_version_dir, report_path, utc_now
from .proposer import (apply_unified_diff, assert_no_holdout_leak, build_evidence_bundle,
                       dedupe_candidates, harness_snapshot, order_candidates, propose,
                       render_proposer_prompt, set_client_provider, validate_candidate)
from .schemas import (CANDIDATE_KINDS, CANDIDATE_REPORT_SCHEMA, CANDIDATE_SCHEMA,
                      EFFECT_METRICS, EVIDENCE_SOURCE_TAGS, GATE_STATE_SCHEMA,
                      HARNESS_REF_SCHEMA, VERDICT_SCHEMA, Candidate, CandidateReport,
                      EvidenceBundle, ExpectedEffect, GateState, HarnessRef,
                      HarnessSnapshot, ProposalResult, ValidationResult, Verdict,
                      canonical_json, candidate_id_of, count_diff_lines, diff_digest,
                      parse_diff_paths, read_candidate_report, read_gate_state,
                      validate_candidate_dict, validate_candidate_report,
                      validate_evidence_bundle, validate_gate_state, validate_harness_ref,
                      validate_verdict, write_candidate_report)
from .versioning import (accept, artifact_hash_of, build_decision_sections, copy_harness_tree,
                         current_version, diff_trees, load_version, next_version, rollback,
                         snapshot_harness, stage_candidate, tree_digest, unified_diff)

__all__ = [
    # 错误（PRD §10）
    "EvolveError", "ProposeFailedError", "NoCandidateError", "HoldoutLeakError",
    "PatchInvalidError", "ScanFailedError", "LoadFailedError", "SmokeFailedError",
    "EvalFailedError", "JudgeFailedError", "GateTimeoutError", "VersionConflictError",
    "GitFailedError", "EVOLVE_ERROR_CODES",
    # 数据模型与 schema（PRD §6.1/§7）
    "Candidate", "CandidateReport", "EvidenceBundle", "ExpectedEffect", "GateState",
    "HarnessRef", "HarnessSnapshot", "ProposalResult", "ValidationResult", "Verdict",
    "CANDIDATE_KINDS", "EFFECT_METRICS", "EVIDENCE_SOURCE_TAGS",
    "CANDIDATE_SCHEMA", "CANDIDATE_REPORT_SCHEMA", "GATE_STATE_SCHEMA",
    "HARNESS_REF_SCHEMA", "VERDICT_SCHEMA",
    "canonical_json", "candidate_id_of", "count_diff_lines", "diff_digest",
    "parse_diff_paths", "write_candidate_report", "read_candidate_report",
    "read_gate_state", "validate_candidate_dict", "validate_candidate_report",
    "validate_evidence_bundle", "validate_gate_state", "validate_harness_ref",
    "validate_verdict",
    # proposer（FR-07-1…6）
    "build_evidence_bundle", "assert_no_holdout_leak", "render_proposer_prompt",
    "propose", "validate_candidate", "harness_snapshot", "apply_unified_diff",
    "dedupe_candidates", "order_candidates", "set_client_provider",
    # judge（FR-07-7/8）
    "evaluate_candidate", "judge", "CandidateEvaluator", "smoke_fast_reject",
    "smoke_seeds_for", "gap_of", "metric_of", "better", "pick_winner", "first_failure",
    "superseded_verdict",
    # versioning（FR-07-9/10）
    "stage_candidate", "accept", "rollback", "load_version", "current_version",
    "next_version", "build_decision_sections", "snapshot_harness", "artifact_hash_of",
    "tree_digest", "diff_trees", "unified_diff", "copy_harness_tree",
    # gate（FR-07-11）
    "await_human", "resolve_gate", "read_gate", "write_gate", "load_gate",
    "gate_expired", "expire_if_needed", "verdict_from_gate",
    # 编排（§8.1）
    "run_one_generation", "GenerationResult", "guard_gate", "load_harness_tools_checked",
    "propose_seed",
    # 路径
    "GenPaths", "harness_version_dir", "report_path", "utc_now",
]
