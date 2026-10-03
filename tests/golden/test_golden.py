"""GAME golden 对拍（MOD-01 PRD §8 / G2 / T-04）。

逐行重放 ``src/game/tests/golden/*.jsonl``（JS 侧 ``scripts/golden-gen.js`` 生成，
Python 侧只读），比对每一步的 ``obsDigest`` / ``rngDigest`` / ``stateDigest`` 与
``illegal`` 标记。任一行不等即失败，并打印首个分歧点。

首个分歧的详细定位用 ``--dump-state`` 从 JS 侧导出同一步全状态（GAME-01 §7）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gamer_rsi.env import GameEngine, digest, load_tables

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
