"""PRD §7.4 / G6：``memory stats|snapshot|rollback|verify|retrieve`` 五条命令。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from dice_ratchet.infra.config import load_config
from dice_ratchet.infra.errors import E, InfraError, error_code_of
from dice_ratchet.infra.run import RunPaths
from dice_ratchet.memory import MemoryStore

from dice_ratchet.evals.seeds import coerce_protocol, validate_protocol
_PROTO = validate_protocol(coerce_protocol(None))
_HELDOUT_SEED = _PROTO.splits["heldout"].baskets["H1"].start + 1
from dice_ratchet.memory import cli as mem_cli

LESSON = "净化值低于 25% 时先把光盾骰放进防御槽，再考虑输出。"
OBS = {"phase": "player_turn", "stage": 3,
       "player": {"hearts": 1, "virtue": 4, "virtueMax": 20},
       "hand": [{"dieId": "d1", "isCorrupt": True}],
       "slots": [{"slotKind": "defense"}],
       "enemies": [{"school": "blade", "intent": [{"kind": "purge", "arrow": "down"}]}]}


def _ns(**kw) -> argparse.Namespace:
    base = {"config_dir": "configs", "run_id": "r-cli", "resume": False,
            "dry_run": False, "reason": "gen-000", "memory_version": None,
            "to_version": None, "full": False, "tick": None, "obs": None,
            "top_k": None, "token_budget": None, "tags": ""}
    base.update(kw)
    return argparse.Namespace(**base)


def _json_tail(text: str) -> dict:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith("{"))
    return json.loads("\n".join(lines[start:]))


@pytest.fixture
def wired(tmp_path, monkeypatch, config_dir):
    """把 ``cli_init_run`` 指向临时 run 目录（避免建真实 runs/ 与事件绑定）。"""
    cfg = load_config()
    monkeypatch.setattr(mem_cli.infra_cli, "cli_init_run",
                        lambda args, **kw: (RunPaths(tmp_path), cfg, "r-cli"))
    return tmp_path, cfg


@pytest.fixture
def seeded(wired, config_dir, make_candidate, make_prov):
    tmp_path, cfg = wired
    store = MemoryStore(tmp_path, cfg.memory, config_dir=config_dir, run_id="r-cli")
    store.add_memory(make_candidate(lesson=LESSON), provenance=make_prov())
    store.add_memory(make_candidate(lesson="地图路线优先走左侧商店。", action_hint=None,
                                    tags=("map_route",), reflection_id="refl-2"),
                     provenance=make_prov())
    store.snapshot(reason="gen-000")
    return tmp_path, cfg, store


# ---- stats ---------------------------------------------------------------------------

def test_cmd_stats_prints_json(wired, capsys) -> None:
    assert mem_cli.cmd_stats(_ns()) == 0
    payload = _json_tail(capsys.readouterr().out)
    assert payload["run_id"] == "r-cli"
    assert payload["entry_count"] == 0
    assert "dedup_rate" in payload and "memory_version" in payload


def test_cmd_stats_on_seeded_store(seeded, capsys) -> None:
    assert mem_cli.cmd_stats(_ns()) == 0
    payload = _json_tail(capsys.readouterr().out)
    assert payload["entry_count"] == 2
    assert payload["memory_version"] == 1
    assert payload["snapshots"] == [1]


# ---- snapshot ------------------------------------------------------------------------

def test_cmd_snapshot_writes_next_version(seeded, capsys) -> None:
    assert mem_cli.cmd_snapshot(_ns(reason="gen-001")) == 0
    payload = _json_tail(capsys.readouterr().out)
    assert payload["memory_version"] == 2
    assert payload["parent_memory_version"] == 1
    assert Path(payload["snapshot"]).name == "memory_v2.jsonl"
    assert Path(payload["snapshot"]).is_file()


def test_cmd_snapshot_explicit_version(seeded, capsys) -> None:
    assert mem_cli.cmd_snapshot(_ns(memory_version=9)) == 0
    payload = _json_tail(capsys.readouterr().out)
    assert payload["memory_version"] == 9


# ---- rollback ------------------------------------------------------------------------

def test_cmd_rollback_prints_prd_line(seeded, capsys) -> None:
    assert mem_cli.cmd_rollback(_ns(to_version=1)) == 0
    out = capsys.readouterr().out
    assert "memory_version: 1 -> 2" in out
    assert "restored from v1" in out
    payload = _json_tail(out)
    assert payload["target_version"] == 1 and payload["dry_run"] is False


def test_cmd_rollback_missing_target_exits_2(seeded) -> None:
    with pytest.raises(InfraError) as exc:
        mem_cli.cmd_rollback(_ns(to_version=42))
    assert exc.value.code == E.E_IO_SNAPSHOT_MISSING
    assert error_code_of(exc.value.code) == 2


def test_cmd_rollback_requires_to_version(seeded) -> None:
    with pytest.raises(InfraError) as exc:
        mem_cli.cmd_rollback(_ns(to_version=None))
    assert exc.value.code == E.E_IO_CONFIG_INVALID


def test_cmd_rollback_dry_run_keeps_state(seeded, capsys) -> None:
    before = Path(seeded[0] / "memory" / "memory_active.jsonl").read_bytes()
    assert mem_cli.cmd_rollback(_ns(to_version=1, dry_run=True)) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert Path(seeded[0] / "memory" / "memory_active.jsonl").read_bytes() == before


# ---- verify --------------------------------------------------------------------------

def test_cmd_verify_healthy_returns_0(seeded, capsys) -> None:
    assert mem_cli.cmd_verify(_ns(full=True, tick=5)) == 0
    payload = _json_tail(capsys.readouterr().out)
    assert payload["healthy"] is True
    assert payload["checked"] == 2


def test_cmd_verify_corrupt_returns_1(seeded, capsys) -> None:
    active = seeded[0] / "memory" / "memory_active.jsonl"
    active.write_text(active.read_text("utf-8") + '{"entry_id": "broken"\n', encoding="utf-8")
    assert mem_cli.cmd_verify(_ns(full=False)) == 1
    payload = _json_tail(capsys.readouterr().out)
    assert payload["healthy"] is False and payload["bad_lines"] == 1


def test_cmd_verify_full_detects_leak(seeded) -> None:
    store = MemoryStore(seeded[0], seeded[1].memory, config_dir=Path("configs"),
                        run_id="r-cli")
    entry = store.entries()[0]
    leaked = entry.with_updates(
        lesson=f"第 {_HELDOUT_SEED} 局的清扫要先挡。")
    store._replace(leaked)    # 刷新行缓存（模拟旧快照载入）
    store._compact()          # 落到 active 文件，供新进程/新 store 复检（I3）
    with pytest.raises(InfraError) as exc:
        mem_cli.cmd_verify(_ns(full=True, tick=5))
    assert exc.value.code == E.E_EVAL_INV5_LEAK


# ---- retrieve ------------------------------------------------------------------------

def test_cmd_retrieve_uses_inline_obs(seeded, capsys) -> None:
    args = _ns(obs=json.dumps(OBS), tick=6)
    assert mem_cli.cmd_retrieve(args) == 0
    out = capsys.readouterr().out
    assert LESSON[:8] in out
    payload = _json_tail(out)
    assert payload["entries"] and payload["tokens"] > 0
    assert payload["entries"][0]["text"].startswith("[")


def test_cmd_retrieve_reads_obs_file(seeded, tmp_path, capsys) -> None:
    obs_file = tmp_path / "obs.json"
    obs_file.write_text(json.dumps(OBS), encoding="utf-8")
    assert mem_cli.cmd_retrieve(_ns(obs=str(obs_file), tick=6)) == 0
    assert _json_tail(capsys.readouterr().out)["entries"]


def test_cmd_retrieve_without_obs_and_with_tags(seeded, capsys) -> None:
    assert mem_cli.cmd_retrieve(_ns(tags="combat,low_virtue", top_k=1,
                                    token_budget=200, tick=6)) == 0
    payload = _json_tail(capsys.readouterr().out)
    assert len(payload["entries"]) <= 1


def test_cmd_retrieve_rejects_bad_obs(seeded) -> None:
    with pytest.raises(InfraError) as exc:
        mem_cli.cmd_retrieve(_ns(obs="{not json"))
    assert exc.value.code == E.E_IO_CONFIG_INVALID


# ---- 端到端：五条命令在空 run 上可直接执行（G6）---------------------------------------

def test_five_commands_end_to_end_on_empty_run(tmp_path, monkeypatch, config_dir) -> None:
    from dice_ratchet import app

    monkeypatch.setenv("DICERATCHET_INFRA__RUNS_ROOT", str(tmp_path / "runs"))
    run_id = "r-e2e"
    common = ["--run-id", run_id, "--config-dir", str(config_dir)]

    assert app.main(["memory", "stats", *common], reexec=False) == 0
    assert app.main(["memory", "snapshot", *common, "--reason", "gen-000"], reexec=False) == 0
    assert app.main(["memory", "verify", *common, "--full"], reexec=False) == 0
    assert app.main(["memory", "retrieve", *common, "--obs", json.dumps(OBS),
                     "--tick", "3"], reexec=False) == 0
    assert app.main(["memory", "rollback", *common, "--to-version", "1",
                     "--dry-run"], reexec=False) == 0
    assert app.main(["memory", "rollback", *common, "--to-version", "1"],
                    reexec=False) == 0
    assert app.main(["memory", "rollback", *common, "--to-version", "1"],
                    reexec=False) == 0
    # 目标不存在 → 退出码 2（E_IO_SNAPSHOT_MISSING）
    assert app.main(["memory", "rollback", *common, "--to-version", "99"],
                    reexec=False) == 2

    run_dir = tmp_path / "runs" / run_id
    assert (run_dir / "memory" / "index.json").is_file()
    assert (run_dir / "memory" / "memory_v1.jsonl").is_file()
    assert (run_dir / "memory" / "memory_v2.jsonl").is_file()  # rollback 落新版本
    assert (run_dir / "memory" / "memory_v3.jsonl").is_file()  # 第二次 rollback 也落新版本
    index = json.loads((run_dir / "memory" / "index.json").read_text("utf-8"))
    assert index["memory_version"] == 3
    assert index["parent_memory_version"] == 1
    assert [row["memory_version"] for row in index["lineage"]] == [1, 2, 3]


def test_memory_subcommands_registered_after_wire() -> None:
    from dice_ratchet import app
    from dice_ratchet.infra import cli

    app.wire()
    for name in ("stats", "snapshot", "rollback", "verify", "retrieve"):
        assert cli.handler_for(["memory", name]) is not None
