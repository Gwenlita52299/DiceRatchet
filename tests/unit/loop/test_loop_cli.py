"""MOD-06 CLI 单测：``loop run|status|smoke`` 的参数面、装配与错误路径。

集成用例（``tests/integration/test_loop_closure.py``）用子进程跑真实 CLI；
这里**进程内**调用，覆盖 CLI 自身的分支（覆盖门 G1 要求 ``gamer_rsi/loop/**`` ≥ 75%）。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path

import pytest

from gamer_rsi.infra import cli as infra_cli
from gamer_rsi.infra import events
from gamer_rsi.infra.config import LoopConfig, load_config
from gamer_rsi.infra.errors import E, InfraError
from gamer_rsi.loop import cli as loop_cli
from loop_support import REPO_ROOT


@pytest.fixture(autouse=True)
def _clean_events():
    events.unbind()
    yield
    events.unbind()


def _run(tmp_path: Path, monkeypatch, argv: list[str], *,
         env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """进程内跑组合根 CLI（不 re-exec），runs 根指向 ``tmp_path``。"""
    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setenv("GAMERSI_INFRA__RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("GAMERSI_ENV__EPISODE__DIFFICULTY", "easy")
    monkeypatch.setenv("GAMERSI_ENV__EPISODE__STAGE_COUNT", "3")
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    from gamer_rsi.app import main

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv, reexec=False)
    return code, out.getvalue(), err.getvalue()


def _state(tmp_path: Path, run_id: str) -> dict:
    return json.loads((tmp_path / "runs" / run_id / "state.json").read_text(encoding="utf-8"))


# =====================================================================
# 命令注册与参数面
# =====================================================================

def test_loop_handlers_registered():
    assert set(loop_cli.HANDLERS) == {("loop", "run"), ("loop", "status"),
                                      ("loop", "smoke")}
    for path in loop_cli.HANDLERS:
        parser = infra_cli.build_parser()
        args = parser.parse_args(list(path))
        assert args.command_path == list(path)


def test_smoke_parser_defaults_are_model_free():
    """``loop smoke`` 的默认值必须是免模型路径（NFR-06-6）。"""
    parser = infra_cli.build_parser()
    args = parser.parse_args(["loop", "smoke"])
    assert args.agent == "heuristic"
    assert args.no_reflect is True
    assert args.no_evolve is True
    assert args.k == 3 and args.generations == 1


def test_run_parser_exposes_loop_overrides():
    parser = infra_cli.build_parser()
    args = parser.parse_args(["loop", "run", "--k", "7", "--generations", "2", "--hitl",
                              "--agent", "heuristic", "--model", "mock",
                              "--no-reflect", "--no-baseline", "--force-rerun"])
    assert (args.k, args.generations, args.hitl) == (7, 2, True)
    assert args.no_reflect is True and args.baseline is False
    assert args.agent == "heuristic" and args.model == "mock"


def test_loop_options_mapping():
    cfg = load_config()
    args = argparse.Namespace(no_reflect=True, hitl=True, k=5, generations=2)
    options = loop_cli._loop_options(args, cfg)
    assert options == {"reflect_enabled": False, "hitl": True, "k": 5,
                       "max_generations": 2}
    assert loop_cli._loop_options(argparse.Namespace(no_reflect=False, hitl=None, k=None,
                                                     generations=None), cfg) == {}


def test_loop_options_maps_retry_reflections():
    """``--retry-reflections`` → ``loop.retry_reflections_on_resume``（FR-06-4 的补偿开关）。"""
    cfg = load_config()
    options = loop_cli._loop_options(
        argparse.Namespace(no_reflect=False, hitl=None, k=None, generations=None,
                           retry_reflections=True), cfg)
    assert options == {"retry_reflections_on_resume": True}


# =====================================================================
# smoke / status 端到端（进程内）
# =====================================================================

def test_cli_smoke_and_status_in_process(tmp_path, monkeypatch):
    code, out, err = _run(tmp_path, monkeypatch,
                          ["loop", "smoke", "--seeds", "train:1000-1002", "--k", "3",
                           "--no-reflect", "--run-id", "u-smoke"])
    assert code == 0, err
    summary = json.loads(out)
    assert summary["phase"] == "done" and summary["completed"] == 3
    state = _state(tmp_path, "u-smoke")
    assert state["counters"]["episodes_total"] == 3
    assert state["counters"]["reflections_skipped"] == 3
    assert (tmp_path / "runs" / "u-smoke" / "episodes" / "train").is_dir()

    code, out, err = _run(tmp_path, monkeypatch,
                          ["loop", "status", "--run-id", "u-smoke"])
    assert code == 0, err
    payload = json.loads(out)
    assert payload["completed"] == 3 and payload["pending"] == 0
    assert payload["run_id"] == "u-smoke" and payload["phase"] == "done"


def test_cli_status_requires_run_id(tmp_path, monkeypatch):
    code, _out, err = _run(tmp_path, monkeypatch, ["loop", "status"])
    assert code == 2
    assert str(E.E_IO_CONFIG_INVALID) in err


def test_cli_status_missing_run(tmp_path, monkeypatch):
    code, _out, err = _run(tmp_path, monkeypatch, ["loop", "status", "--run-id", "nope"])
    assert code == 2 and str(E.E_IO_CONFIG_INVALID) in err


def test_cli_status_missing_state(tmp_path, monkeypatch):
    (tmp_path / "runs" / "empty").mkdir(parents=True)
    code, _out, err = _run(tmp_path, monkeypatch, ["loop", "status", "--run-id", "empty"])
    assert code == 2 and str(E.E_IO_STATE_MISSING) in err


def test_cli_smoke_resume_is_noop(tmp_path, monkeypatch):
    base = ["loop", "smoke", "--seeds", "train:1000-1002", "--k", "3", "--no-reflect",
            "--run-id", "u-resume"]
    assert _run(tmp_path, monkeypatch, base)[0] == 0
    before = (tmp_path / "runs" / "u-resume" / "metrics" / "scoreboard.json").read_text()
    code, out, err = _run(tmp_path, monkeypatch, [*base, "--resume"])
    assert code == 0, err
    assert json.loads(out)["phase"] == "done"
    after = (tmp_path / "runs" / "u-resume" / "metrics" / "scoreboard.json").read_text()
    assert before == after, "续跑必须幂等，不重复计分"


# =====================================================================
# 错误路径（退出码与错误码）
# =====================================================================

def test_cli_rejects_heldout_seeds(tmp_path, monkeypatch):
    code, _out, err = _run(tmp_path, monkeypatch,
                           ["loop", "run", "--agent", "heuristic",
                            "--seeds", "heldout:2000-2001", "--k", "3"])
    assert code == 4, err
    assert str(E.E_EVAL_SPLIT_VIOLATION) in err


def test_cli_llm_agent_without_client(tmp_path, monkeypatch):
    """``--agent llm`` 但 MOD-03 客户端不可用 → 退出码 3（不静默降级）。"""
    def broken(cfg, *, run_id, runs_root):
        raise InfraError(E.E_LLM_LOAD_FAILED, "权重缺失")
        # 上面一行之后不会执行：provider 抛错即 fail-closed

    loop_cli.set_inference_client_provider(broken)
    try:
        code, _out, err = _run(tmp_path, monkeypatch,
                               ["loop", "run", "--agent", "llm", "--seeds",
                                "train:1000-1002", "--k", "3", "--no-baseline",
                                "--run-id", "u-llm"])
    finally:
        loop_cli.set_inference_client_provider(_real_client_provider())
    assert code == 3, err
    assert str(E.E_LLM_LOAD_FAILED) in err


def test_cli_llm_agent_without_provider(tmp_path, monkeypatch):
    loop_cli.set_inference_client_provider(None)
    try:
        code, _out, err = _run(tmp_path, monkeypatch,
                               ["loop", "run", "--agent", "llm", "--seeds",
                                "train:1000-1002", "--k", "3", "--no-baseline",
                                "--run-id", "u-noclient"])
    finally:
        loop_cli.set_inference_client_provider(_real_client_provider())
    assert code == 3, err
    assert str(E.E_LLM_UNAVAILABLE) in err


def _real_client_provider():
    from gamer_rsi.app import _inference_client

    return _inference_client


def test_cli_resume_k_mismatch(tmp_path, monkeypatch):
    base = ["loop", "smoke", "--seeds", "train:1000-1002", "--k", "3", "--no-reflect",
            "--run-id", "u-k"]
    assert _run(tmp_path, monkeypatch, base)[0] == 0
    code, _out, err = _run(tmp_path, monkeypatch,
                           ["loop", "smoke", "--run-id", "u-k", "--resume", "--k", "5"])
    # ``--k`` 变化会让周期幂等键漂移 → 拒绝（Spec F10 + 本模块的额外一致性校验）
    assert code == 2, err
    assert str(E.E_IO_RUN_CONFLICT) in err or str(E.E_IO_CONFIG_INVALID) in err


# =====================================================================
# 装配助手
# =====================================================================

def test_guard_concurrency_rejects(tmp_path):
    class _Cfg:
        loop = LoopConfig(train_concurrency=2)

    with pytest.raises(InfraError) as excinfo:
        loop_cli._guard_concurrency(_Cfg())
    assert excinfo.value.code is E.E_IO_CONCURRENCY


def test_resolve_seeds_defaults_to_train_blocks(tmp_path, monkeypatch):
    monkeypatch.chdir(REPO_ROOT)
    cfg = LoopConfig(k=3, max_generations=2)
    seeds = loop_cli._resolve_seeds(argparse.Namespace(seeds=None), cfg, resume=False)
    assert seeds == list(range(1000, 1030)) + list(range(1030, 1060))


def test_resolve_seeds_resume_uses_state(tmp_path, monkeypatch):
    monkeypatch.chdir(REPO_ROOT)
    assert loop_cli._resolve_seeds(argparse.Namespace(seeds=None), LoopConfig(),
                                   resume=True) is None
