"""守卫测试（FR-09-15）—— T-09-9 / T-09-10，fixture 见 tests/fixtures/guards/。"""

from __future__ import annotations

from pathlib import Path

import pytest

from dice_ratchet.infra import events
from dice_ratchet.infra.errors import E
from dice_ratchet.infra.guards import (GuardFinding, scan_all, scan_determinism,
                                    scan_layer_imports)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "guards"


def _codes(findings: list[GuardFinding]) -> set[str]:
    return {f.code for f in findings}


def _by_path(findings: list[GuardFinding], suffix: str) -> list[GuardFinding]:
    return [f for f in findings if f.path.endswith(suffix)]


# ---- T-09-9 分层 import 门禁（INV-1 / NFR-09-5）------------------------------------------

def test_scan_layer_imports_detects_env_import():
    # T-09-9：agent 侧 import dice_ratchet.env.engine → 1 条 E_IO_GUARD_LAYER，定位到行号
    findings = scan_layer_imports(FIXTURES)
    bad = _by_path(findings, "agent/layer_bad.py")
    assert len(bad) == 1
    assert bad[0].code == E.E_IO_GUARD_LAYER
    assert bad[0].line > 0
    assert "dice_ratchet.env" in bad[0].message


def test_scan_layer_imports_allows_sandbox_bridge():
    # MOD-02 sandbox 是 INV-1 的唯一桥梁，env 层 import 环境层合法
    findings = scan_layer_imports(FIXTURES)
    assert _by_path(findings, "env/bridge_ok.py") == []


def test_scan_layer_imports_allows_legal_agent():
    findings = scan_layer_imports(FIXTURES)
    assert _by_path(findings, "agent/layer_ok.py") == []


def test_scan_layer_imports_infra_dependency_rule():
    # NFR-09-5：infra 不得 import 其他子包
    findings = scan_layer_imports(FIXTURES)
    bad = _by_path(findings, "infra/dep_bad.py")
    assert len(bad) == 1
    assert bad[0].code == E.E_IO_GUARD_LAYER
    ok = _by_path(findings, "infra/dep_ok.py")
    assert ok == []


# ---- 回归（2026-10-03 审计 P0-2）：相对 import 不得绕过分层门禁 --------------------------

def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_relative_env_import_is_rejected(tmp_path):
    """`from ..env.engine import X` 与绝对写法等价，必须报 E_IO_GUARD_LAYER。"""
    _write(tmp_path, "dice_ratchet/agent/policy.py",
           "from ..env.engine import GameEngine\n")
    findings = scan_layer_imports(tmp_path)
    assert len(findings) == 1
    assert findings[0].code == E.E_IO_GUARD_LAYER
    assert "dice_ratchet.env.engine" in findings[0].message


def test_relative_env_import_nested_in_function_is_rejected(tmp_path):
    _write(tmp_path, "dice_ratchet/loop/runner.py",
           "def go():\n    from ..env import engine\n    return engine\n")
    findings = scan_layer_imports(tmp_path)
    assert len(findings) == 1 and findings[0].line == 2


def test_relative_infra_reverse_dependency_is_rejected(tmp_path):
    """infra 用相对写法 import 其他子包同样违反 NFR-09-5。"""
    _write(tmp_path, "dice_ratchet/infra/cli_like.py",
           "from ..sandbox.audit import run_audit\n")
    findings = scan_layer_imports(tmp_path)
    assert len(findings) == 1 and findings[0].code == E.E_IO_GUARD_LAYER
    assert "dice_ratchet.sandbox" in findings[0].message


def test_relative_imports_inside_infra_are_allowed(tmp_path):
    _write(tmp_path, "dice_ratchet/infra/logging.py",
           "from . import events\nfrom .config import config_hash\nfrom .. import errors\n")
    assert scan_layer_imports(tmp_path) == []


def test_relative_import_to_infra_from_agent_is_allowed(tmp_path):
    """agent 可以依赖 infra（L3 横切层），只有 dice_ratchet.env 被 INV-1 禁止。"""
    _write(tmp_path, "dice_ratchet/agent/policy.py",
           "from ..infra.config import derive_seed\n")
    assert scan_layer_imports(tmp_path) == []


def test_layer_findings_honour_allow_comment(tmp_path):
    """FR-09-15：`# guard: allow` 对分层门禁同样生效（豁免但留痕）。"""
    _write(tmp_path, "dice_ratchet/agent/policy.py",
           "from ..env.engine import GameEngine  "
           "# guard: allow E_IO_GUARD_LAYER reason=legacy-bridge\n")
    findings = scan_layer_imports(tmp_path)
    assert len(findings) == 1
    assert findings[0].exempted is True
    assert findings[0].exempt_reason == "legacy-bridge"


def test_layer_allow_comment_with_wrong_code_not_exempted(tmp_path):
    _write(tmp_path, "dice_ratchet/agent/policy.py",
           "from ..env.engine import X  # guard: allow E_IO_GUARD_RANDOM reason=x\n")
    findings = scan_layer_imports(tmp_path)
    assert len(findings) == 1 and findings[0].exempted is False


def test_cli_layer_is_free_of_cross_package_imports():
    """回归（P0-2）：infra 本体不得 import 其他子包——组合根在 dice_ratchet/app.py。"""
    repo = Path(__file__).resolve().parents[3] / "dice_ratchet"
    hard = [f for f in scan_layer_imports(repo) if not f.exempted]
    assert hard == []


def test_scan_all_combines_both():
    findings = scan_all(FIXTURES)
    assert E.E_IO_GUARD_LAYER in _codes(findings)
    assert E.E_IO_GUARD_RANDOM in _codes(findings)


# ---- T-09-10 确定性守卫（INV-6）------------------------------------------------------------

def test_scan_determinism_detects_random_time_uuid():
    findings = scan_determinism(FIXTURES)
    bad = _by_path(findings, "agent/det_bad.py")
    assert bad
    assert all(f.code == E.E_IO_GUARD_RANDOM for f in bad)
    # random / time.time / uuid4 三类
    messages = " ".join(f.message for f in bad)
    assert "random" in messages
    assert "time" in messages
    assert "uuid" in messages


def test_scan_determinism_clean_code():
    findings = scan_determinism(FIXTURES)
    assert _by_path(findings, "agent/det_ok.py") == []


def test_scan_determinism_exemption_makes_exempted_but_visible():
    # T-09-10：加 # guard: allow 注释后 exempted=True 且仍可见（留痕）
    findings = scan_determinism(FIXTURES)
    exempt = _by_path(findings, "agent/det_exempt.py")
    # import random + random.random()/time.time()/uuid.uuid4() 共 4 条，全部豁免
    assert len(exempt) == 4
    assert all(f.exempted for f in exempt)
    assert all(f.exempt_reason == "demo-fixture" for f in exempt)


def test_exemption_without_reason_not_exempted(tmp_path):
    # 没有 reason= 的注释不算豁免
    src = tmp_path / "bad.py"
    src.write_text("import random  # guard: allow E_IO_GUARD_RANDOM\n", encoding="utf-8")
    findings = scan_determinism(tmp_path)
    assert findings and not findings[0].exempted


def test_exemption_wildcard_allowed(tmp_path):
    src = tmp_path / "ok.py"
    src.write_text("import random  # guard: allow * reason=test\n", encoding="utf-8")
    findings = scan_determinism(tmp_path)
    assert findings and findings[0].exempted


def test_exemption_wrong_code_not_exempted(tmp_path):
    src = tmp_path / "ok.py"
    src.write_text("import random  # guard: allow E_IO_GUARD_LAYER reason=test\n",
                   encoding="utf-8")
    findings = scan_determinism(tmp_path)
    assert findings and not findings[0].exempted


def test_numpy_random_detected(tmp_path):
    src = tmp_path / "np.py"
    src.write_text("import numpy.random as nr\n\n\ndef f():\n    return nr.rand()\n",
                   encoding="utf-8")
    findings = scan_determinism(tmp_path)
    assert _codes(findings) == {E.E_IO_GUARD_RANDOM}


# ---- 回归（2026-10-03 审计 P0-3）：numpy/torch 别名与显式 seed 放行 ---------------------

def _det(tmp_path: Path, name: str, body: str) -> list[GuardFinding]:
    (tmp_path / name).write_text(body, encoding="utf-8")
    return scan_determinism(tmp_path)


def test_numpy_alias_unseeded_is_detected(tmp_path):
    """`import numpy as np; np.random.rand()` 是最常见的漏检写法（旧实现 0 findings）。"""
    findings = _det(tmp_path, "a.py", "import numpy as np\n\ndef f():\n    return np.random.rand()\n")
    assert len(findings) == 1 and findings[0].code == E.E_IO_GUARD_RANDOM
    assert "numpy.random.rand" in findings[0].message


def test_numpy_seeded_generator_is_allowed(tmp_path):
    """MOD-08 NFR-08-01 的指定写法必须放行（旧实现误报）。"""
    findings = _det(tmp_path, "b.py",
                    "import numpy as np\n\ndef f(seed):\n"
                    "    return np.random.default_rng(seed).random()\n")
    assert findings == []


def test_numpy_unseeded_generator_is_detected(tmp_path):
    findings = _det(tmp_path, "c.py",
                    "import numpy as np\n\ndef f():\n"
                    "    return np.random.default_rng().random()\n")
    assert len(findings) == 1


def test_from_numpy_random_factory_only_is_allowed(tmp_path):
    findings = _det(tmp_path, "d.py",
                    "from numpy.random import default_rng\n\ndef f(s):\n"
                    "    return default_rng(s).random()\n")
    assert findings == []


def test_from_numpy_random_global_function_is_detected(tmp_path):
    findings = _det(tmp_path, "e.py",
                    "from numpy.random import rand\n\ndef f():\n    return rand()\n")
    assert _codes(findings) == {E.E_IO_GUARD_RANDOM}


def test_torch_global_random_detected_but_seeded_is_allowed(tmp_path):
    bad = _det(tmp_path, "f.py", "import torch\n\ndef f():\n    return torch.rand(3)\n")
    assert len(bad) == 1 and "torch.rand" in bad[0].message
    (tmp_path / "f.py").unlink()
    ok = _det(tmp_path, "g.py",
              "import torch\n\ndef f():\n    torch.manual_seed(0)\n    return torch.rand(3)\n")
    assert ok == []


def test_random_module_is_banned_even_when_seeded(tmp_path):
    """Spec §8.1 全禁 `random.*`（与 numpy 的「显式 seed 可放行」策略不同）。"""
    findings = _det(tmp_path, "h.py",
                    "import random\n\ndef f():\n    random.seed(0)\n    return random.random()\n")
    assert all(f.code == E.E_IO_GUARD_RANDOM for f in findings)
    assert any("random.random" in f.message for f in findings)


def test_syntax_error_reported(tmp_path):
    src = tmp_path / "broken.py"
    src.write_text("import random\n\ndef f(:\n", encoding="utf-8")
    findings = scan_determinism(tmp_path)
    assert _codes(findings) == {E.E_ENV_CODE_SYNTAX}


def test_scan_self_is_clean():
    # 守卫自身代码必须干净（否则无法作为门禁）
    repo = Path(__file__).resolve().parents[3] / "dice_ratchet"
    findings = scan_all(repo)
    hard = [f for f in findings if not f.exempted]
    assert hard == []


def test_emit_findings_leaves_trace():
    # FR-09-15：豁免也记 guard_finding 事件留痕（此处未绑定 run，计数到 dropped_unbound）
    from dice_ratchet.infra.guards import emit_findings
    events.set_dev_mode(False)
    before = events.stats()["dropped_unbound"]
    findings = [GuardFinding("a.py", 1, 0, E.E_IO_GUARD_RANDOM, "x", exempted=True,
                             exempt_reason="test")]
    emit_findings(findings)
    assert events.stats()["dropped_unbound"] == before + 1
