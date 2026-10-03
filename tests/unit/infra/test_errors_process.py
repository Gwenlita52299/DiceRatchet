"""错误码注册表 / 进程确定性环境 / 原子写 测试（FR-09-16 / FR-09-17 / NFR-09-4）。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from gamer_rsi.infra import events
from gamer_rsi.infra.errors import (EXIT_CONFIG, EXIT_DOMAIN, EXIT_INTERRUPT,
                                    EXIT_IO, EXIT_MODEL, EXIT_OK, EXIT_SANDBOX,
                                    E, InfraError, error_code_of)
from gamer_rsi.infra.io import read_jsonl, write_json_atomic
from gamer_rsi.infra.process import (bootstrap_process, process_fingerprint,
                                     process_snapshot)

REPO = Path(__file__).resolve().parents[3]


# ---- FR-09-16 错误码注册表 -------------------------------------------------------------------

def test_all_error_codes_have_exit_code():
    # EXIT_CODE 未覆盖的码必须显式落到默认（领域失败），不能 KeyError
    for code in E:
        assert isinstance(error_code_of(code), int)


def test_exit_code_mapping():
    assert error_code_of(E.E_IO_CONFIG_INVALID) == EXIT_CONFIG
    assert error_code_of(E.E_IO_RUN_CONFLICT) == EXIT_CONFIG
    assert error_code_of(E.E_IO_GIT_DIRTY) == EXIT_SANDBOX
    assert error_code_of(E.E_IO_GUARD_LAYER) == EXIT_SANDBOX
    assert error_code_of(E.E_IO_GUARD_RANDOM) == EXIT_SANDBOX
    assert error_code_of(E.E_IO_DISK_FULL) == EXIT_IO
    assert error_code_of(E.E_LLM_UNAVAILABLE) == EXIT_MODEL
    assert error_code_of(E.E_EVAL_INV5_LEAK) == EXIT_SANDBOX


def test_degradation_codes_do_not_fail():
    # Spec §8.5：可观测性缺失降级，不非零退出
    assert error_code_of(E.E_IO_PLOT_UNAVAILABLE) == EXIT_OK
    assert error_code_of(E.E_IO_GIT_UNAVAILABLE) == EXIT_OK
    assert error_code_of(E.E_IO_CONFIG_UNKNOWN) == EXIT_OK
    assert error_code_of(E.E_IO_TRAILING_TRUNCATED) == EXIT_OK


def test_infra_error_carries_three_elements():
    # NFR-09-4：错误码 + 人类可读信息 + 复现命令
    err = InfraError(E.E_IO_RUN_CONFLICT, "冲突", hint="改用 --run-id")
    assert err.code == E.E_IO_RUN_CONFLICT
    assert err.message == "冲突"
    assert err.hint == "改用 --run-id"
    assert err.exit_code == EXIT_CONFIG
    assert str(err) == "[E_IO_RUN_CONFLICT] 冲突\n  hint: 改用 --run-id"


def test_infra_error_defaults_message_from_registry():
    err = InfraError(E.E_IO_GIT_DIRTY, "")
    assert err.message == ERROR_MESSAGE_IF_AVAILABLE()


def ERROR_MESSAGE_IF_AVAILABLE() -> str:
    from gamer_rsi.infra.errors import ERROR_MESSAGE
    return ERROR_MESSAGE[E.E_IO_GIT_DIRTY]


def test_error_codes_cover_spec_prefixes():
    joined = " ".join(E)
    for prefix in ("E_ENV_", "E_ACTION_", "E_LLM_", "E_EVOLVE_", "E_EVAL_", "E_IO_"):
        assert prefix in joined


def test_spec_named_codes_present():
    # Spec §8.3 显式统一的命名
    for code in ("E_LLM_UNAVAILABLE", "E_LLM_BACKEND_ERROR", "E_LLM_SCHEMA_INVALID",
                 "E_LLM_TIMEOUT", "E_LLM_OOM", "E_EVAL_INV5_LEAK",
                 "E_IO_MEMORY_WRITE_FAILED"):
        assert code in E.__members__


# ---- FR-09-17 进程确定性环境 -----------------------------------------------------------------

def test_bootstrap_forces_env(monkeypatch):
    monkeypatch.delenv("GAMERSI_REEXECED", raising=False)
    monkeypatch.setenv("PYTHONHASHSEED", "0") # 已设置 → 不触发 re-exec
    snap = bootstrap_process(reexec=False)
    assert os.environ["PYTHONHASHSEED"] == "0"
    assert os.environ["TZ"] == "UTC"
    assert os.environ["LC_ALL"] == "C.UTF-8"
    assert snap["threads"] == 1


def test_bootstrap_is_idempotent(monkeypatch):
    monkeypatch.setenv("PYTHONHASHSEED", "0")
    bootstrap_process(reexec=False)
    fingerprint = process_fingerprint()
    assert fingerprint["tz"] == "UTC"
    assert fingerprint["pythonhashseed"] == "0"
    assert fingerprint["threads"] == 1


def test_bootstrap_reexec_in_subprocess(tmp_path):
    # 子进程内 PYTHONHASHSEED 未设置时会 re-exec 一次（-m 语义保留）
    script = tmp_path / "boot.py"
    script.write_text(
        "import os, sys\n"
        "sys.path.insert(0, %r)\n"
        "from gamer_rsi.infra.process import bootstrap_process\n"
        "bootstrap_process(module='gamer_rsi')\n"
        "print('HASHSEED=' + os.environ['PYTHONHASHSEED'])\n"
        "print('REEXECED=' + os.environ.get('GAMERSI_REEXECED', '0'))\n"
        % (str(REPO),), encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
           "PYTHONHASHSEED": ""} # 未设置
    proc = subprocess.run([sys.executable, str(script)], cwd=str(REPO),
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    assert "HASHSEED=0" in proc.stdout
    assert "REEXECED=1" in proc.stdout


# ---- NFR-09-3 derive_seed 跨 PYTHONHASHSEED --------------------------------------------------

def test_derive_seed_stable_across_hashseed(tmp_path):
    # T-09-5：不同 PYTHONHASHSEED 的子进程返回值完全相同
    script = tmp_path / "seed.py"
    script.write_text(
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "from gamer_rsi.infra.config import derive_seed\n"
        "print(derive_seed('a', 1, 2))\n" % (str(REPO),), encoding="utf-8")
    values = set()
    for seed in ("0", "1", "42", "random"):
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
               "PYTHONHASHSEED": seed}
        proc = subprocess.run([sys.executable, str(script)], cwd=str(REPO),
                              capture_output=True, text=True, env=env)
        assert proc.returncode == 0, proc.stderr
        values.add(proc.stdout.strip())
    assert len(values) == 1


# ---- FR-09-18 原子写（与 events 的集成）--------------------------------------------------------

def test_atomic_write_survives_concurrent_readers(tmp_path):
    # 读者永不见半截文件：写的过程中读到的必须是旧值或新值，且可解析
    path = tmp_path / "state.json"
    write_json_atomic(path, {"v": 0})
    observed_ok = True
    for i in range(30):
        write_json_atomic(path, {"v": i})
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            observed_ok = False
            break
    assert observed_ok


def test_bootstrap_emits_no_event(monkeypatch):
    """回归（2026-10-03）：bootstrap 在配置加载与 events.bind 之前执行，
    不能发 ``infra.run_start``——否则 dev 模式下缺 ``config_hash`` 必填字段，
    任何 CLI 命令都会以退出码 1 失败（曾用 ``guards check`` 复现）。"""
    from gamer_rsi.infra import events as ev

    emitted: list[str] = []
    monkeypatch.setattr(ev, "emit", lambda name, **f: emitted.append(name))
    monkeypatch.setenv("GAMERSI_DEV", "1")
    ev.set_dev_mode(True)
    try:
        bootstrap_process(reexec=False)
    finally:
        ev.set_dev_mode(False)
    assert emitted == []


def test_process_snapshot_before_and_after_bootstrap(monkeypatch):
    """``process_snapshot()`` 无论 bootstrap 是否跑过都能给 run_start 提供 env。"""
    import gamer_rsi.infra.process as P

    monkeypatch.setattr(P, "_SNAPSHOT", None)
    fresh = P.process_snapshot()
    assert fresh["TZ"] == "UTC" and "python" in fresh
    after = bootstrap_process(reexec=False)
    assert dict(P.process_snapshot()) == dict(after)
    assert after["threads"] == 1 and after["python"]
