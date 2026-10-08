"""GAME golden 对拍（MOD-01 PRD §8 / G2 / T-04）。

逐行重放 ``src/game/tests/golden/*.jsonl``（JS 侧 ``scripts/golden-gen.js`` 生成，
Python 侧只读），比对每一步的 ``obsDigest`` / ``rngDigest`` / ``stateDigest`` 与
``illegal`` 标记。任一行不等即失败，并打印首个分歧点。

首个分歧的详细定位用 ``--dump-state`` 从 JS 侧导出同一步全状态（GAME-01 §7）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from dice_ratchet.env import GameEngine, digest, load_tables

# golden 由 JS 侧生成，相对仓库根定位（支持从仓库根或 tests/ 下运行）
_REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = Path(_REPO_ROOT / "src/game/tests/golden")
GOLDEN_FILES = sorted(GOLDEN_DIR.glob("*.jsonl")) if GOLDEN_DIR.is_dir() else []


def _read_golden(path: Path) -> tuple[dict, list[dict]]:
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    meta = lines[0]
    steps = [x for x in lines[1:] if x.get("type") not in ("run_end",)]
    return meta, steps


@pytest.fixture(scope="module")
def tables():
    return load_tables()


@pytest.fixture(scope="module")
def engine(tables):
    return GameEngine(tables)


@pytest.mark.parametrize("golden_file", GOLDEN_FILES, ids=lambda p: p.stem)
def test_golden_replay(engine: GameEngine, golden_file: Path) -> None:
    meta, steps = _read_golden(golden_file)
    state = engine.create(meta["config"])

    first = steps[0]
    assert digest(engine.observe(state)) == first["obsDigest"], f"{golden_file.name}: step 0 obsDigest"
    assert digest(state["rng"]) == first["rngDigest"], f"{golden_file.name}: step 0 rngDigest"
    assert digest(state) == first["stateDigest"], f"{golden_file.name}: step 0 stateDigest"

    for step in steps[1:]:
        action = step["action"]
        r = engine.reduce(state, action)
        assert r["illegal"] == step["illegal"], (
            f"{golden_file.name} step {step['step']}: illegal mismatch "
            f"(py={r['illegal']} golden={step['illegal']}) action={action}")
        state = r["state"]
        got = (digest(engine.observe(state)), digest(state["rng"]), digest(state))
        want = (step["obsDigest"], step["rngDigest"], step["stateDigest"])
        if got != want:
            which = ["obsDigest", "rngDigest", "stateDigest"][got.index(next(g for g, w in zip(got, want) if g != w))]
            pytest.fail(
                f"{golden_file.name} step {step['step']}: {which} 分歧\n"
                f"  action: {json.dumps(action, ensure_ascii=False)}\n"
                f"  golden: {want}\n  python: {got}\n"
                f"  定位：node src/game/scripts/golden-gen.js --dump-state "
                f"{golden_file.stem} --step {step['step']}")


# ---- MOD-01 PRD T-08 / MOD-09 PRD G3：跨 PYTHONHASHSEED × TZ 的子进程复跑 -------------
#
# PRD 原文：「给定 子进程 PYTHONHASHSEED=0/12345、TZ=UTC/Asia/Tokyo，当 跑 T-04，
# 则 四种组合全部通过」。本用例把四种组合各跑一次完整 5 组 golden 逐行对拍，并断言
# 四者产出的摘要序列**逐字节相同**（跨进程 + 跨 hash seed + 跨时区都不影响结果）。
# 由 M0 验收报告 F6 补齐：此前只在验收脚本里临时跑过，未落为回归用例。

#: 子进程脚本：逐行重放 5 组 golden，把所有摘要哈希成一个 sha256 打到 stdout。
_T08_CHILD = """import hashlib, json, os, sys
from pathlib import Path

root = Path(os.environ["DICERATCHET_REPO_ROOT"])
sys.path.insert(0, str(root))
from dice_ratchet.env import GameEngine, digest, load_tables  # noqa: E402

engine = GameEngine(load_tables())
h = hashlib.sha256()
for path in sorted((root / "src/game/tests/golden").glob("*.jsonl")):
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    meta = lines[0]
    steps = [x for x in lines[1:] if x.get("type") != "run_end"]
    state = engine.create(meta["config"])
    first = steps[0]
    assert digest(engine.observe(state)) == first["obsDigest"], path.name + " step 0 obsDigest"
    assert digest(state["rng"]) == first["rngDigest"], path.name + " step 0 rngDigest"
    assert digest(state) == first["stateDigest"], path.name + " step 0 stateDigest"
    h.update((path.name + first["obsDigest"] + first["rngDigest"] + first["stateDigest"]).encode())
    for step in steps[1:]:
        r = engine.reduce(state, step["action"])
        assert r["illegal"] == step["illegal"], f"{path.name} step {step['step']}: illegal"
        state = r["state"]
        got = (digest(engine.observe(state)), digest(state["rng"]), digest(state))
        want = (step["obsDigest"], step["rngDigest"], step["stateDigest"])
        assert got == want, f"{path.name} step {step['step']}: 摘要分歧"
        h.update(("".join(got)).encode())
print(h.hexdigest())
"""


@pytest.fixture(scope="module")
def t08_subprocess_digests(tmp_path_factory) -> dict[tuple[str, str], str]:
    """四种 (PYTHONHASHSEED, TZ) 组合各跑一次子进程，返回 sha256。"""
    script = tmp_path_factory.mktemp("t08") / "replay.py"
    script.write_text(_T08_CHILD, encoding="utf-8")
    env_base = {**os.environ, "DICERATCHET_REPO_ROOT": str(_REPO_ROOT),
                "PYTHONPATH": str(_REPO_ROOT)}
    out: dict[tuple[str, str], str] = {}
    for hashseed in ("0", "12345"):
        for tz in ("UTC", "Asia/Tokyo"):
            proc = subprocess.run([sys.executable, str(script)], cwd=_REPO_ROOT,
                                  env={**env_base, "PYTHONHASHSEED": hashseed, "TZ": tz},
                                  capture_output=True, text=True, timeout=600)
            assert proc.returncode == 0, (
                f"PYTHONHASHSEED={hashseed} TZ={tz} 下 T-04 失败：\n{proc.stdout}\n{proc.stderr}")
            out[(hashseed, tz)] = proc.stdout.strip()
    return out


def test_t08_golden_replay_identical_across_hashseed_and_tz(t08_subprocess_digests):
    """T-08：四种组合的摘要序列逐字节相同，且非空（防脚本空跑）。"""
    got = t08_subprocess_digests
    assert len(got) == 4
    assert all(len(v) == 64 for v in got.values()), f"子进程摘要异常：{got}"
    assert len(set(got.values())) == 1, f"跨 PYTHONHASHSEED/TZ 出现分歧：{got}"
