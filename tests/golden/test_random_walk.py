"""JS/Python 差分对拍（MOD-01 成功度量②）：N 局随机动作 0 分歧。

由 JS 侧 ``scripts/golden-gen.js --random-walk N`` 生成随机动作序列（含定期注入的
非法动作探测），Python 侧只读重放并逐步比对 ``obsDigest`` / ``rngDigest`` /
``stateDigest`` 与 ``illegal``。golden（①）覆盖固定脚本，本测试覆盖随机路径。

需要 ``node``；无 node 时跳过。默认 200 局（PRD 度量②），可用
``GAMERSI_PARITY_WALKS`` 覆盖；``@pytest.mark.slow``。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from gamer_rsi.env import GameEngine, digest, load_tables

_REPO_ROOT = os.environ.get("GAMERSI_REPO_ROOT") or str(__import__("pathlib").Path(__file__).resolve().parents[2])
GEN = os.path.join(_REPO_ROOT, "src/game/scripts/golden-gen.js")
WALKS = int(os.environ.get("GAMERSI_PARITY_WALKS", "200"))


def _node_available() -> bool:
    return shutil.which("node") is not None


@pytest.fixture(scope="module")
def walk_episodes(tmp_path_factory):
    if not _node_available() or not os.path.exists(GEN):
        return []
    out = tmp_path_factory.mktemp("random-walk")
    res = subprocess.run(
        ["node", GEN, "--random-walk", str(WALKS), "--out", str(out), "--seed-base", "50000"],
        capture_output=True, text=True, cwd=_REPO_ROOT, timeout=1800,
    )
    if res.returncode != 0:
        pytest.fail(f"golden-gen.js --random-walk 失败：\n{res.stdout}\n{res.stderr}")
    return sorted(p for p in out.glob("rw-*.jsonl"))


@pytest.mark.slow
def test_random_walk_parity(walk_episodes) -> None:
    if not walk_episodes:
        pytest.skip("node 不可用或 golden-gen.js 缺失，跳过 JS/Python 差分对拍")
    tables = load_tables()
    engine = GameEngine(tables)
    total = 0
    for path in walk_episodes:
        lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        meta, steps = lines[0], [x for x in lines[1:] if x.get("type") not in ("run_end",)]
        state = engine.create(meta["config"])
        assert digest(engine.observe(state)) == steps[0]["obsDigest"], f"{path.name} step 0"
        for step in steps[1:]:
            r = engine.reduce(state, step["action"])
            assert r["illegal"] == step["illegal"], (
                f"{path.name} step {step['step']}: illegal 分歧 {step['action']}")
            state = r["state"]
            got = (digest(engine.observe(state)), digest(state["rng"]), digest(state))
            want = (step["obsDigest"], step["rngDigest"], step["stateDigest"])
            assert got == want, (
                f"{path.name} step {step['step']}: 摘要分歧 action={json.dumps(step['action'], ensure_ascii=False)}")
            total += 1
    print(f"\n差分对拍通过：{len(walk_episodes)} 局 / {total} 步")
