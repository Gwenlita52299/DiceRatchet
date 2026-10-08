"""M1 验收 §13.2 的长跑/续跑/单局超时守门用例（F18 / F20 / F5）。

对应缺陷：

- **F18**：``run_eval`` 只在整批结束后落盘一次 → 数小时长跑被中断即全部作废；
- **F20**：``evals.seed_timeout_s`` 传下去后**从不被消费** → 一个卡住的 worker 让 eval 永久挂起；
- **F5**：``load_episode_results`` 的 ``arm`` 形参只打标签不过滤 → 基线局混进代际报告分母
  （实测 ``heldout.n=120``、``win_rate=0.30``）。
"""

from __future__ import annotations

import concurrent.futures
import json
import shutil
import time
from pathlib import Path

import pytest

from dice_ratchet.evals import harness as H
from dice_ratchet.evals.baselines import run_baseline
from dice_ratchet.evals.harness import (EvalSpec, SandboxRunner, load_episode_results, run_eval,
                                     write_episode_summaries)
from dice_ratchet.evals.metrics import EpisodeResult, aggregate
from dice_ratchet.infra.config import load_config
from dice_ratchet.infra.errors import E, InfraError

_REPO = Path(__file__).resolve().parents[3]
_CONFIG_DIR = _REPO / "configs"


def _result(seed: int, *, arm: str = "", outcome: str = "win",
            dropped: bool = False) -> EpisodeResult:
    return EpisodeResult(seed=seed, split="heldout", generation=0, harness_version=0,
                         outcome=outcome, truncation_reason=None, stages_cleared=3,
                         hearts_left=1, battles_won=5, decisions=20, steps=40,
                         illegal_attempts=0, fallback_decisions=0,
                         kind_counts=tuple([1] * 16), slot_counts={}, seq_hash=f"s{seed}",
                         dropped=dropped, arm=arm)


class _StubAgent:
    """最小逐 env 步 agent（``_agent_loop`` 路径）。"""

    name = "stub"
    degraded = False

    def reset(self, seed: int) -> None:  # noqa: D102
        self.seed = seed

    def act(self, obs, legal, hints=None):  # noqa: D102
        return dict(legal[0])


# =====================================================================
# F20：单局预算真的被消费
# =====================================================================

def test_runner_consumes_the_seed_timeout_budget():
    """``SandboxRunner`` 必须消费 ``timeout_s``：超预算的局记 dropped + ``E_EVAL_TIMEOUT``。"""
    cfg = load_config(config_dir=_CONFIG_DIR)
    runner = SandboxRunner(agent_factory=lambda seed, arm: _StubAgent(), config=cfg)
    result = runner.run_episode(1000, split="train", arm="f20", generation=0,
                               harness_version=0, timeout_s=0.001)
    assert result.dropped is True
    assert result.error_code == E.E_EVAL_TIMEOUT
    assert result.win is False # Spec §8.5：dropped 计 n 并按 lose 计
    metrics = aggregate([result], split="train", basket="H3")
    assert metrics.n == 1 and metrics.n_dropped == 1


def test_policy_loop_also_consumes_the_budget():
    """决策点循环（``fixed_prompt`` 这类 ``PolicyAgent``）同样受单局预算约束。"""
    from dice_ratchet.evals.baselines import build_agent

    class _NeverCalled:
        model_id = "stub"

        def generate_json(self, *a, **kw):  # pragma: no cover - 预算先到期
            raise AssertionError("预算到期前不应发生推理")

    cfg = load_config(config_dir=_CONFIG_DIR)
    runner = SandboxRunner(
        agent_factory=lambda seed, arm: build_agent("fixed_prompt", seed=seed,
                                                    client=_NeverCalled(), config=cfg),
        config=cfg)
    result = runner.run_episode(1000, split="train", arm="f20", generation=0,
                               harness_version=0, timeout_s=0.001)
    assert result.dropped is True and result.error_code == E.E_EVAL_TIMEOUT


class _HangingRunner:
    """故意无视 ``timeout_s`` 的 runner（F20 的第二道闸：硬看门狗）。"""

    def __init__(self, delay: float) -> None:
        self.delay = float(delay)
        self.seen: list[int] = []

    def run_episode(self, seed: int, **kw) -> EpisodeResult:  # noqa: D102
        self.seen.append(int(seed))
        time.sleep(self.delay) # 比硬看门狗的上限还久，但会自己结束（不留悬挂线程）
        return _result(seed)


def test_watchdog_abandons_a_hung_worker_and_keeps_going(monkeypatch):
    """硬看门狗：一个 worker 无视预算时，本 run 仍必须结束（且其余 seed 不被饿死）。"""
    monkeypatch.setattr(H, "_TIMEOUT_SLACK_S", 0.2)
    monkeypatch.setattr(H, "_WATCHDOG_POLL_S", 0.05)
    spec = EvalSpec(run_id="r-f20wd", generation=0, harness_version=0, split="train",
                    basket=(1000, 1001), seeds=(1000, 1001), n_workers=1,
                    seed_timeout_s=0, arm="wd")
    runner = _HangingRunner(delay=2.0)
    started = time.monotonic()
    result = run_eval(spec, runner=runner)
    elapsed = time.monotonic() - started
    assert elapsed < 1.6, f"看门狗没生效：eval 花了 {elapsed:.2f}s"
    assert len(result.results) == 2
    assert all(r.dropped and r.error_code == E.E_EVAL_TIMEOUT for r in result.results)
    assert str(E.E_EVAL_TIMEOUT) in result.error_codes
    assert result.metrics.n == 2 and result.metrics.n_dropped == 2
    # 被放弃的 worker 用一个新池补回来，第二个 seed 才不会永远排不上队
    assert runner.seen == [1000, 1001]


def test_watchdog_does_not_trip_for_a_normal_run():
    """反向用例：预算充足时不得误报 dropped。"""
    spec = EvalSpec(run_id="r-f20ok", generation=0, harness_version=0, split="train",
                    basket=(1000,), seeds=(1000,), seed_timeout_s=60, arm="ok")
    result = run_eval(spec, runner=_HangingRunner(delay=0.01))
    assert result.results[0].dropped is False
    assert result.results[0].outcome == "win"
    assert result.error_codes == ()


# =====================================================================
# F18：逐局增量落盘 + --resume
# =====================================================================

def test_results_are_persisted_episode_by_episode(tmp_path):
    """``on_result`` 必须**每完成一局**就落一次盘（不是整批结束后才写）。"""
    spec = EvalSpec(run_id="r-f18", generation=0, harness_version=0, split="train",
                    basket=(1000, 1001, 1002), seeds=(1000, 1001, 1002), arm="inc")
    written: list[int] = []
    seen_at_write: list[int] = []

    def on_result(result: EpisodeResult) -> None:
        path = write_episode_summaries(tmp_path, [result], split="train", generation=0,
                                       arm="inc")[0]
        written.append(result.seed)
        seen_at_write.append(len(list((tmp_path / "episodes" / "train").glob("*.jsonl"))))
        assert path.is_file()

    result = run_eval(spec, runner=_HangingRunner(delay=0.0), on_result=on_result)
    assert written == [1000, 1001, 1002] # 每局都回调，且顺序 = 完成顺序
    assert len(load_episode_results(tmp_path, "train", arm="inc")) == 3
    assert len(result.results) == 3


def test_resume_skips_completed_seeds_and_keeps_the_old_results(tmp_path):
    """F18：``resume=True`` 只跑缺的 seed，并把已完成的局并入同一次聚合。"""
    cfg = load_config(config_dir=_CONFIG_DIR)
    first = run_baseline("heuristic", split="train", basket=(1000, 1001, 1002),
                         run_id="r-f18", run_dir=tmp_path, config=cfg,
                         protocol=None, n_workers=1, archive=False)
    assert first.metrics.n == 3
    files = sorted((tmp_path / "episodes" / "train").glob("*.jsonl"))
    assert len(files) == 3
    # 删掉一局，模拟「跑到一半崩了」
    files[-1].unlink()
    second = run_baseline("heuristic", split="train", basket=(1000, 1001, 1002),
                          run_id="r-f18", run_dir=tmp_path, config=cfg, protocol=None,
                          n_workers=1, archive=False, resume=True)
    assert second.resumed_n == 2
    assert second.pending_seeds == (1002,)
    assert second.metrics.n == 3 # 已完成的 2 局 + 新跑的 1 局
    assert sorted(r.seed for r in second.results) == [1000, 1001, 1002]


def test_resume_with_nothing_left_does_not_archive_again(tmp_path):
    cfg = load_config(config_dir=_CONFIG_DIR)
    run_baseline("heuristic", split="train", basket=(1000,), run_id="r-f18b",
                 run_dir=tmp_path, config=cfg, protocol=None, archive=True)
    first = sorted((tmp_path / "baselines").glob("heuristic-train*.json"))
    assert len(first) == 1
    arm = run_baseline("heuristic", split="train", basket=(1000,), run_id="r-f18b",
                       run_dir=tmp_path, config=cfg, protocol=None, archive=True,
                       resume=True)
    assert arm.pending_seeds == () and arm.resumed_n == 1
    assert arm.archive_path == ""
    assert len(sorted((tmp_path / "baselines").glob("heuristic-train*.json"))) == 1


# =====================================================================
# F5：arm 真过滤
# =====================================================================

def _with_fixed_path(cfg, path) -> object:
    from dataclasses import replace

    return replace(cfg, agent=replace(cfg.agent,
                                       policy=replace(cfg.agent.policy,
                                                      fixed_prompt_path=str(path))))


def _write(tmp_path: Path, arm: str, seeds: list[int]) -> None:
    write_episode_summaries(tmp_path, [_result(s, arm=arm) for s in seeds],
                            split="heldout", generation=0, arm=arm)


def test_load_episode_results_filters_by_arm(tmp_path):
    _write(tmp_path, "parent", [2000])
    _write(tmp_path, "candidates", [2001])
    _write(tmp_path, "baseline-heuristic", [2002])
    _write(tmp_path, "baseline-random", [2003])
    everything = load_episode_results(tmp_path, "heldout")
    assert [r.seed for r in everything] == [2000, 2001, 2002, 2003]
    assert all(r.arm for r in everything) # 行里的 arm 被带出来（修复前恒为空串）

    only_parent = load_episode_results(tmp_path, "heldout", arm="parent")
    assert [r.seed for r in only_parent] == [2000]
    assert [r.seed for r in load_episode_results(tmp_path, "heldout",
                                                 arm=["parent", "candidates"])] == [2000, 2001]
    without_baselines = load_episode_results(
        tmp_path, "heldout", exclude_arms=("baseline-heuristic", "baseline-random"))
    assert [r.seed for r in without_baselines] == [2000, 2001]


def test_eval_report_excludes_baseline_arms(tmp_path):
    """F5 的端到端形态：基线局不得进入代际 held-out 分母。"""
    from dice_ratchet.evals import cli as cli_mod

    _write(tmp_path, "parent", [2000, 2001])
    _write(tmp_path, "baseline-heuristic", [2002, 2003])
    results = load_episode_results(tmp_path, "heldout",
                                   exclude_arms=cli_mod._BASELINE_ARMS)
    assert [r.seed for r in results] == [2000, 2001]
    metrics = aggregate(results, split="heldout", basket="H1")
    assert metrics.n == 2 # 修复前会是 4（真实 run 里是 120）


def test_progress_events_carry_wall_and_cpu_time(monkeypatch, tmp_path):
    """F19 的可观测性：``eval.seed_done`` 同时带墙钟与 CPU 时间（休眠可被发现）。"""
    captured: list[dict] = []

    def fake_emit(event: str, **fields) -> None:
        captured.append({"event": event, **fields})

    monkeypatch.setattr(H, "_emit", fake_emit)
    spec = EvalSpec(run_id="r-f19", generation=0, harness_version=0, split="train",
                    basket=(1000,), seeds=(1000,), arm="obs")
    run_eval(spec, runner=_HangingRunner(delay=0.0))
    done = [row for row in captured if row["event"] == "eval.seed_done"]
    assert done and "dur_s" in done[0] and "cpu_s" in done[0] and "episode_s" in done[0]
    assert done[0]["decisions_llm"] == 0 # 逐 env 步 agent 不产生决策点级推理


def test_abandoned_pools_are_not_joined(monkeypatch):
    """放弃 worker 后不得 ``shutdown(wait=True)``，否则挂起线程会把进程一起拖住。"""
    monkeypatch.setattr(H, "_TIMEOUT_SLACK_S", 0.1)
    monkeypatch.setattr(H, "_WATCHDOG_POLL_S", 0.02)
    waits: list[bool] = []
    original = concurrent.futures.ThreadPoolExecutor

    class _Recording(original):  # type: ignore[misc, valid-type]
        def shutdown(self, wait: bool = True, **kw) -> None:  # noqa: D102
            waits.append(bool(wait))
            return super().shutdown(wait=wait, **kw)

    monkeypatch.setattr(H.concurrent.futures, "ThreadPoolExecutor", _Recording)
    spec = EvalSpec(run_id="r-f20join", generation=0, harness_version=0, split="train",
                    basket=(1000,), seeds=(1000,), n_workers=1, seed_timeout_s=0, arm="j")
    run_eval(spec, runner=_HangingRunner(delay=0.6))
    assert waits and False in waits


def _cleanup(run_id: str) -> None:
    shutil.rmtree(_REPO / "runs" / run_id, ignore_errors=True)


@pytest.fixture(autouse=True)
def _no_leftover_runs():
    yield
    for run_id in ("r-f18", "r-f18b", "r-f18c", "r-f18d"):
        _cleanup(run_id)


# =====================================================================
# F18 的配套闸：不同「测量协议」的局不得混进同一份聚合
# =====================================================================

def test_episode_rows_carry_the_measurement_protocol(tmp_path):
    from dice_ratchet.evals.harness import EPISODE_PROTOCOL, read_episode_protocols

    _write(tmp_path, "baseline-heuristic", [2000])
    assert read_episode_protocols(tmp_path, "heldout",
                                  arm="baseline-heuristic") == {EPISODE_PROTOCOL}
    # 旧版（F22 修复前）的局没有 protocol 字段 → 以 "" 出现
    legacy = tmp_path / "episodes" / "heldout" / "e-002001-g000-baseline-heuristic.jsonl"
    legacy.write_text(json.dumps({"type": "episode_end", "seed": 2001, "arm":
                                  "baseline-heuristic", "generation": 0}) + "\n",
                      encoding="utf-8")
    assert read_episode_protocols(tmp_path, "heldout", arm="baseline-heuristic") == \
        {EPISODE_PROTOCOL, ""}


def test_resume_refuses_to_mix_measurement_protocols(tmp_path):
    """跨代码改动续跑必须被拦住（否则「逐 env 步」与「逐决策点」的局会被拼在一起）。"""
    cfg = load_config(config_dir=_CONFIG_DIR)
    run_baseline("heuristic", split="train", basket=(1000,), run_id="r-f18c",
                 run_dir=tmp_path, config=cfg, protocol=None, archive=False)
    path = next((tmp_path / "episodes" / "train").glob("*.jsonl"))
    path.write_text(path.read_text(encoding="utf-8").replace(
        '"protocol": "episode-protocol-v2"', '"protocol": "episode-protocol-v1"'),
        encoding="utf-8")


def test_baseline_protocol_binds_the_frozen_template(tmp_path):
    """F23：固定提示词模板换过就不许续跑（一轮里改过三次模板，这条闸拦的就是它）。"""
    from dice_ratchet.evals.baselines import baseline_protocol

    cfg = load_config(config_dir=_CONFIG_DIR)
    frozen = tmp_path / "frozen.md"
    frozen.write_text("观测:\n{{observation}}\n记忆:\n{{memory}}\n", encoding="utf-8")
    other = tmp_path / "other.md"
    other.write_text("观测:\n{{observation}}\n记忆:\n{{memory}}\n（改过）\n", encoding="utf-8")
    assert baseline_protocol("heuristic", config=cfg) == "episode-protocol-v2"
    base = baseline_protocol("fixed_prompt", config=cfg)
    assert base.startswith("episode-protocol-v2+")
    a = baseline_protocol("fixed_prompt", config=_with_fixed_path(cfg, frozen))
    b = baseline_protocol("fixed_prompt", config=_with_fixed_path(cfg, other))
    assert a != b and a != base


def test_resume_refuses_when_the_frozen_template_changed(tmp_path):
    """已落盘的局用了模板 A，现在换成模板 B 再 --resume → 必须拒绝（不许混算）。"""
    from dice_ratchet.agent.prompts.assembler import sha256_of

    cfg = load_config(config_dir=_CONFIG_DIR)
    a = tmp_path / "a.md"
    a.write_text("观测:\n{{observation}}\n记忆:\n{{memory}}\n", encoding="utf-8")
    b = tmp_path / "b.md"
    b.write_text("观测:\n{{observation}}\n记忆:\n{{memory}}\n（改过）\n", encoding="utf-8")
    proto_a = "episode-protocol-v2+" + sha256_of(a.read_text(encoding="utf-8"))[7:23]
    _write_protocol_row(tmp_path, 2000, "baseline-fixed_prompt", proto_a)
    with pytest.raises(InfraError) as exc:
        run_baseline("fixed_prompt", split="heldout", basket=(2000,), run_id="r-f18e",
                     run_dir=tmp_path, config=_with_fixed_path(cfg, b), protocol=None,
                     client=_NeverCalledClient(), archive=False, resume=True)
    assert exc.value.code == E.E_EVAL_ARM_NOT_ALLOWED
    assert "测量协议" in str(exc.value)


class _NeverCalledClient:
    """模板闸必须在任何推理之前触发。"""

    model_id = "never"

    def generate_json(self, *a, **kw):  # pragma: no cover - 不该被调用
        raise AssertionError("测量协议不一致时不应发生任何推理")


def _write_protocol_row(tmp_path: Path, seed: int, arm: str, protocol: str) -> None:
    root = tmp_path / "episodes" / "heldout"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"e-{seed:06d}-g000-{arm}.jsonl").write_text(
        json.dumps({"type": "episode_end", "seed": seed, "arm": arm, "generation": 0,
                    "protocol": protocol}) + "\n", encoding="utf-8")


def test_same_protocol_rows_are_accepted(tmp_path):
    """反向：协议一致时必须放行（闸不能空转）。"""
    from dice_ratchet.evals.harness import EPISODE_PROTOCOL

    _write_protocol_row(tmp_path, 2000, "baseline-fixed_prompt", EPISODE_PROTOCOL)
    from dice_ratchet.evals.harness import read_episode_protocols

    assert read_episode_protocols(tmp_path, "heldout", generation=0,
                                  arm="baseline-fixed_prompt") == {EPISODE_PROTOCOL}


def test_resume_accepts_same_protocol_and_skips_everything(tmp_path):
    cfg = load_config(config_dir=_CONFIG_DIR)
    run_baseline("heuristic", split="train", basket=(1000,), run_id="r-f18d",
                 run_dir=tmp_path, config=cfg, protocol=None, archive=False)
    arm = run_baseline("heuristic", split="train", basket=(1000,), run_id="r-f18d",
                       run_dir=tmp_path, config=cfg, protocol=None, archive=False,
                       resume=True)
    assert arm.pending_seeds == () and arm.metrics.n == 1
