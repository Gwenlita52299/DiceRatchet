"""守卫测试（FR-09-15）—— T-09-9 / T-09-10，fixture 见 tests/fixtures/guards/。"""

from __future__ import annotations

from pathlib import Path

import pytest

from gamer_rsi.infra import events
from gamer_rsi.infra.errors import E
from gamer_rsi.infra.guards import (GuardFinding, scan_all, scan_determinism,
                                    scan_layer_imports)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "guards"


def _codes(findings: list[GuardFinding]) -> set[str]:
    return {f.code for f in findings}


def _by_path(findings: list[GuardFinding], suffix: str) -> list[GuardFinding]:
    return [f for f in findings if f.path.endswith(suffix)]


# ---- T-09-9 分层 import 门禁（INV-1 / NFR-09-5）------------------------------------------

def test_scan_layer_imports_detects_env_import():
    # T-09-9：agent 侧 import gamer_rsi.env.engine → 1 条 E_IO_GUARD_LAYER，定位到行号
    findings = scan_layer_imports(FIXTURES)
    bad = _by_path(findings, "agent/layer_bad.py")
    assert len(bad) == 1
    assert bad[0].code == E.E_IO_GUARD_LAYER
    assert bad[0].line > 0
    assert "gamer_rsi.env" in bad[0].message


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


def test_syntax_error_reported(tmp_path):
    src = tmp_path / "broken.py"
    src.write_text("import random\n\ndef f(:\n", encoding="utf-8")
    findings = scan_determinism(tmp_path)
    assert _codes(findings) == {E.E_ENV_CODE_SYNTAX}


def test_scan_self_is_clean():
    # 守卫自身代码必须干净（否则无法作为门禁）
    repo = Path(__file__).resolve().parents[3] / "gamer_rsi"
    findings = scan_all(repo)
    hard = [f for f in findings if not f.exempted]
    assert hard == []


def test_emit_findings_leaves_trace():
    # FR-09-15：豁免也记 guard_finding 事件留痕（此处未绑定 run，计数到 dropped_unbound）
    from gamer_rsi.infra.guards import emit_findings
    events.set_dev_mode(False)
    before = events.stats()["dropped_unbound"]
    findings = [GuardFinding("a.py", 1, 0, E.E_IO_GUARD_RANDOM, "x", exempted=True,
                             exempt_reason="test")]
    emit_findings(findings)
    assert events.stats()["dropped_unbound"] == before + 1
