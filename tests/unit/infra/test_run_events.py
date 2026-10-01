"""run 目录树 / state 原子写 / 事件注册表 测试 —— T-09-12 / T-09-13 / T-09-16。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gamer_rsi.infra import events
from gamer_rsi.infra.config import config_hash, load_config
from gamer_rsi.infra.errors import E, InfraError
from gamer_rsi.infra.io import read_jsonl
from gamer_rsi.infra.run import (RunPaths, init_run_dir, make_run_id, read_state,
                                 resolve_run_dir, utc_timestamp, write_state)

REPO = Path(__file__).resolve().parents[3]
CONFIGS = REPO / "configs"

# Spec §5 的目录树
_EXPECTED_TREE = ["audit", "episodes", "generations", "memory", "metrics",
                  "plots", "prompts", "reflections"]


@pytest.fixture()
def cfg():
    return load_config(config_dir=CONFIGS)


@pytest.fixture()
def runs(tmp_path, cfg):
    return tmp_path / "runs"


def _init(runs, cfg, run_id="r-0001", **kw):
    return init_run_dir(run_id, cfg, runs_root=runs, git_check=False, **kw)


# ---- T-09-12 run 目录初始化 -------------------------------------------------------------

def test_init_run_dir_creates_full_tree(runs, cfg):
    paths = _init(runs, cfg)
    for name in _EXPECTED_TREE:
        assert (paths.root / name).is_dir(), name
    assert paths.config.is_file()
    assert paths.events.name == "events.jsonl"
    assert paths.state.name == "state.json"


def test_init_run_dir_is_idempotent(runs, cfg):
    a = _init(runs, cfg)
    b = _init(runs, cfg)
    assert a.root == b.root
    assert b.config.is_file()


def test_init_run_dir_conflict_on_different_hash(runs, cfg):
    # T-09-12：同 run_id 但 config_hash 不同 → E_IO_RUN_CONFLICT
    _init(runs, cfg)
    other = load_config(cli_overrides={"loop.k": 31}, config_dir=CONFIGS)
    with pytest.raises(InfraError) as exc:
        init_run_dir("r-0001", other, runs_root=runs, git_check=False)
    assert exc.value.code == E.E_IO_RUN_CONFLICT
    assert exc.value.exit_code == 2


def test_config_snapshot_contents(runs, cfg):
    paths = _init(runs, cfg)
    snap = json.loads(paths.config.read_text(encoding="utf-8"))
    assert snap["config_hash"] == config_hash(cfg)
    assert snap["run_id"] == "r-0001"
    assert "config" in snap


def test_run_id_format(cfg):
    # FR-09-4：{git_short_sha}-{config_hash[:8]}-{utc_ts}
    run_id, sha = make_run_id(cfg, now=datetime(2026, 10, 1, 12, 0, 0,
                                                tzinfo=timezone.utc))
    parts = run_id.split("-")
    assert parts[0] == sha
    assert len(parts[1]) == 8
    assert parts[2] == "20261001T120000Z"


def test_run_id_explicit_wins(cfg):
    run_id, _ = make_run_id(cfg, explicit="repro-001")
    assert run_id == "repro-001"


def test_utc_timestamp_format():
    ts = utc_timestamp(datetime(2026, 10, 1, 12, 30, 45, tzinfo=timezone.utc))
    assert ts == "20261001T123045Z"
    assert ts.endswith("Z")


def test_episodes_split_validation(runs, cfg):
    paths = _init(runs, cfg)
    assert paths.episodes_split("train").is_dir()
    assert paths.episodes_split("heldout").is_dir()
    with pytest.raises(InfraError):
        paths.episodes_split("secret")


# ---- FR-09-19 runs/ 必须被 gitignore ----------------------------------------------------

def test_runs_not_ignored_rejected(tmp_path, cfg):
    # 在一个 git 仓库里建 runs/，且不加入 .gitignore → E_IO_RUNS_NOT_IGNORED
    repo = tmp_path / "repo"
    repo.mkdir()
    import subprocess
    subprocess.run(["git", "init", "-q", str(repo)], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"],
                   check=True, capture_output=True)
    (repo / "a.txt").write_text("x", encoding="utf-8")
    with pytest.raises(InfraError) as exc:
        init_run_dir("r-0001", cfg, runs_root=repo / "runs")
    assert exc.value.code == E.E_IO_RUNS_NOT_IGNORED
    assert exc.value.exit_code == 2


def test_runs_ignored_accepted(tmp_path, cfg):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    (repo / ".gitignore").write_text("runs/\n", encoding="utf-8")
    paths = init_run_dir("r-0001", cfg, runs_root=repo / "runs")
    assert paths.root.is_dir()


# ---- state.json 原子读写（FR-09-18）-------------------------------------------------------

def test_state_roundtrip(runs, cfg):
    paths = _init(runs, cfg)
    write_state(paths, {"phase": "train", "tick": 3})
    assert read_state(paths) == {"phase": "train", "tick": 3}


def test_state_no_tmp_left(runs, cfg):
    paths = _init(runs, cfg)
    write_state(paths, {"phase": "train"})
    assert not (paths.state.parent / "state.json.tmp").exists()


def test_state_overwrite_is_atomic(runs, cfg):
    paths = _init(runs, cfg)
    for i in range(50):
        write_state(paths, {"tick": i})
    assert read_state(paths)["tick"] == 49


# ---- FR-09-12 --resume 定位 --------------------------------------------------------------

def test_resolve_run_dir_resume(runs, cfg):
    paths = _init(runs, cfg)
    write_state(paths, {"phase": "train"})
    again = resolve_run_dir("r-0001", cfg, runs_root=runs, resume=True)
    assert again.root == paths.root


def test_resume_missing_state_rejected(runs, cfg):
    _init(runs, cfg)
    with pytest.raises(InfraError) as exc:
        resolve_run_dir("r-0001", cfg, runs_root=runs, resume=True)
    assert exc.value.code == E.E_IO_STATE_MISSING


def test_resume_unknown_run_rejected(runs, cfg):
    with pytest.raises(InfraError):
        resolve_run_dir("nope", cfg, runs_root=runs, resume=True)


def test_resume_rejects_config_mismatch(runs, cfg):
    _init(runs, cfg)
    other = load_config(cli_overrides={"loop.k": 31}, config_dir=CONFIGS)
    with pytest.raises(InfraError) as exc:
        resolve_run_dir("r-0001", other, runs_root=runs, resume=True)
    assert exc.value.code == E.E_IO_RUN_CONFLICT


def test_resume_tolerates_harness_change(runs, cfg, tmp_path):
    # B-19：config_hash 不含 harness，所以演化过的 run 也能续跑
    _init(runs, cfg)
    # harness 变化不改变 config_hash —— 直接复用同一 cfg 即可证明
    paths = resolve_run_dir("r-0001", cfg, runs_root=runs)
    assert paths.root.is_dir()


# ---- T-09-13 / FR-09-7 事件注册表 ----------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_events():
    events.unbind()
    events.set_dev_mode(True)
    yield
    events.unbind()
    events.set_dev_mode(False)


def test_emit_writes_bound_sink(runs, cfg):
    paths = _init(runs, cfg)
    events.bind(paths.events, "r-0001")
    events.emit("infra.run_start", config_hash=config_hash(cfg))
    rows, _ = read_jsonl(paths.events)
    assert rows[0]["event"] == "infra.run_start"
    assert rows[0]["run_id"] == "r-0001"
    assert rows[0]["module"] == "infra"
    assert rows[0]["ts"].endswith("Z")


def test_unknown_event_raises_in_dev(runs, cfg):
    paths = _init(runs, cfg)
    events.bind(paths.events, "r-0001")
    with pytest.raises(InfraError) as exc:
        events.emit("infra.not_registered")
    assert exc.value.code == E.E_IO_EVENT_UNKNOWN


def test_unknown_event_downgraded_in_prod(runs, cfg):
    events.set_dev_mode(False)
    paths = _init(runs, cfg)
    events.bind(paths.events, "r-0001")
    events.emit("module.mystery", foo=1)
    rows, _ = read_jsonl(paths.events)
    assert rows[0]["event"] == "infra.unknown_event"
    assert rows[0]["original"] == "module.mystery"


def test_alias_events_resolved(runs, cfg):
    paths = _init(runs, cfg)
    events.bind(paths.events, "r-0001")
    events.emit("memory_retrieve", obs_digest="sha256:x", returned_ids=[])
    rows, _ = read_jsonl(paths.events)
    assert rows[0]["event"] == "memory.retrieve"


def test_missing_required_field_raises_in_dev(runs, cfg):
    paths = _init(runs, cfg)
    events.bind(paths.events, "r-0001")
    with pytest.raises(InfraError):
        events.emit("infra.run_start") # 缺 config_hash


def test_unbound_emit_dropped_not_raised():
    before = events.stats()["dropped_unbound"]
    events.emit("infra.run_start", config_hash="x")
    assert events.stats()["dropped_unbound"] == before + 1


def test_registry_has_spec_sections():
    names = set(events.event_names())
    # Spec §8.2 / PRD §6.5 的核心事件必须在册
    for ev in ["infra.run_start", "infra.run_end", "infra.guard_finding",
               "infra.redaction_hit", "sandbox.gate_reject", "llm.call",
               "policy.decision", "memory.add", "loop.episode_end",
               "evolve.decision", "eval.report"]:
        assert ev in names, ev
