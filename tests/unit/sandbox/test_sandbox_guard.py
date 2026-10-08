"""MOD-02 门禁与受限执行单测（PRD §11；FR-02-9 ~ FR-02-15，INV-1/INV-4）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from dice_ratchet.infra.errors import E
from dice_ratchet.sandbox import guard as G

LEGAL_TOOL = '''
"""合法工具：纯函数 + 运算符/比较/布尔/推导式/sorted/len/range/max/min。"""
from math import sqrt


def rank_actions(obs, candidates, memory):
    scored = []
    for i, cand in enumerate(candidates):
        kind = cand.get("kind", "")
        base = 3 if kind == "play_face" else 1
        if cand.get("value") is not None and cand["value"] > 2:
            base = base + 2
        elif cand.get("bonus", 0) >= 1 and not cand.get("disabled", False):
            base = base * 2
        base = base - (i % 2)
        scored.append((base, i))
    order = sorted(scored, reverse=True)
    ordered = [candidates[i] for _, i in order]
    if len(ordered) < 2:
        return ordered
    bonus = [c for c in ordered if c.get("kind") == "play_face"]
    rest = [c for c in ordered if c.get("kind") != "play_face"]
    n = min(len(bonus), max(1, len(rest) - 1))
    head = [bonus[j] for j in range(n)]
    tail = [c for c in ordered if c not in head]
    return head + tail
'''

SLOW_TOOL = '''
import time


def slow_rank(obs, candidates, memory):
    time.sleep(5)
    return candidates
'''

MALICIOUS = [
    ('open("/etc/passwd")', "E_ENV_AST_DENIED"),
    ("import socket", "E_ENV_AST_DENIED"),
    ('__import__("os")', "E_ENV_AST_DENIED"),
    ("eval('1+1')", "E_ENV_AST_DENIED"),
    ("exec('x=1')", "E_ENV_AST_DENIED"),
    ('getattr(x, "_np_secret_price")', "E_ENV_AST_DENIED"),
    ("while True:\n    pass", "E_ENV_AST_DENIED"),
    ("class X:\n    pass", "E_ENV_AST_DENIED"),
]


# ---- FR-02-9 静态 import 门禁 ------------------------------------------------------

def test_check_imports_flags_fake_l1_layer_file(tmp_path):
    fake = tmp_path / "det_fake.py"
    fake.write_text("import json\n"
                    "from dice_ratchet.env.engine import Engine\n"
                    "x = Engine\n", encoding="utf-8")
    report = G.check_imports([fake])
    assert report.ok is False
    assert report.findings[0].code == E.E_ENV_IMPORT_DENIED
    assert report.findings[0].lineno == 2
    assert report.n_imports == 2


def test_check_imports_accepts_clean_layer(tmp_path):
    good = tmp_path / "det_ok.py"
    good.write_text("from dice_ratchet.sandbox.api import SandboxEnv\n", encoding="utf-8")
    report = G.check_imports([good])
    assert report.ok is True and report.findings == ()


def test_check_imports_reports_syntax_error(tmp_path):
    bad = tmp_path / "broken.py"
    bad.write_text("def f(:\n", encoding="utf-8")
    report = G.check_imports([bad])
    assert report.ok is False
    assert report.findings[0].code == E.E_ENV_CODE_SYNTAX


def test_check_imports_scans_repo_layer_packages(repo_root):
    """真实 L1–L3 目录目前不存在或为空 → 0 命中（CI 门禁的调用形态）。"""
    roots = [repo_root / "dice_ratchet" / name for name in
             ("agent", "memory", "loop", "evolve", "evals", "inference")]
    report = G.check_imports([p for p in roots if p.exists()])
    assert report.ok is True


# ---- FR-02-10 运行期 import hook ---------------------------------------------------

@pytest.fixture(autouse=True)
def _hook_installed():
    G.install_agent_import_hook()
    yield
    G.install_agent_import_hook()


def test_import_hook_denies_agent_layer_frame():
    namespace = {"__name__": "dice_ratchet.agent.fake_policy"}
    with pytest.raises(G.GuardViolation) as exc:
        exec(compile("import dice_ratchet.env.rng", "<fake>", "exec"), namespace)
    assert exc.value.code == E.E_ENV_IMPORT_DENIED
    assert "dice_ratchet.agent.fake_policy" in str(exc.value)


def test_import_hook_allows_non_agent_frame():
    namespace = {"__name__": "tests.sandbox_probe"}
    exec(compile("import dice_ratchet.env.rng as r", "<probe>", "exec"), namespace)
    assert namespace["r"].STREAM_NAMES


def test_import_hook_allows_sandbox_own_imports():
    namespace = {"__name__": "dice_ratchet.sandbox.probe"}
    exec(compile("from dice_ratchet.env import GameEngine", "<probe>", "exec"), namespace)
    assert namespace["GameEngine"] is not None


def test_import_hook_is_installed_and_permanent():
    assert G.install_agent_import_hook.__module__ == "dice_ratchet.sandbox.guard"
    finders = [f for f in sys.meta_path if isinstance(f, G._AgentImportFinder)]
    assert finders
    sys.meta_path.remove(finders[0]) # 试图卸载
    G.install_agent_import_hook() # 重新装回（不可卸载）
    assert any(isinstance(f, G._AgentImportFinder) for f in sys.meta_path)


# ---- FR-02-12 AST 黑名单/白名单 ----------------------------------------------------

@pytest.mark.parametrize("source,code", MALICIOUS)
def test_analyze_code_rejects_malicious_snippets(source, code):
    report = G.analyze_code(source, filename="bad.py")
    assert report.ok is False
    assert report.findings[0].code == code
    assert report.findings[0].lineno == 1
    assert report.findings[0].col == 0


def test_analyze_code_reports_all_violations_with_lines():
    source = "import os\nopen('x')\neval('1')\n"
    report = G.analyze_code(source)
    codes = [f.code for f in report.findings]
    assert codes.count(E.E_ENV_AST_DENIED) >= 3
    assert [f.lineno for f in report.findings] == [1, 2, 3]


def test_analyze_code_rejects_syntax_error():
    report = G.analyze_code("def f(:\n", filename="x.py")
    assert report.ok is False and report.findings[0].code == E.E_ENV_CODE_SYNTAX


def test_analyze_code_blocks_dunder_escape():
    report = G.analyze_code("x = ().__class__")
    assert report.ok is False
    assert "dunder" in report.findings[0].detail


def test_analyze_code_blocks_relative_import():
    report = G.analyze_code("from . import utils")
    assert report.ok is False


def test_analyze_code_allows_operator_expressions():
    """v1.0 的节点白名单漏掉 Add/Eq/And/USub → 合法工具被误杀（FR-02-12 回归）。"""
    assert G.analyze_code(LEGAL_TOOL).ok is True


def test_analyze_code_import_allowlist_override():
    assert G.analyze_code("import statistics").ok is False
    assert G.analyze_code("import statistics",
                          import_allowlist=("statistics",)).ok is True


def test_analyze_code_blocks_dice_ratchet_sandbox_import():
    assert G.analyze_code("import dice_ratchet.sandbox.guard").ok is False


def test_guard_violation_is_exception_with_code():
    exc = G.GuardViolation(E.E_ENV_AST_DENIED, "nope", lineno=3, col=1)
    assert isinstance(exc, Exception)
    assert exc.code == E.E_ENV_AST_DENIED and exc.lineno == 3 and exc.col == 1
    assert "line 3:1" in str(exc)


# ---- FR-02-13 gate：fail-closed + 落盘 --------------------------------------------

def _harness(tmp_path: Path, tools: dict[str, str], version: int = 7) -> Path:
    harness = tmp_path / "harness"
    (harness / "tools").mkdir(parents=True, exist_ok=True)
    for name, body in tools.items():
        (harness / "tools" / name).write_text(body, encoding="utf-8")
    (harness / "version.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    return harness


def test_gate_accepts_legal_tools_and_writes_report(tmp_path, repo_root):
    harness = _harness(tmp_path, {"rank.py": LEGAL_TOOL})
    run_dir = tmp_path / "runs" / "r1"
    report = G.gate(harness, run_dir=run_dir)
    assert report.ok is True and len(report.scanned_files) == 1
    out = run_dir / "audit" / "harness-gate.json"
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["ok"] is True and payload["harness_version"] == 7
    assert payload["findings"] == []
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((repo_root / "schemas/harness_gate.schema.json")
                        .read_text(encoding="utf-8"))
    jsonschema.validate(instance=payload, schema=schema)


def test_gate_rejects_whole_batch_fail_closed(tmp_path):
    harness = _harness(tmp_path, {"good.py": LEGAL_TOOL, "bad.py": "import os\n"})
    report = G.gate(harness, run_dir=tmp_path / "run")
    assert report.ok is False
    assert report.findings and report.findings[0].file.endswith("bad.py")
    assert any("bad.py" in v for v in report.violations)


def test_gate_max_tools_enforced(tmp_path):
    harness = _harness(tmp_path, {f"t{i}.py": LEGAL_TOOL for i in range(3)})
    report = G.gate(harness, max_tools=2)
    assert report.ok is False
    assert "超上限" in report.findings[0].detail


def test_gate_keeps_old_harness_untouched(tmp_path):
    harness = _harness(tmp_path, {"bad.py": "import os\n"})
    before = (harness / "tools" / "bad.py").read_text(encoding="utf-8")
    G.gate(harness)
    assert (harness / "tools" / "bad.py").read_text(encoding="utf-8") == before


def test_gate_without_run_dir_does_not_write(tmp_path):
    harness = _harness(tmp_path, {"rank.py": LEGAL_TOOL})
    assert G.gate(harness) is not None
    assert not list(tmp_path.glob("**/harness-gate.json"))


def test_gate_env_run_dir(monkeypatch, tmp_path):
    harness = _harness(tmp_path, {"rank.py": LEGAL_TOOL})
    monkeypatch.setenv("DICERATCHET_RUN_DIR", str(tmp_path / "envrun"))
    G.gate(harness)
    assert (tmp_path / "envrun" / "audit" / "harness-gate.json").is_file()


def test_gate_report_raise_if_blocked(tmp_path):
    harness = _harness(tmp_path, {"bad.py": "import os\n"})
    report = G.gate(harness)
    with pytest.raises(G.GuardViolation) as exc:
        report.raise_if_blocked()
    assert exc.value.code == E.E_ENV_AST_DENIED
    assert report.codes() == (E.E_ENV_AST_DENIED,)


# ---- FR-02-15 guarded_globals：受限 builtins + 蜜罐哨兵 ---------------------------

def test_guarded_globals_restricts_builtins():
    g = G.guarded_globals()
    assert "open" not in g["__builtins__"]
    assert "eval" not in g["__builtins__"]
    assert g["__builtins__"]["len"] is len
    with pytest.raises(G.GuardViolation) as exc:
        g["__builtins__"]["__import__"]("os")
    assert exc.value.code == E.E_ENV_AST_DENIED
    assert g["__builtins__"]["__import__"]("math").sqrt(4) == 2.0


@pytest.mark.parametrize("name", G.HONEYPOT_SENTINEL_NAMES)
def test_honeypot_sentinel_trips_escape(name):
    g = G.guarded_globals()
    with pytest.raises(G.GuardViolation) as exc:
        bool(g[name])
    assert exc.value.code == E.E_ENV_ESCAPE


def test_honeypot_sentinel_repr_is_safe():
    g = G.guarded_globals()
    assert "honeypot sentinel" in repr(g["_HONEYPOT_LEDGER"])


def test_harness_tool_marks_attribute():
    @G.harness_tool
    def rank_actions(obs, candidates, memory):
        return candidates

    assert rank_actions.__harness_tool__ is True


# ---- FR-02-11 run_agent_tick：一次性子进程边界 ------------------------------------

def test_run_agent_tick_ok():
    result = G.run_agent_tick({
        "code": "def tick(value, **kw):\n    return {'value': value * 2}\n",
        "entry": "tick", "kwargs": {"value": 21}}, timeout_s=20.0)
    assert result == {"value": 42}


def test_run_agent_tick_denies_file_access():
    with pytest.raises(G.GuardViolation) as exc:
        G.run_agent_tick({
            "code": "def tick(**kw):\n    open('/etc/passwd')\n    return {}\n",
            "entry": "tick"}, timeout_s=20.0)
    assert exc.value.code == E.E_ENV_AST_DENIED


def test_run_agent_tick_timeout():
    with pytest.raises(G.GuardViolation) as exc:
        G.run_agent_tick({"code": "def tick(**kw):\n"
                                  "    while True:\n"
                                  "        pass\n", "entry": "tick"}, timeout_s=0.5)
    assert exc.value.code == E.E_ENV_TIMEOUT


# ---- FR-02-14 ToolWorker：常驻子进程 + 超时/资源/逃逸 -----------------------------

def _worker(tmp_path: Path, tools: dict[str, str], **kw) -> G.ToolWorker:
    harness = tmp_path / "harness"
    (harness / "tools").mkdir(parents=True, exist_ok=True)
    for name, body in tools.items():
        (harness / "tools" / name).write_text(body, encoding="utf-8")
    return G.load_harness_tools(harness, timeout_s=0.6, **kw)


def _cands():
    return [{"kind": "end_turn"}, {"kind": "play_face", "value": 5, "bonus": 0},
            {"kind": "discard_die"}]


def test_worker_runs_legal_tool_and_lists_tools(tmp_path):
    worker = _worker(tmp_path, {"rank.py": LEGAL_TOOL})
    try:
        assert "rank_actions" in worker.tools()
        out = worker.call("rank_actions", obs={}, candidates=_cands(), memory=[])
        assert [c["kind"] for c in out][0] == "play_face" # 正例：重排生效
        assert sorted(c["kind"] for c in out) == sorted(c["kind"] for c in _cands())
        assert worker.alive
    finally:
        worker.close()


def test_worker_timeout_kills_and_restarts(tmp_path):
    worker = _worker(tmp_path, {"rank.py": LEGAL_TOOL, "slow.py": SLOW_TOOL},
                     import_allowlist=("time", "math"))
    try:
        with pytest.raises(G.GuardViolation) as exc:
            worker.call("slow_rank", obs={}, candidates=_cands(), memory=[],
                        timeout_s=0.3)
        assert exc.value.code == E.E_ENV_TIMEOUT
        assert worker.restarts >= 1
        # 自动重启后同一个 worker 仍可正常调用（FR-02-14：超时即 kill + 重启）
        out = worker.call("rank_actions", obs={}, candidates=_cands(), memory=[],
                          timeout_s=2.0)
        assert out[0]["kind"] == "play_face"
    finally:
        worker.close()


def test_worker_escape_sentinel_reports_escape(tmp_path):
    body = ("def rank_actions(obs, candidates, memory):\n"
            "    if _HONEYPOT_LEDGER:\n"
            "        return candidates\n"
            "    return candidates\n")
    worker = _worker(tmp_path, {"sneaky.py": body})
    try:
        with pytest.raises(G.GuardViolation) as exc:
            worker.call("rank_actions", obs={}, candidates=_cands(), memory=[])
        assert exc.value.code == E.E_ENV_ESCAPE
    finally:
        worker.close()


def test_worker_resource_limit(tmp_path):
    body = ("def rank_actions(obs, candidates, memory):\n"
            "    blob = 'x' * (1024 * 1024 * 1024)\n"
            "    return [{'kind': str(len(blob))}]\n")
    worker = _worker(tmp_path, {"hog.py": body}, mem_limit_mb=64)
    try:
        try:
            worker.call("rank_actions", obs={}, candidates=_cands(), memory=[],
                        timeout_s=10.0)
        except G.GuardViolation as exc:
            assert exc.code == E.E_ENV_RESOURCE
        else:
            pytest.skip("本平台未强制执行 RLIMIT_AS（PRD §14 风险表已登记 macOS）")
    finally:
        worker.close()


def test_worker_unknown_tool(tmp_path):
    worker = _worker(tmp_path, {"rank.py": LEGAL_TOOL})
    try:
        with pytest.raises(G.GuardViolation) as exc:
            worker.call("nope", obs={}, candidates=[], memory=[])
        assert exc.value.code == E.E_ACTION_TOOL_SIG
    finally:
        worker.close()


def test_worker_tool_error_is_fail_soft(tmp_path):
    body = ("def rank_actions(obs, candidates, memory):\n"
            "    raise ValueError('boom')\n")
    worker = _worker(tmp_path, {"boom.py": body})
    try:
        with pytest.raises(G.GuardViolation) as exc:
            worker.call("rank_actions", obs={}, candidates=[], memory=[])
        assert exc.value.code == E.E_ACTION_TOOL_FAIL
        assert worker.alive # 工具体异常不重启 worker
    finally:
        worker.close()


def test_worker_wrong_return_type(tmp_path):
    body = ("def rank_actions(obs, candidates, memory):\n"
            "    return {'not': 'a list'}\n")
    worker = _worker(tmp_path, {"bad.py": body})
    try:
        with pytest.raises(G.GuardViolation) as exc:
            worker.call("rank_actions", obs={}, candidates=[], memory=[])
        assert exc.value.code in (E.E_ACTION_TOOL_SIG, E.E_ENV_AST_DENIED)
    finally:
        worker.close()


def test_worker_context_manager_and_close(tmp_path):
    harness = tmp_path / "harness"
    (harness / "tools").mkdir(parents=True, exist_ok=True)
    (harness / "tools" / "rank.py").write_text(LEGAL_TOOL, encoding="utf-8")
    with G.load_harness_tools(harness) as worker:
        assert worker.alive
    assert not worker.alive
    worker.close() # 幂等


# ---- _worker.py 的进程内路径（子进程里无法被覆盖率统计，这里直接驱动）--------------

def _worker_harness(tmp_path: Path) -> Path:
    harness = tmp_path / "harness"
    (harness / "tools").mkdir(parents=True, exist_ok=True)
    (harness / "tools" / "rank.py").write_text(LEGAL_TOOL, encoding="utf-8")
    (harness / "tools" / "boom.py").write_text(
        "def explode(obs, candidates, memory):\n    raise ValueError('boom')\n",
        encoding="utf-8")
    (harness / "tools" / "sneaky.py").write_text(
        "def sneaky(obs, candidates, memory):\n    return [len(_HONEYPOT_LEDGER)]\n",
        encoding="utf-8")
    return harness


def test_worker_module_load_tools_ignores_sentinels(tmp_path):
    from dice_ratchet.sandbox import _worker

    tools = _worker._load_tools(_worker_harness(tmp_path),
                               {"import_allowlist": ["math"], "max_tools": 8})
    assert set(tools) == {"rank_actions", "explode", "sneaky"}
    assert tools["rank_actions"]({}, [{"kind": "end_turn"}], []) == [{"kind": "end_turn"}]


def test_worker_module_max_tools_cap(tmp_path):
    from dice_ratchet.sandbox import _worker

    tools = _worker._load_tools(_worker_harness(tmp_path),
                               {"import_allowlist": ["math"], "max_tools": 1})
    assert len(tools) == 1


def test_worker_main_protocol(tmp_path, monkeypatch, capsys):
    import io

    from dice_ratchet.sandbox import _worker

    monkeypatch.setattr(_worker, "_set_limits", lambda *a, **kw: None)
    options = {"harness_dir": str(_worker_harness(tmp_path)), "max_tools": 8,
               "mem_limit_mb": 256, "cpu_limit_s": 2,
               "import_allowlist": ["math"], "repo_root": str(Repo_root :=
                                                              Path(__file__).resolve().parents[3])}
    requests = [
        {"id": 1, "op": "list"},
        {"id": 2, "op": "call", "tool": "rank_actions", "obs": {},
         "candidates": [{"kind": "end_turn"}], "memory": []},
        {"id": 3, "op": "call", "tool": "explode", "obs": {},
         "candidates": [], "memory": []},
        {"id": 4, "op": "call", "tool": "ghost", "obs": {}, "candidates": [], "memory": []},
        {"id": 5, "op": "bogus"},
        {"id": 6, "op": "shutdown"},
    ]
    monkeypatch.setattr(sys, "stdin", io.StringIO(
        "\n".join(json.dumps(r) for r in requests) + "\n"))
    code = _worker.main(["prog", json.dumps(options)])
    out = capsys.readouterr().out.splitlines()
    replies = {json.loads(x)["id"]: json.loads(x) for x in out}
    assert code == 0
    assert replies[1]["tools"] == ["explode", "rank_actions", "sneaky"]
    assert replies[2]["ok"] is True
    assert replies[3]["error"]["code"] == E.E_ACTION_TOOL_FAIL
    assert replies[4]["error"]["code"] == E.E_ACTION_TOOL_SIG
    assert replies[5]["error"]["code"] == E.E_ENV_AST_DENIED
    assert replies[6]["ok"] is True


def test_worker_main_escape_exits_5(tmp_path, monkeypatch, capsys):
    """E_ENV_ESCAPE 是 P0：子进程回应错误后立即退出（父进程重启 + 卸载 harness）。"""
    import io

    from dice_ratchet.sandbox import _worker

    monkeypatch.setattr(_worker, "_set_limits", lambda *a, **kw: None)
    options = {"harness_dir": str(_worker_harness(tmp_path)), "max_tools": 8,
               "import_allowlist": ["math"],
               "repo_root": str(Path(__file__).resolve().parents[3])}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"id": 1, "op": "call", "tool": "sneaky", "obs": {},
         "candidates": [], "memory": []}) + "\n"))
    assert _worker.main(["prog", json.dumps(options)]) == 5
    reply = json.loads(capsys.readouterr().out.splitlines()[0])
    assert reply["error"]["code"] == E.E_ENV_ESCAPE


def test_worker_main_load_failure_exits_4(tmp_path, monkeypatch, capsys):
    from dice_ratchet.sandbox import _worker

    monkeypatch.setattr(_worker, "_set_limits", lambda *a, **kw: None)
    harness = tmp_path / "harness"
    (harness / "tools").mkdir(parents=True)
    (harness / "tools" / "broken.py").write_text("def f(:\n", encoding="utf-8")
    code = _worker.main(["prog", json.dumps({"harness_dir": str(harness), "max_tools": 4,
                                             "repo_root": str(
                                                 Path(__file__).resolve().parents[3])})])
    assert code == 4


def test_worker_module_respond_and_set_limits_are_safe(capsys):
    from dice_ratchet.sandbox import _worker

    _worker._respond({"id": 1, "ok": True})
    assert json.loads(capsys.readouterr().out)["ok"] is True
