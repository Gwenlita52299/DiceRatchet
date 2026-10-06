"""MOD-07 集成测试夹具。

单测的构造器（``tests/unit/evolve/evolve_support.py``）本身就是纯构造工具、不依赖
pytest 的私有 fixture，因此这里直接复用同一份实现（**不复制**），避免两套造数逻辑
漂移。路径按 ``__file__`` 解析，与 pytest 的 rootdir/cwd 无关。
"""

from __future__ import annotations

import sys
from pathlib import Path

_UNIT_DIR = Path(__file__).resolve().parents[2] / "unit" / "evolve"
if str(_UNIT_DIR) not in sys.path:
    sys.path.insert(0, str(_UNIT_DIR))

from evolve_support import (CONFIG_JSON, DECISION_MD, REFLECTION_MD, RUN_ID, TOOL_PY,  # noqa: E402
                            FakeArm, FakeRunContext, FakeState, ScriptedArms, StubLLM,
                            context_for, heldout_dict, make_app_config, make_candidate,
                            make_config, make_report, paired_dict, prompt_diff,
                            proposal_payload, tool_diff, write_harness)

__all__ = [
    "CONFIG_JSON", "DECISION_MD", "REFLECTION_MD", "RUN_ID", "TOOL_PY", "FakeArm",
    "FakeRunContext", "FakeState", "ScriptedArms", "StubLLM", "context_for",
    "heldout_dict", "make_app_config", "make_candidate", "make_config", "make_report",
    "paired_dict", "prompt_diff", "proposal_payload", "tool_diff", "write_harness",
]
