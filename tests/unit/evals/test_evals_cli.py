"""CLI 处理函数单测（G6 命令面的实现层；重活全部注入 fake，保持秒级）。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from dice_ratchet.infra.run import RunPaths
from dice_ratchet.evals import cli as cli_mod
from dice_ratchet.evals import seeds as S
from dice_ratchet.evals.ablation import AblationReport
from dice_ratchet.evals.harness import ArmResult, load_episode_results
from dice_ratchet.evals.metrics import EpisodeResult, aggregate
from dice_ratchet.infra.plotting import PLOT_FILES

_REPO = Path(__file__).resolve().parents[3]
_FIXTURES = _REPO / "tests" / "fixtures" / "evals"


def _ns(**kw) -> argparse.Namespace:
    base = {"config_dir": "configs", "run_id": "r-cli", "resume": False,
            "split": "heldout", "basket": "H1", "baseline": "none", "generation": 0,
            "kind": "random", "arms": "no_reflection,no_self_edit",
            "unlock_once": True, "attestation": None, "dry_run": False,
            "transfer": False, "bootstrap_B": None, "check": False}
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
    from dice_ratchet.infra.config import load_config

    cfg = load_config()
    monkeypatch.setattr(cli_mod.infra_cli, "cli_init_run",
                        lambda args, **kw: (RunPaths(tmp_path), cfg, "r-cli"))
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
    # F6：归档溯源字段必须真的传下去（后端名 / 模型 id / 单局预算）
    assert calls["model_hash"] == "qwen3-8b-q4"
    assert calls["model_backend"] in ("mlx", "mock", "unknown")
    assert calls["seed_timeout_s"] == _cfg.evals.seed_timeout_s
    assert payload["seeds"] == [2000, 2001, 2002] and payload["resumed_n"] == 0


def test_cmd_baseline_resume_does_not_require_state_json(wired, monkeypatch):
    """F18：``baseline --resume`` 只要求 run 目录 + episodes/，不要求 state.json。"""
    tmp_path, cfg = wired
    seen: dict[str, object] = {}

    def fake_init(args, **kw):
        seen["require_state"] = kw.get("require_state")
        return RunPaths(tmp_path), cfg, "r-cli"

    def fake_run_baseline(kind, **kw):
        seen["resume"] = kw.get("resume")
        return _arm()

    monkeypatch.setattr(cli_mod.infra_cli, "cli_init_run", fake_init)
    monkeypatch.setattr(cli_mod, "run_baseline", fake_run_baseline)
    assert cli_mod.cmd_baseline(_ns(kind="heuristic", resume=True)) == 0
    assert seen["require_state"] is False and seen["resume"] is True


def test_cmd_baseline_rejects_unknown_kind(wired):
    from dice_ratchet.infra.errors import InfraError

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
    from dice_ratchet.infra.errors import E, InfraError

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


def test_fm33_eval_report_writes_all_splits_and_plots(wired, capsys):
    """**F-M3-3 的端到端护栏**：`eval report` 必须 (a) 台账写全 split、(b) 真的产出 PNG。

    修复前两处都坏：
      · 台账只写 heldout 行（`_csv_row`）⇒ train 一侧永远缺席，train 对照曲线与 gap 图无源；
      · `plot_generations` **没有生产调用点** ⇒ `runs/*/plots/` 全空。
    且绘图侧的四列名在真实台账里不存在 ⇒ 即便接上了线，产出的也是**空白图**。
    """
    pytest.importorskip("matplotlib")
    import csv

    tmp_path, _cfg = wired
    src = _FIXTURES / "episodes" / "heldout"
    dst = tmp_path / "episodes" / "heldout"
    dst.mkdir(parents=True)
    for path in src.glob("*.jsonl"):
        (dst / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    assert cli_mod.cmd_eval_report(_ns()) == 0
    capsys.readouterr()

    # (a) 台账：至少一行 heldout，且列名与权威清单一致
    from dice_ratchet.infra.plotting import GENERATIONS_CSV_COLUMNS

    csv_path = tmp_path / "metrics" / "generations.csv"
    assert csv_path.is_file()
    with csv_path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        assert tuple(reader.fieldnames or ()) == tuple(GENERATIONS_CSV_COLUMNS)
        rows = list(reader)
    assert [r["split"] for r in rows] == ["heldout"], "夹具没有 train 局 ⇒ 只有 heldout 行"
    assert float(rows[0]["win_rate"] or 0) > 0, "win_rate 必须真的写进去了"

    # (b) 绘图：5 张 PNG 真的落盘（不是空白——空白会被 E_IO_PLOT_NO_DATA 拦成异常）
    from dice_ratchet.infra.plotting import PLOT_FILES

    plots = tmp_path / "plots"
    assert sorted(p.name for p in plots.glob("*.png")) == sorted(PLOT_FILES)
    for name in PLOT_FILES:
        assert (plots / name).stat().st_size > 0


def test_fm39_acceptance_handler_writes_json_and_returns_zero(wired, capsys):
    """`eval acceptance`（F-M3-9）：落 `metrics/acceptance.json`，**总是返回 0**。

    返回 0 是刻意的：它是**判定器**不是守卫。把「验收不合格」编码成进程退出码会让
    「命令失败」与「实验结论是否定」混淆（F47-D 刚把这两件事分开）。
    """
    import csv

    tmp_path, _cfg = wired
    (tmp_path / "metrics").mkdir(parents=True, exist_ok=True)
    from dice_ratchet.infra.plotting import GENERATIONS_CSV_COLUMNS

    with (tmp_path / "metrics" / "generations.csv").open("w", encoding="utf-8",
                                                        newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(GENERATIONS_CSV_COLUMNS))
        w.writeheader()
        for gen, win, lo in ((0, 0.50, 0.40), (1, 0.55, 0.53)):
            w.writerow({"generation": gen, "split": "heldout", "n": 40,
                        "win_rate": win, "ci_lo": lo, "decision": "accept"})
            # F48 之后台账含 train 行：判定器必须忽略它
            w.writerow({"generation": gen, "split": "train", "n": 30,
                        "win_rate": 0.99, "ci_lo": 0.9})
    for gen, dec in ((0, "baseline"), (1, "accept")):
        d = tmp_path / "generations" / f"gen-{gen:03d}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "report.json").write_text(json.dumps({"generation": gen, "decision": dec}),
                                       encoding="utf-8")

    rc = cli_mod.cmd_eval_acceptance(_ns(bootstrap_B=200))
    out = capsys.readouterr().out
    assert rc == 0, "判定器总是返回 0"
    payload = json.loads(out)
    assert payload["accepts"] == 1
    assert payload["monotonic_ok"] is True
    assert payload["ok"] is True
    assert (tmp_path / "metrics" / "acceptance.json").is_file()


def test_fm39_acceptance_handler_reports_failure_on_stderr(wired, capsys):
    """不通过时把原因写 stderr（**必须非静默**），但退出码仍为 0。"""
    import csv

    tmp_path, _cfg = wired
    (tmp_path / "metrics").mkdir(parents=True, exist_ok=True)
    from dice_ratchet.infra.plotting import GENERATIONS_CSV_COLUMNS

    with (tmp_path / "metrics" / "generations.csv").open("w", encoding="utf-8",
                                                        newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(GENERATIONS_CSV_COLUMNS))
        w.writeheader()
        for gen, win, lo in ((0, 0.80, 0.70), (1, 0.40, 0.30)):
            w.writerow({"generation": gen, "split": "heldout", "n": 40,
                        "win_rate": win, "ci_lo": lo, "decision": "reject"})
    rc = cli_mod.cmd_eval_acceptance(_ns(bootstrap_B=200))
    err = capsys.readouterr().err
    assert rc == 0
    assert "不通过" in err


# =====================================================================
# D-M3-5：`eval test --check` 只读预检（一次性资源的不可逆风险）
# =====================================================================

def test_dm35_test_check_never_evaluates_or_consumes(wired, monkeypatch, capsys):
    """**核心护栏**：预检不得调用 `evaluate_arm`，也不得创建存证。

    现场（实测的顺序问题）：`cmd_eval_test` 先跑 100 局 test（≈4.2 h / ≈¥88）
    —— 指标要先算出来才能写进取证 —— **之后**才调 `test_unlock`；而 `test_unlock`
    的「单次」判据是 **attestation 文件是否已存在**。所以「路径不可写」这类失败一旦发生，
    那 100 局已经花掉、却拿不到有效存证：这是 test 池上唯一真正不可逆的失败模式。
    """
    tmp_path, _cfg = wired

    def _boom(*a, **kw):   # pragma: no cover - 被调用即失败
        raise AssertionError("--check 不得调用 evaluate_arm（那会真的跑 100 局）")

    monkeypatch.setattr(cli_mod, "evaluate_arm", _boom)
    monkeypatch.setattr(cli_mod.seeds_mod, "test_unlock", _boom)
    rc = cli_mod.cmd_eval_test(_ns(check=True, unlock_once=True, transfer=False))
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert rc == 0
    assert payload["ok"] is True
    assert payload["test_seeds"] == 100, "test_full 应是 100 局"
    assert {c["check"] for c in payload["checks"]} >= {
        "test_pool_locked", "test_basket_seeds", "unlocked_protocol_valid",
        "protocol_hash", "attestation_not_exists", "attestation_dir_writable"}
    att = tmp_path / "generations" / "gen-000" / "test_attestation.json"
    assert not att.exists(), "预检**不得**创建存证（那会消耗一次性解锁）"


def test_dm35_test_check_fails_when_generation_already_unlocked(wired, capsys):
    """该代已有存证 ⇒ 预检必须**非 0 退出**并在 stderr 说清「未消耗解锁」。

    真跑到这一步才发现，代价是那 100 局。
    """
    tmp_path, _cfg = wired
    att = tmp_path / "generations" / "gen-000" / "test_attestation.json"
    att.parent.mkdir(parents=True, exist_ok=True)
    att.write_text("{}", encoding="utf-8")
    rc = cli_mod.cmd_eval_test(_ns(check=True, unlock_once=True, transfer=False))
    err = capsys.readouterr().err
    assert rc != 0, "预检不通过必须是守卫语义（非 0）"
    assert "attestation_not_exists" in err
    assert "未消耗解锁" in err


def test_dm35_test_check_fails_when_attestation_dir_unwritable(wired, capsys):
    """存证路径不可写 ⇒ 预检必须拦下（这正是「跑完 100 局才发现」的那个洞）。"""
    tmp_path, _cfg = wired
    rc = cli_mod.cmd_eval_test(_ns(check=True, unlock_once=True, transfer=False,
                                   attestation="/proc/definitely/not/writable/att.json"))
    err = capsys.readouterr().err
    assert rc != 0
    assert "attestation_dir_writable" in err


def test_dm35_test_check_does_not_require_unlock_once(wired, capsys):
    """`--check` 不该要求 `--unlock-once`：预检查的是「真跑时会不会失败」，
    而不是「有没有给解锁开关」。锁着也能预检，才不会诱导人先加上开关再试。"""
    tmp_path, _cfg = wired
    rc = cli_mod.cmd_eval_test(_ns(check=True, unlock_once=False, transfer=False))
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


# =====================================================================
# F56：台账 + 绘图必须由**同一个**助手写全（`loop run` 的两条路径都漏过）
# =====================================================================

def _f56_report(generation: int = 0) -> dict:
    """一代最小合法报告段（含台账两行所需的 heldout/train 指标）。"""
    return {
        "generation": generation, "harness_version": 0, "parent_version": 0,
        "decision": "baseline" if generation == 0 else "reject",
        "heldout": {"basket": "H3", "n": 20, "win_rate": 0.15, "win_rate_ci95": [0.05, 0.36],
                    "mean_stages_cleared": 4.3, "mean_hearts_left": 0.5,
                    "mean_battles_won": 11.0, "mean_decisions": 100.0,
                    "illegal_rate": 0.03, "fallback_rate": 0.001,
                    "truncation_rate": 0.25, "entropy_mean_norm": 0.4, "low_confidence": False},
        "train": {"window": 30, "n": 30, "win_rate": 0.25,
                  "mean_stages_cleared": 5.0, "mean_hearts_left": 1.0,
                  "mean_battles_won": 12.0, "mean_decisions": 110.0,
                  "illegal_rate": 0.02, "fallback_rate": 0.0, "truncation_rate": 0.2,
                  "entropy_mean_norm": 0.45},
        "gain_realism": {"win_rate_gap": 0.1, "verdict": "null"},
        "diversity": {"entropy_slope": 0.0, "collapse_flag": False, "jsd_kind": None},
    }


def test_f56_ledger_and_plots_writes_ledger_row_and_pngs(tmp_path):
    """给定一代报告；当 ``harness.ledger_and_plots``；

    则台账出现**该代**的 heldout 行、且 `plots/` 下 5 张 PNG **都有数据点**。

    现场（2026-10-12 M3 彩排，实测）：`runs/_sweep/m3-rehearsal/` 跑完 2 代后
    `plots/` **全空**、`metrics/generations.csv` **缺 gen-000 基线行** ⇒ 彩排的两条判据
    （「5 张 PNG 有数据点」「判据 1 判定器能给出结论」）必然不合格。
    """
    pytest.importorskip("matplotlib")
    from dice_ratchet.evals.harness import ledger_and_plots

    run = tmp_path / "runs" / "r-f56"
    (run / "metrics").mkdir(parents=True)
    out = ledger_and_plots(run, 0, _f56_report(0))
    assert out["ledger_error"] is None and out["plot_error"] is None, out
    assert out["ledger_rows"] >= 1
    rows = (run / "metrics" / "generations.csv").read_text(encoding="utf-8").splitlines()
    assert any(r.startswith("0,") and ",heldout," in r for r in rows), rows
    assert sorted(out["plots"]) == sorted(PLOT_FILES), out["plots"]
    assert all((run / "plots" / n).is_file() for n in PLOT_FILES)


def test_f56_ledger_and_plots_degrades_without_raising(tmp_path):
    """台账/绘图失败**不得**抛出（附属产物失败不阻塞裁决），但必须留下非静默的错误码。"""
    from dice_ratchet.evals.harness import ledger_and_plots

    run = tmp_path / "runs" / "r-f56-bad"
    run.mkdir(parents=True)
    # 报告段缺 heldout ⇒ 台账行构造失败；本调用仍须返回而不抛
    out = ledger_and_plots(run, 0, {"generation": 0, "harness_version": 0,
                                    "parent_version": 0, "decision": "baseline"})
    assert isinstance(out, dict)
    assert out["ledger_error"] is not None or out["plot_error"] is not None


# =====================================================================
# F60（2026-10-14）：`eval report` 不得伪造版本列
# =====================================================================

def test_f60_eval_report_reads_versions_from_existing_report(wired, capsys):
    """已裁决的代重算时，版本列 / play 面指纹必须**沿用 report.json 里的事实**。

    现场：这里原先写死 `harness_version=generation / parent_version=generation-1`，
    而这份内存里的 report 随后进 `ledger_and_plots` ⇒ 台账被静默覆盖（0/0 → 3/2），
    判据 1 把 `not_applicable` 翻成假结论「单调性不成立」。
    """
    tmp_path, _cfg = wired
    from dice_ratchet.evals.harness import generation_report_path

    src = _FIXTURES / "episodes" / "heldout"
    dst = tmp_path / "episodes" / "heldout"
    dst.mkdir(parents=True)
    for path in src.glob("*.jsonl"):
        (dst / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    # MOD-07 写下的权威报告（gen-002：起点 v0、结束时也在役 v0，play 面指纹 P）
    path = generation_report_path(tmp_path, 2)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "generation": 2, "harness_version": 0, "parent_version": 0,
        "harness_play_hash": "sha256:" + "a" * 64,
        "decision": "reject", "decided_by": "auto", "reason": "C2:no_significant_gain",
        # `measured`：本代真测过 ⇒ 重算会写台账行（继承的代不写行，那条规则另有用例）
        "heldout": {"basket": "H1", "n": 4, "win_rate": 0.25, "source": "measured"},
        "train": {"window": 30, "win_rate": 0.2, "n": 30, "source": "measured"},
        "gain_realism": {"win_rate_gap": 0.1, "gap_threshold": 0.15, "verdict": "null"},
        "diversity": {"entropy_slope": 0.0, "collapse_flag": False},
        "ablation": None, "candidates": [],
    }, ensure_ascii=False), encoding="utf-8")

    assert cli_mod.cmd_eval_report(_ns(generation=2)) == 0
    # 已裁决的代：重算报告落 **sidecar**，权威 report.json 不被覆盖
    assert (path.parent / "report.eval.json").is_file()
    authoritative = json.loads(path.read_text(encoding="utf-8"))
    assert authoritative["harness_version"] == 0 and authoritative["parent_version"] == 0
    # 台账行的版本列同样来自权威报告（不是 generation=2 / generation-1=1）
    import csv as _csv

    with (tmp_path / "metrics" / "generations.csv").open(encoding="utf-8") as fh:
        rows = [r for r in _csv.DictReader(fh) if r["split"] == "heldout"]
    assert rows and rows[0]["harness_version"] == "0", rows
    assert rows[0]["parent_version"] == "0", rows
    # ext 里记录版本列的出处（可核验）
    ext = json.loads((tmp_path / "metrics" / "gen-002.ext.json").read_text(encoding="utf-8"))
    assert ext["version_source"] == "report_json"
    assert ext["play_hash"] == "sha256:" + "a" * 64


def _prepare_train_run(tmp_path: Path, *, generation: int = 1,
                       served: int = 3) -> None:
    """造一个「有真训练块 + 同代冒烟局」的 run（F61/F64/F71 共用）。

    - ``episodes/heldout/``：复制 golden 夹具并把 ``generation`` 改成 ``generation``；
    - ``episodes/train/``：``take("train", generation)`` 的前 ``served`` 个 seed 记
      ``arm="train"``（真训练局），末一个 seed 记 ``arm="parent-smoke"``（同代冒烟局）。
    """
    from dice_ratchet.evals.seeds import take

    heldout_dir = tmp_path / "episodes" / "heldout"
    heldout_dir.mkdir(parents=True, exist_ok=True)
    for path in (_FIXTURES / "episodes" / "heldout").glob("*.jsonl"):
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        for row in rows:
            row["generation"] = generation
        (heldout_dir / path.name).write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
            encoding="utf-8")

    block = take("train", generation=generation, protocol=S.default_protocol())
    train_dir = tmp_path / "episodes" / "train"
    train_dir.mkdir(parents=True, exist_ok=True)

    def _write(seed: int, arm: str) -> None:
        key = f"e-{seed:06d}-g{generation:03d}-{arm}"
        row = {"type": "episode_end", "episode_key": key, "generation": generation,
               "harness_version": 0, "arm": arm, "seed": seed, "split": "train",
               "outcome": "win", "stages_cleared": 5, "hearts_left": 1, "battles_won": 9,
               "decisions": 90, "steps": 200, "illegal_attempts": 0,
               "fallback_decisions": 0, "truncation_reason": None, "dropped": False,
               "degraded": False, "kind_counts": [1] * 16, "slot_counts": {},
               "seq_hash": f"s{seed}", "decision_kinds": [0]}
        (train_dir / f"{key}.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    for seed in block[:served]:
        _write(seed, "train")
    _write(block[-1], "parent-smoke")     # 同代冒烟局：用的是本代训练块末尾 seed


def test_f64_eval_report_train_section_uses_the_served_block(wired):
    """``eval report`` 的 train 段与 ``evaluate_generation`` **同一口径**（F64）。

    修复前这里用 ``train_window`` 且**不过滤臂** ⇒ 在 `m3-formal` 上拿到的是 4 局冒烟局
    （``train.n=4``），还被写进台账当训练段。现在：取 ``take("train", generation)`` 的块 +
    ``arm="train"`` 真过滤；不足则 ``source="insufficient"``。
    """
    tmp_path, _cfg = wired
    _prepare_train_run(tmp_path, generation=1)

    assert cli_mod.cmd_eval_report(_ns(generation=1)) == 0
    report = json.loads((tmp_path / "generations" / "gen-001" / "report.json")
                        .read_text(encoding="utf-8"))
    assert report["train"]["n"] == 3, report["train"]
    assert report["train"]["source"] == "measured"


# =====================================================================
# F61 / F71（2026-10-14）：`--split train` 能解析臂，且**只读**
# =====================================================================

def test_f61_train_split_default_arm_is_the_served_block(wired, capsys):
    """F61：train split 的臂命名是另一套（``train`` / ``parent-smoke``）。

    修复前解析器只认 heldout 口径（``parent`` / ``cand-<id>``）⇒ `eval report
    --split train` 抛 `E_EVAL_ARM_NOT_ALLOWED`（现场退出码 2，train 侧报告没产出）。
    """
    tmp_path, _cfg = wired
    _prepare_train_run(tmp_path, generation=1)
    assert cli_mod.cmd_eval_report(_ns(generation=1, split="train")) == 0
    out = capsys.readouterr().out
    assert "arm: train" in out, out
    payload = json.loads((tmp_path / "metrics" / "gen-001.train.readonly.json")
                         .read_text(encoding="utf-8"))
    assert payload["arm"] == "train" and payload["readonly"] is True
    assert payload["heldout"]["n"] == 3, payload["heldout"]


def test_f71_train_split_recompute_writes_nothing_authoritative(wired, capsys):
    """**F71**：`--split train` **不得**写 report.json / 代际台账 / 绘图。

    这里刻意**显式传** ``--arm train``（F61 修复前也能解析）⇒ 修复前这条命令会一路走到
    写入路径：`report["heldout"]` 被塞进 train 的聚合、`generations/gen-001/report.json`
    与 `metrics/generations.csv` / `metrics/plots_data/` 全部落地。这正是「修好 F61 反而
    制造一个静默跨 split 污染」的现场。
    """
    tmp_path, _cfg = wired
    _prepare_train_run(tmp_path, generation=1)
    assert cli_mod.cmd_eval_report(_ns(generation=1, split="train", arm="train")) == 0
    assert not (tmp_path / "generations" / "gen-001" / "report.json").exists(), \
        "只读路径不得写权威报告"
    assert not (tmp_path / "metrics" / "generations.csv").exists(), \
        "只读路径不得写代际台账"
    assert not (tmp_path / "metrics" / "plots_data").exists(), "只读路径不得画图"
    assert not (tmp_path / "metrics" / "gen-001.ext.json").exists(), \
        "只读路径不得写 ext（它属于 heldout 口径）"
    assert (tmp_path / "metrics" / "gen-001.train.readonly.json").is_file(), \
        "复算口径要留档（非权威 sidecar）"
    out = capsys.readouterr().out
    assert "只读" in out


def test_f71_train_split_does_not_clobber_a_baseline_report(wired, capsys):
    """**F71 的最坏形态**：gen-000（``decision="baseline"``）不在 ``VERDICT_DECISIONS``
    里 ⇒ 不走 sidecar 保护，修复前 `--split train` 会**直接覆盖** ``report.json``
    的 heldout 段（把 train 的数字写成 heldout）。
    """
    tmp_path, _cfg = wired
    from dice_ratchet.evals.harness import generation_report_path

    path = generation_report_path(tmp_path, 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps({
        "generation": 0, "harness_version": 0, "parent_version": 0,
        "harness_play_hash": "sha256:" + "b" * 64,
        "decision": "baseline", "decided_by": "auto", "reason": "gen-000 基线",
        "heldout": {"basket": "H1", "n": 40, "win_rate": 0.175, "source": "measured"},
        "train": {"window": 0, "win_rate": 0.0, "n": 0, "source": "unavailable"},
        "gain_realism": {"win_rate_gap": 0.0, "gap_threshold": 0.15, "verdict": "null"},
        "diversity": {"entropy_slope": 0.0, "collapse_flag": False},
        "ablation": None, "candidates": [],
    }, ensure_ascii=False)
    path.write_text(original, encoding="utf-8")

    assert cli_mod.cmd_eval_report(_ns(generation=0, split="train", arm="train")) == 0
    assert path.read_text(encoding="utf-8") == original, \
        "gen-000 的 report.json 必须逐字节不变（修复前会被 train 聚合覆盖）"
    assert not (tmp_path / "metrics" / "generations.csv").exists()
