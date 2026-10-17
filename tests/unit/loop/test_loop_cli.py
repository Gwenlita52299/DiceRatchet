"""MOD-06 CLI 单测：``loop run|status|smoke`` 的参数面、装配与错误路径。

集成用例（``tests/integration/test_loop_closure.py``）用子进程跑真实 CLI；
这里**进程内**调用，覆盖 CLI 自身的分支（覆盖门 G1 要求 ``dice_ratchet/loop/**`` ≥ 75%）。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from dice_ratchet.infra import cli as infra_cli
from dice_ratchet.infra import events
from dice_ratchet.infra.config import LoopConfig, load_config
from dice_ratchet.infra.errors import E, InfraError
from dice_ratchet.loop import cli as loop_cli
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
    monkeypatch.setenv("DICERATCHET_INFRA__RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("DICERATCHET_ENV__EPISODE__DIFFICULTY", "easy")
    monkeypatch.setenv("DICERATCHET_ENV__EPISODE__STAGE_COUNT", "3")
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    from dice_ratchet.app import main

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
                                      ("loop", "smoke"), ("loop", "baseline")}
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
    # ``--harness-dir`` 指向仓库外的拷贝：F47-A 的起跑前门禁在非 git 路径下降级跳过，
    # 使用例只验「种子分片校验」，不依赖宿主工作区是否干净（门禁另有用例直接覆盖）。
    code, _out, err = _run(tmp_path, monkeypatch,
                           ["loop", "run", "--agent", "heuristic",
                            "--harness-dir", _tmp_harness_copy(tmp_path),
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
    from dice_ratchet.app import _inference_client

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
# F47-A：演化起跑前的 git 门禁（CLI 边界）
# =====================================================================

def _tmp_harness_copy(tmp_path: Path) -> str:
    """把仓库 ``harness/`` 拷到 ``tmp_path``（git worktree 之外）并返回路径字符串。

    与 ``tests/integration/test_loop_closure.py::_isolated_harness`` 同一套绝缘理由：
    F47-A 之后，会演化的 ``loop run`` 起跑前要求工作区干净；指向仓库外的拷贝可让
    「种子分片」「错误码」等**与被测行为无关**的用例不依赖宿主工作区状态。
    """
    dst = tmp_path / "harness"
    if not dst.is_dir():
        shutil.copytree(REPO_ROOT / "harness", dst)
    return str(dst)


def _init_git_repo(path: Path) -> Path:
    """建一个干净的 git 仓库（只有一个初始提交）。"""
    path.mkdir(parents=True, exist_ok=True)
    for cmd in (["init", "-q"], ["config", "user.email", "t@example.com"],
                ["config", "user.name", "Tester"],
                ["config", "commit.gpgsign", "false"]):
        subprocess.run(["git", "-C", str(path), *cmd], check=True, capture_output=True)
    (path / "README.md").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"],
                   check=True, capture_output=True)
    return path


def _harness_in(repo: Path) -> Path:
    harness = repo / "harness"
    (harness / "prompts").mkdir(parents=True, exist_ok=True)
    (harness / "prompts" / "decision.md").write_text("v0\n", encoding="utf-8")
    (harness / "version.json").write_text(json.dumps(
        {"version": 0, "parent": None, "git_commit": "", "artifact_hash": "",
         "created_at": "2026-10-06T00:00:00Z", "reason": "t", "accepted_by": "human",
         "base_model": None}), encoding="utf-8")
    # 提交 harness/：否则它自己是 `??`，会让「干净」用例先天为脏
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "harness"],
                   check=True, capture_output=True)
    return harness


def test_cli_run_aborts_on_dirty_worktree_before_any_spend(tmp_path, monkeypatch):
    """F47-A：脏工作区必须在**任何推理调用与 gen-000 基线之前**被拒（退出码 4）。

    原先同一判据只长在 MOD-07 ``commit_harness`` 内部，即整代跑完、即将落版本时——
    实测代价 3.75 h / ≈¥47，且被记成 ``decision="reject"``（看起来像合法拒绝）。
    """
    repo = _init_git_repo(tmp_path / "outer")
    harness = _harness_in(repo)
    (repo / "工具产物.txt").write_text("x\n", encoding="utf-8") # 未跟踪 → 脏

    calls: list[tuple] = []

    class _CountingClient:
        def generate_json(self, *args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("门禁未生效：发生了推理调用")

    loop_cli.set_inference_client_provider(
        lambda cfg, *, run_id, runs_root: _CountingClient())
    loop_cli.set_evolve_provider(lambda cfg, runner, run_ctx: (lambda state, ctx: {}))
    try:
        code, _out, err = _run(tmp_path, monkeypatch,
                               ["loop", "run", "--agent", "llm", "--model", "mock",
                                "--no-reflect", "--harness-dir", str(harness),
                                "--seeds", "train:1000-1002", "--k", "3",
                                "--run-id", "u-dirty"])
    finally:
        loop_cli.set_inference_client_provider(_real_client_provider())
        loop_cli.set_evolve_provider(None)

    assert code == 4, err
    assert str(E.E_IO_GIT_DIRTY) in err
    assert calls == [], "门禁必须先于任何推理调用"
    assert not (tmp_path / "runs" / "u-dirty" / "generations" / "gen-000").exists(), \
        "被拒的 run 不得先付 gen-000 基线"


def test_cli_smoke_is_not_gated_by_worktree(tmp_path, monkeypatch):
    """范围界定：门禁只保护「会落版本」的 run ⇒ ``loop smoke``（--no-evolve）不拦。

    否则本地带未提交改动时连零成本诊断都做不了。语义已写进 MOD-06 PRD。
    """
    repo = _init_git_repo(tmp_path / "outer2")
    harness = _harness_in(repo)
    (repo / "工具产物.txt").write_text("x\n", encoding="utf-8") # 同样脏

    code, out, err = _run(tmp_path, monkeypatch,
                          ["loop", "smoke", "--no-reflect", "--harness-dir", str(harness),
                           "--seeds", "train:1000-1002", "--k", "3", "--run-id", "u-dirty-smoke"])
    assert code == 0, err
    assert json.loads(out)["completed"] == 3


def test_ensure_evolution_worktree_clean_helper(tmp_path):
    """F47-A 助手：干净 → True；脏 → 抛；非 git → False（降级不阻断）。"""
    import types

    from dice_ratchet.loop.generation import ensure_evolution_worktree_clean

    repo = _init_git_repo(tmp_path / "repo3")
    harness = _harness_in(repo)
    repo_ctx = types.SimpleNamespace(harness_dir=str(harness))
    assert ensure_evolution_worktree_clean(repo_ctx) is True

    (repo / "dirty.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(InfraError) as exc:
        ensure_evolution_worktree_clean(repo_ctx)
    assert exc.value.code == E.E_IO_GIT_DIRTY

    plain = tmp_path / "plain"
    (plain / "harness").mkdir(parents=True)
    assert ensure_evolution_worktree_clean(
        types.SimpleNamespace(harness_dir=str(plain / "harness"))) is False


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


# =====================================================================
# F55：只读命令必须能定位「配置面不同」的 run
# =====================================================================

def test_f55_readonly_command_reads_run_with_different_max_generations(tmp_path, monkeypatch):
    """给定一个用 ``--generations 2`` 建的 run（``loop.max_generations=2`` 进 config_hash）；

    当 ``rehearsal check``（**只读**、没有 ``--generations`` 这个 CLI 面，算出 3）去读它；

    则**不得**抛 ``E_IO_RUN_CONFLICT``，而要照常产出 6 项判据报告。

    **现场（2026-10-12 彩排，实测）**：`eaf6f18ce6e7865a != 9403576ed90e79ba` —— 彩排 run 是
    `loop run --generations 2` 建的，而 `scripts/run_m3_rehearsal.sh` 结尾调
    `rehearsal check --run-id m3-rehearsal`（同一组 env、无 `--generations`）⇒ 必然被
    config_hash 硬门拒。后果：**彩排永远不可能「合格」**（最后一关无论如何都失败），
    而这不是彩排的问题、是只读路径的问题。
    """
    run_id = "readonly-target"
    # ① 建一个 max_generations=2 的 run（只读命令的 CLI 面里没有这个参数）
    code, out, err = _run(tmp_path, monkeypatch,
                          ["loop", "run", "--generations", "2", "--no-evolve",
                           "--no-baseline", "--agent", "heuristic", "--k", "1",
                           "--run-id", run_id])
    run_dir = tmp_path / "runs" / run_id
    recorded = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))["config_hash"]

    # ② 只读命令用默认配置（max_generations=3）读它 —— 修复前这里就是 E_IO_RUN_CONFLICT
    code2, out2, err2 = _run(tmp_path, monkeypatch,
                             ["rehearsal", "check", "--run-id", run_id])
    assert "E_IO_RUN_CONFLICT" not in err2, err2
    assert code2 in (0, 1), (code2, err2)   # 0=合格 / 1=不合格，都算「读到了」
    payload = json.loads(out2)
    assert {c["check"] for c in payload["checks"]} >= {
        "mechanical_failures_zero", "plots_have_data_points",
        "ledger_has_both_splits", "acceptance_judge_runs",
        "arm_archive_present", "one_shot_resources_untouched"}

    # ③ 反向护栏：**写侧**（--resume）仍然必须硬校验 config_hash
    code3, _out3, err3 = _run(tmp_path, monkeypatch,
                              ["loop", "run", "--resume", "--run-id", run_id,
                               "--agent", "heuristic", "--k", "1"])
    assert code3 != 0 and "E_IO_RUN_CONFLICT" in err3, (code3, err3)
    assert recorded  # 起跑时的 hash 非空（上面第 ② 步的对照基准）
