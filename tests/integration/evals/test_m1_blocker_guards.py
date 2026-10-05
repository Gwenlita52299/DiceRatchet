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
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

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
    # F5/F6/F22：逐局摘要的 arm 必须是基线臂，且真的发生过决策点级推理
    files = sorted((_REPO / "runs" / run_id / "episodes" / "heldout").glob("*.jsonl"))
    assert len(files) == 3
    for path in files:
        row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert row["arm"] == "baseline-fixed_prompt"
        assert row["decisions_llm"] >= 1


# =====================================================================
# F22：fixed_prompt 的推理粒度 = 决策点（不是 env 步）
# =====================================================================

class _FirstLegalClient:
    """按 prompt 里 ``legal_actions`` 段取真实合法动作组 plan 的假客户端。

    模拟契约良好的模型：``player_turn`` 下最多挑 3 张骰子（骰子/槽位互不重复，避免自造
    非法组合）并**显式 ``end_turn``**（MOD-04 任务说明要求「打完自己想打的牌后必须显式
    end_turn」）；其它阶段 1 个动作。于是每个**决策点**（= 每回合或每个非战斗步）只调用
    一次模型，一次计划推进多个 env 步。
    """

    model_id = "first-legal"
    backend = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def generate_json(self, messages, schema, *, mode, sampling, seed):  # noqa: D102
        self.calls += 1
        text = "\n".join(str(getattr(m, "content", m)) for m in messages)
        phase = (re.search(r"phase=(\w+)", text) or [None, ""])[1]
        section = text
        if "## 当前可选动作" in text:
            section = text.split("## 当前可选动作", 1)[1].split("\n## ", 1)[0]
        actions = []
        for line in section.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and "kind" in item:
                actions.append(item)
        picks: list[dict] = []
        used: set = set()
        if phase == "player_turn":
            for item in actions:
                if item.get("kind") != "play_face":
                    continue
                if item.get("dieId") in used or item.get("slotId") in used:
                    continue
                used.update((item.get("dieId"), item.get("slotId")))
                picks.append(item)
                if len(picks) >= 3:
                    break
            end_turn = next((a for a in actions if a.get("kind") == "end_turn"), None)
            if picks and end_turn is not None:
                picks.append(end_turn)
        picks = picks or [a for a in actions if a.get("kind") != "play_face"][:1] \
            or actions[:1] or [{"kind": "end_turn"}]
        out = SimpleNamespace(text=json.dumps({"reasoning": "x", "plan": picks}),
                              usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5,
                                                    latency_ms=1),
                              attempts=1, prompt_digest="sha256:" + "0" * 64)
        return out


def test_fixed_prompt_baseline_infers_once_per_decision_point(tmp_path):
    """F22：一次推理推进**一个决策点**（多个 env 步），不是一次推理推进一个 env 步。

    修复前 MOD-08 自带逐 env 步的 ``FixedPromptAgent``：调用数 = env 步数（实测
    285/87、317/106、132/44 → 每决策点 **3.1** 次推理）。修复后调用数 = 决策点数，
    且 ``steps / calls ≈ 2.5``（一次计划覆盖多个 env 步）。
    """
    cfg = load_config(config_dir=_CONFIG_DIR)
    client = _FirstLegalClient()
    arm = run_baseline("fixed_prompt", split="train", basket=(1000,), run_id="r-f22",
                       run_dir=tmp_path, config=cfg, client=client, archive=False,
                       model_backend="mock")
    result = arm.results[0]
    assert result.degraded is False
    assert result.decisions_llm > 0, "决策点循环没被使用（decisions_llm 只在 _policy_loop 里计数）"
    # 每个决策点恰好一次推理（重试只会让调用数略多：实测 70 vs 65 / 122 vs 114）
    assert result.decisions_llm <= client.calls <= result.decisions_llm * 1.25, \
        f"推理 {client.calls} 次 vs 决策点 {result.decisions_llm} 个 → 粒度不对"
    assert result.decisions_llm >= result.decisions
    # 一次推理覆盖多个 env 步：这正是「逐 env 步 vs 逐决策点」的成本差（F22 的 3.1×）
    assert result.steps >= 2 * client.calls, \
        f"steps={result.steps} calls={client.calls} → 疑似逐 env 步推理"
    assert result.fallback_decisions == 0
    assert arm.model_hash == "first-legal" # F6：记实际加载的模型，而不是配置里的 model_id
    assert arm.model_backend == "mock"


@pytest.mark.parametrize("agent", ["random", "heuristic"])
def test_play_runs_offline_agents(agent, run_id):
    """F4：``play`` 已接线，Spec §4.6 的 ``--agent random|heuristic`` 可直接执行。"""
    proc = _run_cli("play", "--agent", agent, "--seeds", "train:1000-1001",
                    "--run-id", run_id)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "agent=" in proc.stdout and "n=2" in proc.stdout


def test_play_llm_without_weights_fails_closed(run_id):
    """``play --agent llm`` 拿不到模型 → 退出码 3，而不是安静地跑一趟空测量。

    用**阶段二 30B** 构造「模型不可用」：它的权重不在仓库里（`models/` 已 gitignore），
    `load()` 会在路径校验处立刻抛 `E_LLM_UNAVAILABLE`，因此本用例不受「8B 权重是否已落盘」
    影响——8B 一旦落盘，用 8B 反而会真的跑起来并 900 s 超时（2026-10-04 实测踩到）。
    """
    from gamer_rsi.inference.registry import load_registry

    registry = load_registry(str(_REPO / "configs" / "models.yaml"))
    spec = registry.spec("qwen3-30b-a3b-q4")
    if Path(spec.weights_path).exists():
        pytest.skip("阶段二权重已落盘，无法用 30B 构造「模型不可用」")
    proc = _run_cli("play", "--agent", "llm", "--model", "qwen3-30b-a3b-q4",
                    "--seeds", "train:1000-1000", "--run-id", run_id)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert E.E_LLM_UNAVAILABLE in proc.stderr


def test_play_rejects_the_removed_fixed_alias(run_id):
    """``--agent fixed`` 这个非 Spec §4.6 取值必须被 argparse 拒绝（退出码 2）。"""
    proc = _run_cli("play", "--agent", "fixed", "--seeds", "train:1000-1000",
                    "--run-id", run_id)
    assert proc.returncode == 2
