"""CLI 测试（FR-09-10/11/12）—— T-09-13 / T-09-15。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from gamer_rsi.infra import cli, events
from gamer_rsi.infra.cli import build_parser, main, parse_seeds_spec
from gamer_rsi.infra.errors import E
from gamer_rsi.infra.io import read_jsonl

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _clean_events():
    events.unbind()
    yield
    events.unbind()


def _run(argv: list[str]) -> tuple[int, str, str]:
    """直接调用 main（不 re-exec），返回 (exit_code, stdout, stderr)。"""
    import io
    import contextlib
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv, reexec=False)
    return code, out.getvalue(), err.getvalue()


# ---- FR-09-10 子命令树对齐 Spec §4.6 --------------------------------------------------------

SPEC_COMMANDS = [
    ("env", "selftest"), ("play",), ("baseline",), ("loop", "run"),
    ("loop", "status"), ("loop", "smoke"), ("evolve", "approve"), ("evolve", "rollback"),
    ("eval", "report"), ("eval", "ablation"), ("eval", "test"), ("eval", "diversity"),
    ("audit", "leak"), ("inference", "probe"), ("inference", "bench"),
    ("inference", "robustness"), ("memory", "stats"), ("memory", "snapshot"),
    ("memory", "rollback"), ("memory", "verify"), ("memory", "retrieve"),
    ("guards", "check"),
]


@pytest.mark.parametrize("path", SPEC_COMMANDS)
def test_spec_commands_registered(path: tuple[str, ...]):
    # Spec §4.6 的每个命令都必须在 parser 里
    parser = build_parser()
    args = parser.parse_args(list(path) + _required_args(path))
    assert args.command == path[0]
    assert args.command_path == list(path)


def _required_args(path: tuple[str, ...]) -> list[str]:
    if path == ("evolve", "approve") or path == ("evolve", "rollback"):
        return ["--generation", "1"]
    return []


def test_every_declared_command_is_wired():
    """M2 收口：MOD-01…09 全部落地，命令面不再有 ``E_IO_MODULE_PENDING`` 的悬空命令。

    守门口径从"未落地模块返回 pending"改为"**不允许**再返回 pending"——任何新命令如果
    只登记了 argparse 却没有注册实现，本用例会红（``evolve approve`` 作为抽样）。
    """
    from gamer_rsi.app import wire
    from gamer_rsi.infra import cli as _cli

    wire()
    # MOD-07 的两个子命令必须真的注册了实现（不是只登记了 argparse）
    handlers = getattr(_cli, "_HANDLERS", {})
    assert ("evolve", "approve") in handlers
    assert ("evolve", "rollback") in handlers
    code, _out, err = _run(["evolve", "approve", "--run-id", "r-pending",
                            "--generation", "1"])
    assert E.E_IO_MODULE_PENDING not in err
    # run 目录不存在 → 配置错误（退出码 2）
    assert code == 2


def test_guards_check_on_repo_is_clean():
    code, out, err = _run(["guards", "check", "--root", "gamer_rsi"])
    assert code == 0, err
    assert "OK" in out


def test_guards_check_dirty_fixture_exits_4():
    code, out, err = _run(["guards", "check",
                           "--root", "tests/fixtures/guards/gamer_rsi"])
    assert code == 4, err
    assert "E_IO_GUARD_LAYER" in err or "E_IO_GUARD_LAYER" in out


def test_guards_check_fix_hints():
    code, out, err = _run(["guards", "check", "--fix-hints",
                           "--root", "tests/fixtures/guards/gamer_rsi"])
    assert code == 4
    assert "guard: allow" in out


def test_guards_check_only_exempted_exits_0(tmp_path):
    # 全部违例都被白名单豁免 → 0
    src = tmp_path / "ok.py"
    src.write_text("import random  # guard: allow E_IO_GUARD_RANDOM reason=t\n",
                   encoding="utf-8")
    code, out, err = _run(["guards", "check", "--root", str(tmp_path)])
    assert code == 0
    assert "exempted" in out


# ---- FR-09-12 dry-run -----------------------------------------------------------------------

def test_dry_run_emits_parseable_json(tmp_path, monkeypatch):
    monkeypatch.chdir(REPO)
    code, out, err = _run(["loop", "run", "--generations", "3", "--k", "30", "--dry-run"])
    assert code == 0, err
    plan = json.loads(out)
    assert plan["dry_run"] is True
    assert plan["config"]["loop"]["k"] == 30
    assert plan["config"]["loop"]["max_generations"] == 3
    assert plan["config_hash"]


def test_dry_run_writes_nothing_to_runs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "default.yaml").write_text("loop:\n  k: 10\n", encoding="utf-8")
    code, out, err = _run(["loop", "run", "--dry-run", "--config-dir", "configs"])
    assert code == 0, err
    # T-09-13：runs/ 无任何新文件
    assert not (tmp_path / "runs").exists()


def test_dry_run_after_subcommand(tmp_path, monkeypatch):
    # --dry-run 出现在子命令之后也必须生效
    monkeypatch.chdir(REPO)
    code, out, err = _run(["loop", "run", "--dry-run"])
    assert code == 0, err
    assert json.loads(out)["dry_run"] is True


# ---- FR-09-11 退出码 --------------------------------------------------------------------------

def test_unknown_command_exits_2():
    code, out, err = _run(["nope"])
    assert code == 2


def test_missing_required_arg_exits_2():
    code, out, err = _run(["evolve", "approve"])
    assert code == 2


def test_bad_choice_exits_2():
    code, out, err = _run(["play", "--agent", "bogus"])
    assert code == 2


def test_invalid_args_stderr_contains_error_code():
    """T-09-15（P1-10）：非法参数 → 退出码 2 且 stderr 含错误码。"""
    code, out, err = _run(["play", "--agent", "bogus"])
    assert code == 2
    assert E.E_IO_CONFIG_INVALID in err
    code, out, err = _run(["nope"])
    assert code == 2 and E.E_IO_CONFIG_INVALID in err


# ---- 回归（2026-10-03 审计 P1-2/P1-7/P1-12）：注册表 + run 生命周期 --------------------

@pytest.fixture()
def clean_registry():
    """保存/恢复命令注册表与活跃 run，避免用例间串味。"""
    saved = dict(cli._HANDLERS)
    saved_active = cli._ACTIVE_RUN
    saved_provider = cli._CONTENT_HASH_PROVIDER
    yield
    cli._HANDLERS.clear()
    cli._HANDLERS.update(saved)
    cli._ACTIVE_RUN = saved_active
    cli._CONTENT_HASH_PROVIDER = saved_provider


def test_register_extension_point_overrides_pending(clean_registry):
    """FR-09-10：实现由模块经 register() 注入，未注册才落 E_IO_MODULE_PENDING。"""
    # 本用例测的是**机制**（未注册 → pending），因此显式摘掉 play 的接线
    # （fixture 会在用后恢复；play 自 2026-10-04 M1 验收 F4 起已由组合根注册）
    cli._HANDLERS.pop(("play",), None)
    assert E.E_IO_MODULE_PENDING in _run(["play"])[2]
    cli.register(["play"], lambda args: 0)
    code, out, err = _run(["play", "--seeds", "train:1000-1000"])
    assert code == 0, err
    assert (("play",) in cli.registered_commands())


def test_run_end_and_io_latency_written(tmp_path, monkeypatch, clean_registry):
    """P1-2：命令结束必须发 infra.run_end（含 exit_code/duration_s）并写 io_latency.csv。"""
    from argparse import Namespace

    from gamer_rsi.infra import events
    from gamer_rsi.infra import run as run_mod

    monkeypatch.chdir(tmp_path)
    real_init = run_mod.init_run_dir
    monkeypatch.setattr(cli, "init_run_dir",
                        lambda *a, **k: real_init(*a, **{**k, "git_check": False}))

    def handler(args: Namespace) -> int:
        cli.cli_init_run(Namespace(config_dir=str(REPO / "configs"), run_id="r-end",
                                   resume=False, dry_run=False))
        events.emit("evolve.version", from_version=0, to_version=1, git_commit="abc")
        return 7

    cli.register(["loop", "run"], handler)
    code, out, err = _run(["loop", "run", "--generations", "1"])
    assert code == 7, err
    rows, _ = read_jsonl(tmp_path / "runs" / "r-end" / "events.jsonl")
    names = [r["event"] for r in rows]
    assert names[0] == "infra.run_start" and names[-1] == "infra.run_end"
    end = rows[-1]
    assert end["exit_code"] == 7 and end["duration_s"] >= 0
    latency = tmp_path / "runs" / "r-end" / "metrics" / "io_latency.csv"
    assert latency.is_file()
    header, *body = latency.read_text(encoding="utf-8").strip().splitlines()
    assert header == "run_id,n,p50_ms,p99_ms,max_ms,exit_code"
    assert body[0].startswith("r-end,") and body[0].endswith(",7")
    assert not events.is_bound() # 收尾必须解绑


def test_content_hash_provider_feeds_snapshot(tmp_path, monkeypatch, clean_registry):
    """Spec §7：content_hash 由组合根注入的提供者写入 config.json。"""
    from argparse import Namespace

    from gamer_rsi.infra import run as run_mod

    monkeypatch.chdir(tmp_path)
    real_init = run_mod.init_run_dir
    monkeypatch.setattr(cli, "init_run_dir",
                        lambda *a, **k: real_init(*a, **{**k, "git_check": False}))
    cli.register_content_hash_provider(lambda: "sha256:content-hash")

    def handler(args: Namespace) -> int:
        paths, _cfg, _rid = cli.cli_init_run(Namespace(
            config_dir=str(REPO / "configs"), run_id="r-hash", resume=False, dry_run=False))
        assert json.loads(paths.config.read_text(encoding="utf-8"))["content_hash"] \
            == "sha256:content-hash"
        return 0

    cli.register(["loop", "run"], handler)
    assert _run(["loop", "run"])[0] == 0


def test_config_overrides_maps_log_level():
    """P1-7：--log-level 必须真正进入配置覆盖（旧实现是 no-op）。"""
    from argparse import Namespace

    from gamer_rsi.infra.config import load_config

    assert cli.config_overrides(Namespace(log_level="DEBUG")) == {"infra.log_level": "DEBUG"}
    cfg = load_config(cli.config_overrides(Namespace(log_level="DEBUG")),
                      config_dir=REPO / "configs")
    assert cfg.infra.log_level == "DEBUG"


def test_missing_run_dir_reports_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "default.yaml").write_text("", encoding="utf-8")
    code, out, err = _run(["loop", "status", "--run-id", "ghost", "--config-dir",
                           "configs"])
    # 未绑定模块命令：E_IO_MODULE_PENDING（退出码 2）
    assert code == 2


# ---- FR-09-10 --seeds SPEC 解析 ----------------------------------------------------------------

@pytest.mark.parametrize("spec,expected", [
    ("train:1000-1004", {"train": [1000, 1001, 1002, 1003, 1004]}),
    ("heldout:2000-2099", {"heldout": list(range(2000, 2100))}),
    ("9000,9001", {"default": [9000, 9001]}),
])
def test_parse_seeds_spec(spec: str, expected: dict):
    assert parse_seeds_spec(spec) == expected


def test_parse_seeds_spec_file(tmp_path):
    f = tmp_path / "seeds.txt"
    f.write_text("1000,1001 1002\n", encoding="utf-8")
    assert parse_seeds_spec(f"file:{f}") == {"default": [1000, 1001, 1002]}


def test_parse_seeds_spec_bad_file(tmp_path):
    from gamer_rsi.infra.errors import InfraError
    with pytest.raises(InfraError) as exc:
        parse_seeds_spec(f"file:{tmp_path / 'nope.txt'}")
    assert exc.value.code == E.E_IO_CONFIG_INVALID


# ---- G6 可复现命令（端到端子进程）--------------------------------------------------------------

@pytest.mark.skipif(not (REPO / ".venv" / "bin" / "python").is_file(),
                    reason="无 .venv")
def test_end_to_end_guards_check():
    env = {"MPLCONFIGDIR": "/tmp", "PYTHONHASHSEED": "0", "PATH": "/usr/bin:/bin"}
    proc = subprocess.run(
        [str(REPO / ".venv" / "bin" / "python"), "-m", "gamer_rsi",
         "guards", "check", "--root", "gamer_rsi"],
        cwd=str(REPO), capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    assert "guards: OK" in proc.stdout


@pytest.mark.skipif(not (REPO / ".venv" / "bin" / "python").is_file(),
                    reason="无 .venv")
def test_end_to_end_dev_mode_is_not_broken():
    """回归（2026-10-03）：``GAMERSI_DEV=1`` 下任何命令都曾因 bootstrap 早发
    ``infra.run_start``（缺 config_hash 必填字段）而退出 1。"""
    env = {"MPLCONFIGDIR": "/tmp", "PYTHONHASHSEED": "0", "PATH": "/usr/bin:/bin",
           "GAMERSI_DEV": "1"}
    proc = subprocess.run(
        [str(REPO / ".venv" / "bin" / "python"), "-m", "gamer_rsi",
         "guards", "check", "--root", "gamer_rsi"],
        cwd=str(REPO), capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    assert "E_IO_EVENT_UNKNOWN" not in proc.stdout + proc.stderr
