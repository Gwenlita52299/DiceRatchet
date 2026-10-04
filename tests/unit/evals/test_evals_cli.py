"""CLI 处理函数单测（G6 命令面的实现层；重活全部注入 fake，保持秒级）。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from gamer_rsi.infra.run import RunPaths
from gamer_rsi.evals import cli as cli_mod
from gamer_rsi.evals import seeds as S
from gamer_rsi.evals.ablation import AblationReport
from gamer_rsi.evals.harness import ArmResult, load_episode_results
from gamer_rsi.evals.metrics import EpisodeResult, aggregate

_REPO = Path(__file__).resolve().parents[3]
_FIXTURES = _REPO / "tests" / "fixtures" / "evals"


def _ns(**kw) -> argparse.Namespace:
    base = {"config_dir": "configs", "run_id": "r-cli", "resume": False,
            "split": "heldout", "basket": "H1", "baseline": "none", "generation": 0,
            "kind": "random", "arms": "no_reflection,no_self_edit",
            "unlock_once": True, "attestation": None, "dry_run": False,
            "transfer": False}
    base.update(kw)
    return argparse.Namespace(**base)


def _arm(name: str = "baseline-random") -> ArmResult:
    results = tuple(_result(2000 + i) for i in range(3))
    return ArmResult(arm=name, split="heldout", basket="H1", harness_version=0,
                     memory_version=0, run_id="r-cli", results=results,
                     metrics=aggregate(results, basket="H1"))


def _result(seed: int) -> EpisodeResult:
    counts = [0] * 16
    counts[0] = 2
    return EpisodeResult(seed=seed, split="heldout", generation=0, harness_version=0,
                         outcome="win", truncation_reason=None, stages_cleared=5,
                         hearts_left=1, battles_won=9, decisions=90, steps=200,
                         illegal_attempts=0, fallback_decisions=0,
                         kind_counts=tuple(counts), slot_counts={}, seq_hash=f"s{seed}",
                         decision_kinds=(0, 1, 2))


def _json_tail(text: str) -> dict:
    """``cmd_*`` 会先打印一行路径，再打印 JSON 段；取第一行 ``{`` 起的部分。"""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith("{"))
    return json.loads("\n".join(lines[start:]))


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """把 ``cli_init_run`` 换成临时 run 目录，避免建真实 runs/ 与事件绑定。"""
    from gamer_rsi.infra.config import load_config

    cfg = load_config()
    monkeypatch.setattr(cli_mod.infra_cli, "cli_init_run",
                        lambda args: (RunPaths(tmp_path), cfg, "r-cli"))
    return tmp_path, cfg


def test_cmd_baseline_prints_and_archives(wired, monkeypatch, capsys):
    tmp_path, _cfg = wired
    calls: dict[str, object] = {}

    def fake_run_baseline(kind, **kw):
        calls.update({"kind": kind, **kw})
        return _arm()

    monkeypatch.setattr(cli_mod, "run_baseline", fake_run_baseline)
    assert cli_mod.cmd_baseline(_ns(kind="heuristic")) == 0
    payload = _json_tail(capsys.readouterr().out)
    assert payload["kind"] == "heuristic" and "power_note" in payload
    assert calls["split"] == "heldout" and calls["basket"] == "H1"
    assert calls["run_dir"] == tmp_path


def test_cmd_baseline_rejects_unknown_kind(wired):
    from gamer_rsi.infra.errors import InfraError

    with pytest.raises(InfraError):
        cli_mod.cmd_baseline(_ns(kind="bogus"))


def test_cmd_eval_report_real_artifacts(wired, capsys):
    tmp_path, _cfg = wired
    # 复制 golden 夹具作为 heldout 轨迹
    src = _FIXTURES / "episodes" / "heldout"
    dst = tmp_path / "episodes" / "heldout"
    dst.mkdir(parents=True)
    for path in src.glob("*.jsonl"):
        (dst / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    # 基线归档存在时 --baseline all 不报缺失
    base_dir = tmp_path / "baselines"
    base_dir.mkdir()
    (base_dir / "heuristic-heldout.json").write_text(json.dumps(
        {"kind": "heuristic", "split": "heldout", "metrics": {"win_rate": 0.45}}),
        encoding="utf-8")
    assert cli_mod.cmd_eval_report(_ns(baseline="all")) == 0
    out = capsys.readouterr().out
    assert "report.json" in out and "sha256:" in out
    report = json.loads((tmp_path / "generations" / "gen-000" / "report.json")
                        .read_text(encoding="utf-8"))
    assert report["heldout"]["n"] == 4 # degraded 局被剔除
    ext = json.loads((tmp_path / "metrics" / "gen-000.ext.json").read_text(encoding="utf-8"))
    assert ext["baselines"]["heuristic-heldout"]["win_rate"] == 0.45
    assert (tmp_path / "metrics" / "generations.csv").is_file()
    assert (tmp_path / "metrics" / "plots_data" / "gen-000_entropy.json").is_file()


def test_cmd_eval_report_warns_when_baseline_missing(wired, capsys):
    tmp_path, _cfg = wired
    assert cli_mod.cmd_eval_report(_ns(baseline="all")) == 0
    assert "E_EVAL_BASELINE_MISSING" in capsys.readouterr().err


def test_cmd_eval_diversity_writes_ext(wired, capsys):
    tmp_path, _cfg = wired
    src = _FIXTURES / "episodes" / "heldout"
    dst = tmp_path / "episodes" / "heldout"
    dst.mkdir(parents=True)
    for path in src.glob("*.jsonl"):
        (dst / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    assert cli_mod.cmd_eval_diversity(_ns()) == 0
    payload = _json_tail(capsys.readouterr().out)
    assert payload["generation"] == 0 and payload["n"] == 4
    ext = json.loads((tmp_path / "metrics" / "gen-000.ext.json").read_text(encoding="utf-8"))
    assert "kind_distribution" in ext and "collapse_reasons" in ext


def test_cmd_eval_ablation_writes_report(wired, monkeypatch, capsys):
    tmp_path, _cfg = wired
    captured: dict[str, object] = {}

    def fake_run_ablation(generation, **kw):
        captured.update({"generation": generation, **kw})
        return AblationReport(generation=generation, basket=kw["split_basket"], alpha=0.05,
                              arms={"full": 0.5}, delta_full={"no_reflection": 0.1},
                              mcnemar_p={"no_reflection": 0.01},
                              holm={"no_reflection": {"threshold": 0.025,
                                                      "significant": True}},
                              attribution="reflection", n={"full": 40}, run_id="r-cli")

    monkeypatch.setattr(cli_mod, "run_ablation", fake_run_ablation)
    assert cli_mod.cmd_eval_ablation(_ns()) == 0
    payload = _json_tail(capsys.readouterr().out)
    assert payload["attribution"] == "reflection"
    assert captured["arms"][0] == "full" # full 自动补上


def test_cmd_eval_test_locked_without_flag(wired):
    from gamer_rsi.infra.errors import E, InfraError

    with pytest.raises(InfraError) as exc:
        cli_mod.cmd_eval_test(_ns(unlock_once=False))
    assert exc.value.code == E.E_EVAL_TEST_LOCKED


def test_cmd_eval_test_unlocks_and_writes_attestation(wired, monkeypatch, capsys):
    tmp_path, _cfg = wired
    monkeypatch.setattr(cli_mod, "evaluate_arm", lambda *a, **kw: _arm("llm-test"))
    att = tmp_path / "generations" / "gen-000" / "test_attestation.json"
    assert cli_mod.cmd_eval_test(_ns(attestation=str(att))) == 0
    assert att.is_file()
    payload = _json_tail(capsys.readouterr().out)
    assert payload["test"]["n"] == 3
    ext = json.loads((tmp_path / "metrics" / "gen-000.ext.json").read_text(encoding="utf-8"))
    assert ext["transfer"] is None and "attestation" in ext


def test_cmd_eval_test_transfer_batch_is_reported(wired, monkeypatch, capsys):
    tmp_path, _cfg = wired
    monkeypatch.setattr(cli_mod, "evaluate_arm", lambda *a, **kw: _arm("llm-test"))
    att = tmp_path / "generations" / "gen-000" / "test_attestation.json"
    assert cli_mod.cmd_eval_test(_ns(transfer=True, attestation=str(att))) == 0
    payload = _json_tail(capsys.readouterr().out)
    assert payload["transfer"]["episode"] == "medium×10"
    ext = json.loads((tmp_path / "metrics" / "gen-000.ext.json").read_text(encoding="utf-8"))
    assert ext["transfer"]["decision_path"] is False
    assert ext["bootstrap_seed"] > 0 and ext["seeds_hash"].startswith("sha256:")


def test_latest_generation_scans_generations_and_metrics(tmp_path):
    (tmp_path / "generations" / "gen-002").mkdir(parents=True)
    (tmp_path / "metrics").mkdir()
    (tmp_path / "metrics" / "gen-005.ext.json").write_text("{}", encoding="utf-8")
    (tmp_path / "generations" / "junk").mkdir()
    assert cli_mod._latest_generation(tmp_path) == 5
    assert cli_mod._latest_generation(tmp_path / "nope") == 0


def test_generation_of_and_basket_of():
    assert cli_mod._generation_of(_ns(generation=3), Path("/tmp")) == 3
    assert cli_mod._basket_of(_ns(split="test")) == "test_full"
    assert cli_mod._basket_of(_ns(split="train")) == "train_window"
    assert cli_mod._basket_of(_ns(split="heldout", basket="H2")) == "H2"
