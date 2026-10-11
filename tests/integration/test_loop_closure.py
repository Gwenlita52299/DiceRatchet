"""MOD-06 闭环集成（PRD §11 的 T-06-14…T-06-17；M2 验收的闭环证据）。

真实环境 + 真实 CLI（``python -m dice_ratchet loop …``），只用启发式 agent，
**不调模型**。为了让冒烟秒级完成，run 级 override 走 Spec §8.6 B-30 的环境变量写法：

    DICERATCHET_ENV__EPISODE__DIFFICULTY=easy DICERATCHET_ENV__EPISODE__STAGE_COUNT=3

``DICERATCHET_ENV__EPISODE__*`` 参与 ``config_hash``（F10），所以同一 run 的续跑必须带同一组
环境变量（本文件的 ``_ENV`` 统一注入）。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

#: 冒烟口径（Spec §8.6 bring-up 的同源写法）：easy × 3 关，秒级完成
_ENV = {
    "DICERATCHET_ENV__EPISODE__DIFFICULTY": "easy",
    "DICERATCHET_ENV__EPISODE__STAGE_COUNT": "3",
}

from dice_ratchet.evals.seeds import coerce_protocol, validate_protocol
_H1_START = validate_protocol(coerce_protocol(None)).splits["heldout"].baskets["H1"].start
_H1_SEEDS = (_H1_START, _H1_START + 1, _H1_START + 2)


def _run_cli(tmp_path: Path, argv: list[str], *, timeout: float = 300.0
             ) -> tuple[int, str, str]:
    """在 ``tmp_path`` 里跑真实 CLI（不 re-exec），返回 (exit_code, stdout, stderr)。"""
    env = {**os.environ, **_ENV, "DICERATCHET_INFRA__RUNS_ROOT": str(tmp_path / "runs")}
    proc = subprocess.run([sys.executable, "-m", "dice_ratchet", *argv], cwd=_REPO,
                          env=env, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def _run_dir(tmp_path: Path, run_id: str) -> Path:
    return tmp_path / "runs" / run_id


def _isolated_harness(tmp_path: Path) -> Path:
    """把仓库 ``harness/`` 拷到 ``tmp_path``（**git worktree 之外**）并返回路径。

    F47-A 之后，会演化的 ``loop run`` 在起跑前要求 git 工作区干净；而本文件的用例
    跑真实子进程、注册真实 MOD-07 provider，若沿用仓库内的 ``harness/``，就会让
    「宿主工作区是否干净」决定用例成败（本地带未提交改动即失败）。指向仓库外的拷贝后，
    起跑前门禁按 Spec §8.5「非 git 路径 → 降级跳过」处理，用例回到只验编排契约。
    门禁本身由 ``tests/unit/loop/test_loop_cli.py`` 与
    ``tests/unit/infra/test_git_ops.py`` 直接覆盖。
    """
    dst = tmp_path / "harness"
    if not dst.is_dir():
        shutil.copytree(_REPO / "harness", dst)
    return dst


def _state(tmp_path: Path, run_id: str) -> dict:
    return json.loads((_run_dir(tmp_path, run_id) / "state.json").read_text(encoding="utf-8"))


def _keys(tmp_path: Path, run_id: str) -> list[str]:
    path = _run_dir(tmp_path, run_id) / "metrics" / "scoreboard.json"
    if not path.is_file():
        return []
    return [row["episode_key"] for row in
            json.loads(path.read_text(encoding="utf-8"))["episodes"]]


def _run_with_hook(tmp_path: Path, hook_source: str, run_id: str,
                   argv: list[str]) -> subprocess.CompletedProcess[str]:
    """把 ``hook_source``（注册 MOD-07 provider 的代码）注入进程后跑真实 CLI。

    ``python -m dice_ratchet`` 没有通用插件入口，所以用 ``-c`` 先 exec 注入再调
    ``dice_ratchet.app.main``——与被 ``wire()`` 注册的正式路径完全一致。
    """
    env = {**os.environ, **_ENV, "DICERATCHET_INFRA__RUNS_ROOT": str(tmp_path / "runs")}
    full = [*argv, "--run-id", run_id]
    script = (
        f"exec(compile({hook_source!r}, '<evolve-hook>', 'exec'));"
        "import sys;"
        f"sys.argv = ['dice_ratchet', *{full!r}];"
        "from dice_ratchet.app import main;"
        "raise SystemExit(main(sys.argv[1:], reexec=False))")
    proc = subprocess.run([sys.executable, "-c", script], cwd=_REPO, env=env,
                          capture_output=True, text=True, timeout=300)
    assert (_run_dir(tmp_path, run_id)).is_dir(), (
        f"注入 hook 的子进程没有产出 run 目录\nrc={proc.returncode}\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}\nruns_root="
        f"{env.get('DICERATCHET_INFRA__RUNS_ROOT')}")
    return proc


def _events(tmp_path: Path, run_id: str) -> list[dict]:
    path = _run_dir(tmp_path, run_id) / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


# =====================================================================
# T-06-14 闭环冒烟（G6）
# =====================================================================

def test_t06_14_smoke_completes_and_writes_three_episodes(tmp_path):
    """给定 ``loop smoke --seeds train:1000-1002 --k 3 --no-reflect``；当执行；

    则退出码 0、``state.json`` 为 ``phase=="done"``、产出 3 个 ``episodes/*.jsonl``。

    时长口径：NFR-06-6 的「< 2 s 跑完 3 局」针对**假依赖**路径（真实环境 step 的耗时
    不属于编排开销，见 NFR-06-3 的 ≤5 ms/步）。这里对真实环境给的是
    「3 局 easy×3 关（≈100 步/局）在 30 s 内完成」的上界，实测见
    ``docs/prd/MOD-06-loop-PRD.md`` 的落地记录。
    """
    started = time.perf_counter()
    code, out, err = _run_cli(tmp_path, ["loop", "smoke",
                                         "--seeds", "train:1000-1002", "--k", "3",
                                         "--no-reflect", "--run-id", "t14"])
    duration = time.perf_counter() - started
    assert code == 0, f"stdout={out}\nstderr={err}"
    assert duration < 30.0, f"真实环境冒烟退化：{duration:.2f}s"

    run_id = "t14"
    state = _state(tmp_path, run_id)
    assert state["phase"] == "done"
    assert state["completed_train_seeds"] == [1000, 1001, 1002]
    assert state["tick"] == 3
    assert state["generation"] == 0
    assert state["counters"]["episodes_total"] == 3
    assert state["counters"]["reflections_skipped"] == 3      # --no-reflect

    train_dir = _run_dir(tmp_path, run_id) / "episodes" / "train"
    files = sorted(p.name for p in train_dir.glob("*.jsonl"))
    assert files == ["e-001000-g000-train.jsonl", "e-001001-g000-train.jsonl",
                     "e-001002-g000-train.jsonl"]
    # 每局都以 episode_end 收尾（且只有一条）
    for name in files:
        rows = [json.loads(line) for line in
                (train_dir / name).read_text(encoding="utf-8").splitlines() if line.strip()]
        ends = [r for r in rows if r.get("type") == "episode_end"]
        assert len(ends) == 1 and ends[0]["outcome"] in ("win", "lose")
    # 计分幂等：3 个 episode_key，全唯一
    keys = _keys(tmp_path, run_id)
    assert len(keys) == len(set(keys)) == 3


def test_t06_14_smoke_is_resumable_noop(tmp_path):
    """冒烟跑完后 ``--resume`` 必须是幂等的 no-op（不重复计分、不重跑）。"""
    base = ["loop", "smoke", "--seeds", "train:1000-1002", "--k", "3", "--no-reflect",
            "--run-id", "t14b"]
    code, out, err = _run_cli(tmp_path, base)
    assert code == 0, err
    before = _keys(tmp_path, "t14b")
    code, out, err = _run_cli(tmp_path, [*base, "--resume"])
    assert code == 0, err
    assert _keys(tmp_path, "t14b") == before
    assert _state(tmp_path, "t14b")["tick"] == 3


def test_t06_14_loop_status_reads_state(tmp_path):
    """``loop status`` 只读地打印 state.json（G6 第二条命令）。"""
    code, _out, err = _run_cli(tmp_path, ["loop", "smoke", "--seeds", "train:1000-1002",
                                          "--k", "3", "--no-reflect", "--run-id", "t14c"])
    assert code == 0, err
    code, out, err = _run_cli(tmp_path, ["loop", "status", "--run-id", "t14c"])
    assert code == 0, err
    payload = json.loads(out)
    assert payload["phase"] == "done"
    assert payload["completed"] == 3
    assert payload["pending"] == 0


def test_t06_14_status_missing_run_is_config_error(tmp_path):
    """未知 run → 配置类失败（退出码 2），不是 traceback。"""
    code, _out, err = _run_cli(tmp_path, ["loop", "status", "--run-id", "nope"])
    assert code == 2
    assert "E_IO_CONFIG_INVALID" in err


# =====================================================================
# T-06-15 固定 seed 确定性（G2/G3）
# =====================================================================

def _trajectory_fingerprint(root: Path) -> list[str]:
    """``obs_digest+action+reward`` 序列（与并发度、run_id 无关的字节级指纹）。"""
    lines: list[str] = []
    for path in sorted((root / "episodes" / "train").glob("*.jsonl")):
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            row = json.loads(raw)
            if row.get("type") == "episode_end":
                continue
            lines.append(json.dumps([path.name, row["step"], row["obs_digest"],
                                     row["action"], row["reward"]],
                                    ensure_ascii=False, sort_keys=True))
    return lines


def test_t06_15_same_seeds_are_byte_identical(tmp_path):
    """给定 ``--seeds train:1000-1004 --agent heuristic``；当跑两次（不同 run_id）；

    则两次 ``obs_digest+action+reward`` 序列逐字节相同。
    """
    for run_id in ("t15a", "t15b"):
        code, _out, err = _run_cli(tmp_path, [
            "loop", "run", "--agent", "heuristic", "--seeds", "train:1000-1004",
            "--k", "99", "--generations", "1", "--no-reflect", "--no-evolve",
            "--run-id", run_id])
        assert code == 0, err
    first = _trajectory_fingerprint(_run_dir(tmp_path, "t15a"))
    second = _trajectory_fingerprint(_run_dir(tmp_path, "t15b"))
    assert first, "没读到任何轨迹行"
    assert first == second, "同一 seed 清单 + 同 harness_version 必须逐字节可复现"


# =====================================================================
# T-06-16 演化一轮（编排层；真实 MOD-07 + 本地小模型见 M2 验收记录）
# =====================================================================

def test_t06_16_cycle_boundary_writes_gen001_and_events(tmp_path):
    """给定 ``--k 3 --generations 1`` 与注入的 MOD-07 stub；当执行；

    则 ``state.generation == 1``、``generations/gen-001/report.json`` 存在、
    ``events.jsonl`` 含 ``loop.cycle_start``。

    真实「本地小模型 + MOD-07」的一轮演化（M2 验收）不在本用例范围内：MOD-07 尚未落地
    （排期 M3），本用例用 MOD-07 stub 覆盖 MOD-06 一侧的**编排契约**（FR-06-5/6/7）。
    """
    stub = """
from dice_ratchet.loop.cli import set_evolve_provider
from dice_ratchet.loop.generation import EvolveOutcome
from dice_ratchet.infra.io import write_json_atomic
from dice_ratchet.infra import events
import pathlib


def provider(cfg, runner, run_ctx):
    def evolve(state, ctx):
        gen = int(state.generation) + 1
        write_json_atomic(pathlib.Path(ctx.paths.generations) / f"gen-{gen:03d}"
                          / "report.json",
                          {"generation": gen, "decision": "reject",
                           "decided_by": "auto", "reason": "stub"})
        events.emit("evolve.decision", gen=gen, decision="reject", decided_by="auto")
        return EvolveOutcome(decision="reject", generation=gen)
    return evolve


set_evolve_provider(provider)
"""
    proc = _run_with_hook(tmp_path, stub, "t16", [
        "loop", "run", "--agent", "heuristic", "--seeds", "train:1000-1002",
        "--k", "3", "--generations", "1", "--no-reflect", "--no-baseline",
        "--harness-dir", str(_isolated_harness(tmp_path))])
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"

    state = _state(tmp_path, "t16")
    assert state["generation"] == 1
    assert state["last_completed_cycle"] == 1
    report = _run_dir(tmp_path, "t16") / "generations" / "gen-001" / "report.json"
    assert report.is_file(), "周期边界必须产出 gen-001/report.json"

    names = {row["event"] for row in _events(tmp_path, "t16")}
    assert "loop.cycle_start" in names, "FR-06-13：每代开始写 loop.cycle_start"
    assert "evolve.decision" in names, "MOD-07 的裁决事件必须落到同一个 events.jsonl"
    keys = _keys(tmp_path, "t16")
    assert len(keys) == len(set(keys)) == 3


def test_t06_16_evolve_error_is_recorded_as_error_reject(tmp_path):
    """MOD-07 抛 ``E_EVOLVE_*`` → ``evolve.decision{decision:error_reject}`` 且 run 继续。"""
    stub = """
from dice_ratchet.loop.cli import set_evolve_provider
from dice_ratchet.infra.errors import E, InfraError


def provider(cfg, runner, run_ctx):
    def evolve(state, ctx):
        raise InfraError(E.E_EVOLVE_JUDGE_FAILED, "stub 裁判失败")
    return evolve


set_evolve_provider(provider)
"""
    proc = _run_with_hook(tmp_path, stub, "t16b", [
        "loop", "run", "--agent", "heuristic", "--seeds", "train:1000-1002",
        "--k", "3", "--generations", "1", "--no-reflect", "--no-baseline",
        "--harness-dir", str(_isolated_harness(tmp_path))])
    assert proc.returncode == 0, proc.stderr
    state = _state(tmp_path, "t16b")
    assert state["generation"] == 1
    assert state["phase"] == "done"
    decisions = [row for row in _events(tmp_path, "t16b")
                 if row["event"] == "evolve.decision"]
    assert decisions and decisions[-1]["decision"] == "error_reject"


# =====================================================================
# T-06-17 断电模拟
# =====================================================================

@pytest.mark.slow
def test_t06_17_sigkill_then_resume_loses_no_completed_seed(tmp_path):
    """给定在 ``state.json`` 原子替换前后注入 ``kill -9``；当 ``--resume``；

    则状态可解析、无重复计分、无已完成 seed 丢失。
    """
    run_id = "t17"
    seeds = [str(s) for s in range(1000, 1010)]
    env = {**os.environ, **_ENV, "DICERATCHET_INFRA__RUNS_ROOT": str(tmp_path / "runs")}
    argv = [sys.executable, "-m", "dice_ratchet", "loop", "run", "--agent", "heuristic",
            "--seeds", f"train:{seeds[0]}-{seeds[-1]}", "--k", "99", "--generations", "1",
            "--no-reflect", "--no-evolve", "--run-id", run_id]
    proc = subprocess.Popen(argv, cwd=_REPO, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    state_path = _run_dir(tmp_path, run_id) / "state.json"
    deadline = time.time() + 60
    completed_before: list[int] = []
    while time.time() < deadline:
        if state_path.is_file():
            try:
                completed_before = json.loads(state_path.read_text(encoding="utf-8"))[
                    "completed_train_seeds"]
            except (json.JSONDecodeError, KeyError):
                completed_before = []
            if len(completed_before) >= 2:
                break
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=30)
    assert completed_before, "没能在 kill 前观察到任何已完成 seed"

    # state.json 必须仍可解析（NFR-06-5：原子替换，任意时刻 SIGKILL 都不自相矛盾）
    state = _state(tmp_path, run_id)
    assert state["completed_train_seeds"][:len(completed_before)] == completed_before
    keys_before = _keys(tmp_path, run_id)
    assert set(keys_before) == {f"e-{s:06d}-g000-train" for s in completed_before}
    assert len(keys_before) == len(set(keys_before))

    # ---- 续跑跑完 -------------------------------------------------------
    code, out, err = _run_cli(tmp_path, [
        "loop", "run", "--agent", "heuristic", "--seeds", f"train:{seeds[0]}-{seeds[-1]}",
        "--k", "99", "--generations", "1", "--no-reflect", "--no-evolve",
        "--run-id", run_id, "--resume"])
    assert code == 0, f"stdout={out}\nstderr={err}"
    final = _state(tmp_path, run_id)
    assert final["phase"] == "done"
    assert final["completed_train_seeds"] == list(range(1000, 1010))
    keys_after = _keys(tmp_path, run_id)
    assert len(keys_after) == len(set(keys_after)) == 10, "不得重复计分"
    assert set(completed_before) <= {int(k.split("-")[1]) for k in keys_after}


# =====================================================================
# NFR-06-3：编排层单步开销（不含推理与环境 step）
# =====================================================================

def test_nfr06_3_orchestration_overhead_per_step(tmp_path):
    """假依赖下编排层的单步开销（落盘 + 记账）应远低于 5 ms（NFR-06-3）。"""
    from fakes import FakeEnv, FakePolicy                     # noqa: PLC0415
    from loop_support import build_runner                     # noqa: PLC0415


    from dice_ratchet.loop.state import init_state               # noqa: PLC0415

    env = FakeEnv(steps_to_terminate=200)
    policy = FakePolicy()
    state = init_state("r-perf", k=999, seeds=[1000], harness_version=0)
    runner, ctx = build_runner(tmp_path, policy=policy, env=env, state=state,
                               loop_cfg=_perf_cfg(), seeds=(1000,))
    started = time.perf_counter()
    rec = runner.run_episode(1000, split="train", arm="train", harness_version=0)
    per_step_ms = (time.perf_counter() - started) * 1000.0 / max(1, rec.steps)
    assert rec.steps == 200
    assert per_step_ms < 5.0, f"编排层单步 {per_step_ms:.2f} ms 超预算（NFR-06-3）"


def _perf_cfg():
    from dice_ratchet.infra.config import LoopConfig

    return LoopConfig(k=999, reflect_enabled=False, heldout_n=4, smoke_n=2, n_candidates=1)


# =====================================================================
# FR-06-13a：gen-000 基线（harness v0 在 H1 上的评估）
# =====================================================================

def test_gen000_baseline_report_is_written(tmp_path, monkeypatch):
    """给定 harness v0 + 缩小的 heldout 篮子；当 ``write_gen000_baseline``；

    则 ``generations/gen-000/report.json`` 存在、``decision=="baseline"``、
    评估局轨迹落在 ``episodes/heldout/``（经 MOD-06 的 ``EvalRunnerAdapter``）。

    **本用例曾因一个真实缺陷而失败（2026-10-11 修）**：`write_gen000_baseline`
    调 `evaluate_generation` 时**没传 `run_dir`**，而 MOD-08 从 F48/F49 起会在该路径上
    落**按代**产物（`metrics/kind_distribution/gen-XXX.json`）⇒ `run_dir=None` 回落到
    **硬编码的 ``Path("runs")``**，于是真实 `runs/r-loop-test/` 被建出来。
    后果不止「位置错」：该目录一旦存在，F42-A 的**臂归档**
    （`_arm_archive_dir` 按 `runs_root/run_id` 推导且要求目录已存在）就生效
    ⇒ 第二次评估**复用归档、不再执行任何局**（`heldout.n=3` 仍成立，因为结果来自归档），
    而 `episodes/heldout/` 为空 ⇒ 本断言失败。**这不是 flaky**，是共享可写状态的确定性后果；
    修法是让生产代码传 `run_dir`，并**在此断言 `runs/` 未被污染**。
    """
    monkeypatch.chdir(_REPO)
    from fakes import FakeEnv, FakePolicy
    from dice_ratchet.infra.config import load_config
    from dice_ratchet.loop.generation import write_gen000_baseline
    from loop_support import build_runner

    cfg = load_config()
    runner, ctx = build_runner(tmp_path, policy=FakePolicy(),
                               env=FakeEnv(steps_to_terminate=3, outcome="win"),
                               loop_cfg=cfg.loop, seeds=())
    ctx.app_config = cfg
    ctx.config = cfg.loop
    runner.config = cfg.loop
    ctx.memory_version = 0

    report_path = write_gen000_baseline(ctx, harness_version=0,
                                        basket=_H1_SEEDS, runner=runner)
    assert report_path is not None and report_path.is_file()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["decision"] == "baseline"
    assert report["generation"] == 0
    assert report["heldout"]["n"] == 3
    assert (tmp_path / "r-loop-test" / "episodes" / "heldout").glob("*.jsonl")
    heldout = sorted(p.name for p in
                     (tmp_path / "r-loop-test" / "episodes" / "heldout").glob("*.jsonl"))
    assert heldout == [f"e-{s:06d}-g000-parent.jsonl" for s in _H1_SEEDS]
    # **反向护栏**：本次调用不得在真实 runs/ 下留下任何按代产物
    # （`run_dir` 漏传时 `kind_distribution/gen-000.json` 会落到那里）
    assert not (_REPO / "runs" / "r-loop-test" / "metrics" / "kind_distribution").exists(), (
        "write_gen000_baseline 必须把按代产物写进 run_ctx.paths.root，"
        "不得回落到硬编码的 Path('runs')")


def test_f54_gen000_baseline_uses_configured_basket(tmp_path, monkeypatch):
    """给定 ``evolve.heldout_basket="H3"``；当 ``write_gen000_baseline`` **不传 basket**；

    则评估局的文件名落在 **H3**（731080–731099）上，而不是写死的 H1。

    **本用例对应 2026-10-11 的彩排事故（F54）**：``write_gen000_baseline`` 的 ``basket``
    默认值是字面量 ``"H1"``，且生产调用方从不传它 ⇒ ``scripts/run_m3_rehearsal.sh``
    （声明只用 H3、且 D-M3-5 明令 H1/H2/test 一律不出现）实测**跑在 H1 上**：
    已落盘 ``e-731000…731004-g000-parent.jsonl``，代价 ≈¥5.1 API + 5 个**一次性** seed。
    这不是「文档没写清」，是「代码里没有那条路」——所以护栏必须落在**真实产物**上：
    断言的判据是**落盘文件名里的 seed**，不是「某个函数参数等于什么」。
    """
    monkeypatch.chdir(_REPO)
    from dataclasses import replace

    from fakes import FakeEnv, FakePolicy
    from dice_ratchet.infra.config import load_config
    from dice_ratchet.loop.generation import write_gen000_baseline
    from loop_support import build_runner

    h3 = validate_protocol(coerce_protocol(None)).splits["heldout"].baskets["H3"]
    h3_seeds = (h3.start, h3.start + 1, h3.start + 2)
    cfg = load_config()
    cfg = replace(cfg, evolve=replace(cfg.evolve, heldout_basket="H3"))
    runner, ctx = build_runner(tmp_path, policy=FakePolicy(),
                               env=FakeEnv(steps_to_terminate=3, outcome="win"),
                               loop_cfg=cfg.loop, seeds=())
    ctx.app_config = cfg
    ctx.config = cfg.loop
    runner.config = cfg.loop

    # **不传 basket**：走配置键（这正是修复前后的行为分叉点）
    report_path = write_gen000_baseline(ctx, harness_version=0, runner=runner)
    assert report_path is not None and report_path.is_file()
    heldout = sorted(p.name for p in
                     (tmp_path / "r-loop-test" / "episodes" / "heldout").glob("*.jsonl"))
    # H3 的前 3 个 seed —— 完整 20 局是配置给的，这里只证「篮子来自配置」
    assert h3_seeds[0] in [int(n.split("-")[1]) for n in heldout if n.startswith("e-")]
    assert not any(n.startswith("e-73100") for n in heldout), (
        f"H1（一次性资源）不得出现在彩排路径上；实际落盘 {heldout}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["heldout"].get("basket") == "H3", report["heldout"].get("basket")


def test_gen000_baseline_is_idempotent(tmp_path, monkeypatch):
    """已存在的 gen-000 report 不再重算（FR-06-13a 的幂等）。"""
    monkeypatch.chdir(_REPO)
    from fakes import FakeEnv, FakePolicy
    from dice_ratchet.infra.config import load_config
    from dice_ratchet.loop.generation import write_gen000_baseline
    from loop_support import build_runner

    cfg = load_config()
    runner, ctx = build_runner(tmp_path, policy=FakePolicy(), env=FakeEnv(),
                               loop_cfg=cfg.loop, seeds=())
    ctx.app_config = cfg
    target = tmp_path / "r-loop-test" / "generations" / "gen-000"
    target.mkdir(parents=True, exist_ok=True)
    (target / "report.json").write_text('{"decision": "baseline"}', encoding="utf-8")
    assert write_gen000_baseline(ctx, basket=(_H1_START,), runner=runner) == target / "report.json"
    # 没有触发任何评估局（篮子被忽略、文件未重写）
    assert json.loads((target / "report.json").read_text(encoding="utf-8")) == {
        "decision": "baseline"}


# =====================================================================
# FR-06-11：HITL 挂起 → 人工 approve → --resume 的端到端演示
# =====================================================================

def test_hitl_end_to_end_suspend_and_resume(tmp_path):
    """``--hitl`` → ``phase=hitl_wait`` + 退出码 0 → 切换 harness → ``--resume`` 跑完。

    这是 M2 验收要求的「HITL 挂起/恢复端到端演示记录」的可执行版本（PRD §12）。
    """
    stub = """
from dice_ratchet.loop.cli import set_evolve_provider
from dice_ratchet.loop.generation import EvolveOutcome
from dice_ratchet.infra.io import write_json_atomic
import pathlib


def provider(cfg, runner, run_ctx):
    def evolve(state, ctx):
        gen = int(state.generation) + 1
        write_json_atomic(pathlib.Path(ctx.paths.generations) / f"gen-{gen:03d}"
                          / "report.json",
                          {"generation": gen, "decision": "accept",
                           "decided_by": "human", "reason": "HITL 待批"})
        return EvolveOutcome(decision="accept", generation=gen, pending_human=True,
                             candidate_dir=f"generations/gen-{gen:03d}",
                             report="report.json", reason="HITL 待批")
    return evolve


set_evolve_provider(provider)
"""
    base = ["loop", "run", "--agent", "heuristic", "--seeds", "train:1000-1002",
            "--k", "3", "--generations", "1", "--no-reflect", "--no-baseline", "--hitl",
            "--harness-dir", str(_isolated_harness(tmp_path))]
    proc = _run_with_hook(tmp_path, stub, "t9", base)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    state = _state(tmp_path, "t9")
    assert state["phase"] == "hitl_wait"
    assert state["hitl_pending"]["cycle"] == 1
    assert state["hitl_pending"]["generation"] == 1
    assert state["generation"] == 1
    assert "已挂起等人工裁决" in proc.stderr

    # ---- 人工裁决：MOD-07 的 evolve approve 落 harness 新版本 ----------------
    harness_version = _run_dir(tmp_path, "t9") / "harness" / "version.json"
    version_file = _REPO / "harness" / "version.json"
    payload = json.loads(version_file.read_text(encoding="utf-8"))
    payload["version"] = int(payload.get("version", 0)) + 1
    if not harness_version.parent.is_dir():
        harness_version.parent.mkdir(parents=True, exist_ok=True)
    harness_version.write_text(json.dumps(payload), encoding="utf-8")

    # ---- --resume（同一组 override）清空 hitl_pending 并收尾 -----------------
    argv = [*base, "--resume"]
    proc = _run_with_hook(tmp_path, stub, "t9", argv)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    state = _state(tmp_path, "t9")
    assert state["hitl_pending"] is None
    assert state["phase"] == "done"
    keys = _keys(tmp_path, "t9")
    assert len(keys) == len(set(keys)) == 3
