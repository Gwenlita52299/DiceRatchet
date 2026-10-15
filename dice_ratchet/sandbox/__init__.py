"""MOD-02 env-sandbox：L0 与 L1 之间唯一合法的数据通道。

三条独立防线（PRD §1）：

- **投影裁剪**（``projection``）：agent 只可能看到 ``OBSERVATION_SCHEMA`` 登记过的字段。
- **代码/进程隔离**（``guard``）：静态 AST 门禁 + 运行期 import hook + 子进程 worker。
- **审计与蜜罐**（``audit`` / ``honeypot``）：6 类泄漏扫描 + canary 活体探针。

对外入口：``SandboxEnv``（agent 侧唯一）、``load_harness_tools``（工具执行）、
``run_audit``（审计）。导入本包即安装 agent 侧 import hook（INV-1）。
"""

from __future__ import annotations

from .api import ALL_ON_TOGGLES, EpisodeConfig, SandboxEnv, StepResult, make_default_env
from .audit import (AuditResult, LeakClass, LeakFinding, RULESET_VERSION, VisibleSet,
                    audit_to_console, check_ruleset_pin, rebuild_visible_set,
                    register_sensitive, run_audit, scan_text, write_report)
from .guard import (GuardFinding, GuardReport, GuardViolation, ToolWorker, analyze_code,
                    check_imports, gate, guarded_globals, install_agent_import_hook,
                    load_harness_tools, run_agent_tick)
from .honeypot import (FAKE_FIELD_NAMES, HONEYPOT_CANARY, HoneypotHit,
                       HoneypotSelfTestResult, injected_copy, reset_canary, self_test)
from .honeypot import inject as honeypot_inject
from .honeypot import scan as honeypot_scan
from .projection import (OBSERVATION_SCHEMA, OBSERVATION_SCHEMA_VERSION, ObsFieldSpec,
                         ProjectionCtx, ProjectionStat, canonical_json, check_changelog,
                         obs_digest, obs_field_paths, project_state, render_compact,
                         validate_observation)

__all__ = [
    # projection
    "OBSERVATION_SCHEMA", "OBSERVATION_SCHEMA_VERSION", "ObsFieldSpec", "ProjectionCtx",
    "ProjectionStat", "canonical_json", "check_changelog", "obs_digest",
    "obs_field_paths", "project_state", "render_compact", "validate_observation",
    # api
    "SandboxEnv", "EpisodeConfig", "StepResult", "ALL_ON_TOGGLES", "make_default_env",
    # guard
    "GuardViolation", "GuardFinding", "GuardReport", "ToolWorker", "analyze_code",
    "check_imports", "gate", "guarded_globals", "install_agent_import_hook",
    "load_harness_tools", "run_agent_tick",
    # audit
    "RULESET_VERSION", "LeakClass", "LeakFinding", "AuditResult", "VisibleSet",
    "rebuild_visible_set", "scan_text", "register_sensitive", "run_audit",
    "write_report", "audit_to_console", "check_ruleset_pin",
    # honeypot
    "HONEYPOT_CANARY", "FAKE_FIELD_NAMES", "HoneypotHit", "HoneypotSelfTestResult",
    "honeypot_inject", "injected_copy", "honeypot_scan", "reset_canary", "self_test",
]
