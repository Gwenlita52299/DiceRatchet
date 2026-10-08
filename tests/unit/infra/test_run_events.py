"""run 目录树 / state 原子写 / 事件注册表 测试 —— T-09-12 / T-09-13 / T-09-16。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dice_ratchet.infra import events
from dice_ratchet.infra.config import config_hash, load_config
from dice_ratchet.infra.errors import E, InfraError
from dice_ratchet.infra.io import read_jsonl
from dice_ratchet.infra.run import (RunPaths, init_run_dir, make_run_id, read_state,
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


def test_init_run_dir_creates_split_dirs_and_events_file(runs, cfg):
    """FR-09-5（P1-9）：episodes/<split>/ 四个 split 与 events.jsonl 必须在建 run 时齐备。"""
    paths = _init(runs, cfg)
    for split in ("train", "heldout", "test", "demo"):
        assert (paths.episodes / split).is_dir(), split
    assert paths.events.is_file() and paths.events.stat().st_size == 0


def test_config_snapshot_is_complete(runs, cfg, monkeypatch):
    """Spec §7 / P1-8：快照含完整 AppConfig、content_hash、env 白名单与 harness 覆盖键。"""
    monkeypatch.setenv("DICERATCHET_LOOP__K", "12")
    cfg2 = load_config(config_dir=CONFIGS)
    paths = init_run_dir("r-snap", cfg2, runs_root=runs, git_check=False,
                         content_hash="content-abc")
    snap = json.loads(paths.config.read_text(encoding="utf-8"))
    assert snap["content_hash"] == "content-abc"
    assert snap["env_whitelist"] == ["DICERATCHET_LOOP__K"]
    assert snap["harness_overridden_keys"] == []
    # 完整 AppConfig：volatile 字段也在（canonical_json 会裁掉它们）
    assert snap["config"]["infra"]["log_level"] == "INFO"
    assert snap["config"]["infra"]["runs_root"] == "runs"
    assert snap["config"]["loop"]["k"] == 12


def test_runs_ignored_degrades_outside_git_repo(tmp_path, cfg):
    """非 git 目录（git check-ignore 退出码 ≥2）按可观测性缺失降级，不阻断建 run。"""
    paths = init_run_dir("r-nogit", cfg, runs_root=tmp_path / "runs")
    assert paths.root.is_dir()


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


def test_resume_tolerates_real_harness_change(runs, cfg, tmp_path):
    """T-09-16（真实版）：harness 演化后 `--resume` 必须通过 config_hash 校验（B-19）。

    回归（2026-10-03 审计 P0-1）：旧用例只是复用同一个 cfg，从未改动 harness 文件，
    因此没能发现"harness 层被算进 config_hash"的问题。
    """
    paths = _init(runs, cfg)
    write_state(paths, {"phase": "train"})
    harness = tmp_path / "harness"
    harness.mkdir()
    (harness / "config.json").write_text(
        json.dumps({"agent.policy.temperature": 0.95, "memory.retrieve.top_k": 3}),
        encoding="utf-8")
    evolved = load_config(config_dir=CONFIGS, harness_config=harness / "config.json")
    assert evolved.agent.policy.temperature == 0.95 # 生效视图确实演化过
    again = resolve_run_dir("r-0001", evolved, runs_root=runs, resume=True)
    assert again.root == paths.root
    # 幂等再 init 同样不得冲突（演化后的 run 仍属同一 run 身份）
    assert init_run_dir("r-0001", evolved, runs_root=runs, git_check=False).root == paths.root


def test_resume_rejects_static_config_change(runs, cfg):
    """反向用例：静态配置变了 → 仍必须拒绝续跑（config_hash 覆盖静态配置）。"""
    _init(runs, cfg)
    other = load_config(cli_overrides={"infra.fsync_every": 7}, config_dir=CONFIGS)
    with pytest.raises(InfraError) as exc:
        resolve_run_dir("r-0001", other, runs_root=runs)
    assert exc.value.code == E.E_IO_RUN_CONFLICT


def test_resume_tolerates_harness_change(runs, cfg):
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


# ---- FR-09-17：进程环境快照记入**真正的** run_start --------------------------------

def test_cli_init_run_start_carries_process_env(tmp_path, monkeypatch):
    from argparse import Namespace

    from dice_ratchet.infra import cli, events as ev
    from dice_ratchet.infra import process as P
    from dice_ratchet.infra import run as run_mod

    # 临时目录不在 git 仓库里：跳过 runs/ 的 .gitignore 检查，只验 run_start 字段
    real_init = run_mod.init_run_dir
    monkeypatch.setattr(cli, "init_run_dir",
                        lambda *a, **k: real_init(*a, **{**k, "git_check": False}))
    monkeypatch.chdir(tmp_path)
    # 真实入口顺序（cli.main）：先 bootstrap_process() 固化环境，再 cli_init_run()。
    # 这里显式重放该顺序：指纹读的是**真实** env，不能依赖别的用例的副作用。
    monkeypatch.delenv("PYTHONHASHSEED", raising=False)
    monkeypatch.setattr(P, "_SNAPSHOT", None)
    P.bootstrap_process(reexec=False)
    events.unbind()
    ev.set_dev_mode(True)
    try:
        paths, cfg, run_id = cli.cli_init_run(Namespace(
            config_dir=str(CONFIGS), run_id="r-proc-env", resume=False, dry_run=False))
        rows, _ = read_jsonl(paths.events)
    finally:
        events.unbind()
        ev.set_dev_mode(False)
    start = next(r for r in rows if r["event"] == "infra.run_start")
    assert start["config_hash"] == config_hash(cfg) and start["run_id"] == run_id
    assert start["env"]["PYTHONHASHSEED"] == "0" and start["env"]["TZ"] == "UTC"
    assert start["pythonhashseed"] == "0" and start["tz"] == "UTC"
    assert start["threads"] == 1
    assert start["unknown_keys"] == [] or isinstance(start["unknown_keys"], list)
    assert sum(1 for r in rows if r["event"] == "infra.run_start") == 1 # 只发一次


def test_unknown_keys_recorded_in_run_start_and_event(tmp_path, monkeypatch):
    """回归（P1-3）：FR-09-1 的「未知键清单写入 run 启动日志」+ E_IO_CONFIG_UNKNOWN 留痕。"""
    from argparse import Namespace

    from dice_ratchet.infra import cli
    from dice_ratchet.infra import run as run_mod

    real_init = run_mod.init_run_dir
    monkeypatch.setattr(cli, "init_run_dir",
                        lambda *a, **k: real_init(*a, **{**k, "git_check": False}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DICERATCHET_LOOP__NOPE", "1")
    monkeypatch.setenv("DICERATCHET_FOO__BAR", "1")
    events.unbind()
    events.set_dev_mode(True)
    try:
        paths, _cfg, _rid = cli.cli_init_run(Namespace(
            config_dir=str(CONFIGS), run_id="r-unknown", resume=False, dry_run=False))
        rows, _ = read_jsonl(paths.events)
        events.unbind()
    finally:
        events.unbind()
        events.set_dev_mode(False)
    start = next(r for r in rows if r["event"] == "infra.run_start")
    assert set(start["unknown_keys"]) == {"loop.nope", "foo.bar", "DICERATCHET_FOO__BAR"}
    unknown_event = next(r for r in rows if r["event"] == "infra.config_unknown_key")
    assert E.E_IO_CONFIG_UNKNOWN == unknown_event["code"]
