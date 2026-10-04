"""M1 验收 F1/F3/F4 的回归守门用例（2026-10-04）。

覆盖三条曾经真实发生、且没有任何用例拦得住的缺陷：

- **F1**：``fixed_prompt`` 基线无路可通真实 LLM，且整局降级被静默当成真实基线入档
  （``degraded_n=0``、`win_rate` 与 heuristic 逐位相同）；
- **F3**：``audit leak`` 在**任何** MOD-08 评估 run 上都判 ``critical``（``eval.start``
  的 held-out seed 列表 + 微秒耗时数字被 seed 扫描误判），G4 质量门失效；
- **F4**：``play``（MOD-04 的 G6 命令）未接线，恒返回 ``E_IO_MODULE_PENDING``。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from gamer_rsi.evals.baselines import run_baseline
from gamer_rsi.evals.harness import SandboxRunner
from gamer_rsi.evals.metrics import aggregate
from gamer_rsi.infra.config import load_config
from gamer_rsi.infra.errors import E, InfraError
from gamer_rsi.sandbox.api import EpisodeConfig, make_default_env

_REPO = Path(__file__).resolve().parents[3]
_CONFIG_DIR = _REPO / "configs"


def _run_cli(*argv: str, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "gamer_rsi", *argv],
                          cwd=_REPO, capture_output=True, text=True, timeout=timeout)


@pytest.fixture
def run_id(request):
    """独立 run 目录 + 用后清理。"""
    name = f"f1f3f4-{request.node.name[:40].replace('[', '-').replace(']', '')}"
    root = _REPO / "runs" / name
    if root.exists():
        shutil.rmtree(root)
    yield name
    shutil.rmtree(root, ignore_errors=True)


# =====================================================================
# F1：fixed_prompt 必须真的有 LLM 客户端；降级不得静默
# =====================================================================

def test_fixed_prompt_baseline_refuses_to_start_without_a_client(tmp_path):
    """没有客户端时 fail-closed（退出码语义 = E_LLM_UNAVAILABLE），不得降级归档。"""
    cfg = load_config(config_dir=_CONFIG_DIR)
    with pytest.raises(InfraError) as exc:
        run_baseline("fixed_prompt", split="heldout", basket="H3", run_id="r-f1",
                     run_dir=tmp_path, config=cfg, client=None)
    assert exc.value.code == E.E_LLM_UNAVAILABLE
    assert not (tmp_path / "baselines").exists(), "fail-closed 时不得留下归档"


def test_agent_loop_propagates_the_degraded_flag():
    """``_agent_loop`` 必须把基线 agent 的 ``degraded`` 抬进 ``EpisodeResult``。

    这里用一个「每次都声明自己已降级」的最小 agent 驱动真实 ``SandboxEnv``：修复前
    ``degraded_n`` 恒为 0（M1 验收 F1 的根因）。
    """
    class _AlwaysDegraded:
        degraded = True

        def reset(self, seed: int) -> None:  # noqa: D102
            self.degraded = True

        def act(self, obs, legal, hints=None):  # noqa: D102
            return dict(legal[0])

    cfg = load_config(config_dir=_CONFIG_DIR)
    runner = SandboxRunner(agent_factory=lambda seed, arm: _AlwaysDegraded(), config=cfg)
    result = runner.run_episode(1000, split="train", arm="f1", generation=0,
                                harness_version=0)
    assert result.degraded is True
    metrics = aggregate([result], split="train", basket="H3")
    assert metrics.degraded_n == 1 and metrics.n == 0


# =====================================================================
# F3：评估 run 上的 audit leak 必须 clean（G4）
# =====================================================================

def test_eval_run_has_zero_leak_findings(run_id):
    """跑一遍基线 + 代际报告后，``audit leak`` 必须 ``clean`` / 退出码 0。

    修复前：``verdict=critical``、136 findings、退出码 2（``eval.start`` 的 held-out
    seed 列表 120 条 + ``duration_us``/``dur_s`` 假阳性 16 条）。
    """
    proc = _run_cli("baseline", "--kind", "heuristic", "--split", "heldout",
                    "--seeds", "2000,2001,2002", "--run-id", run_id)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = _run_cli("eval", "report", "--split", "heldout", "--baseline", "all",
                      "--run-id", run_id)
    assert report.returncode == 0, report.stdout + report.stderr
    audit = _run_cli("audit", "leak", "--run-id", run_id)
    assert audit.returncode == 0, audit.stdout + audit.stderr
    payload = json.loads((_REPO / "runs" / run_id / "audit" / "leak-leak.json")
                         .read_text(encoding="utf-8"))
    assert payload["verdict"] == "clean" and payload["findings"] == []


def test_content_leak_in_the_log_surface_is_still_caught(run_id):
    """反例：把 held-out seed 塞进**内容**字段，扫描必须照旧抓到（修复不能变成盲区）。"""
    proc = _run_cli("baseline", "--kind", "random", "--split", "heldout",
                    "--seeds", "2000,2001", "--run-id", run_id)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    events = _REPO / "runs" / run_id / "events.jsonl"
    row = json.loads(events.read_text(encoding="utf-8").splitlines()[-1])
    row["reason"] = "泄漏面：heldout seed 2037 出现在内容字段"
    with events.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    audit = _run_cli("audit", "leak", "--run-id", run_id)
    assert audit.returncode != 0
    payload = json.loads((_REPO / "runs" / run_id / "audit" / "leak-leak.json")
                         .read_text(encoding="utf-8"))
    assert payload["verdict"] == "critical"
    assert any(f["leak_class"] == "seed_leak" for f in payload["findings"])


# =====================================================================
# F1 + F4：fixed_prompt 走真实后端；play 命令可用
# =====================================================================

def test_fixed_prompt_baseline_actually_calls_the_backend(run_id):
    """``--model mock`` 时 fixed_prompt 必须走 LLM 路径（degraded_n=0、与 heuristic 不同）。

    修复前无任何路径能注入客户端，该命令只能静默产出 heuristic 的副本。
    """
    proc = _run_cli("baseline", "--kind", "fixed_prompt", "--split", "heldout",
                    "--seeds", "2000,2001,2002", "--model", "mock", "--run-id", run_id)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["inference_client"] is True and payload["model_id"] == "mock"
    payload_metrics = payload["metrics"]
    assert payload_metrics["degraded_n"] == 0 and payload_metrics["n"] == 3
    # mock 固定输出 end_turn：决策数应与启发式明显不同（证明不是同一份实现）
    assert payload_metrics["mean_decisions"] != pytest.approx(95.7, abs=5.0)


@pytest.mark.parametrize("agent", ["random", "heuristic"])
def test_play_runs_offline_agents(agent, run_id):
    """F4：``play`` 已接线，Spec §4.6 的 ``--agent random|heuristic`` 可直接执行。"""
    proc = _run_cli("play", "--agent", agent, "--seeds", "train:1000-1001",
                    "--run-id", run_id)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "agent=" in proc.stdout and "n=2" in proc.stdout


def test_play_llm_without_weights_fails_closed(run_id):
    """``play --agent llm`` 拿不到模型 → 退出码 3，而不是安静地跑一趟空测量。"""
    proc = _run_cli("play", "--agent", "llm", "--seeds", "train:1000-1001",
                    "--run-id", run_id)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert E.E_LLM_UNAVAILABLE in proc.stderr


def test_play_rejects_the_removed_fixed_alias(run_id):
    """``--agent fixed`` 这个非 Spec §4.6 取值必须被 argparse 拒绝（退出码 2）。"""
    proc = _run_cli("play", "--agent", "fixed", "--seeds", "train:1000-1000",
                    "--run-id", run_id)
    assert proc.returncode == 2
