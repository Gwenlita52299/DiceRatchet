"""``python -m gamer_rsi env selftest``（PRD G6 / FR-01-9）。

环境层落地自检：不依赖 pytest，可直接在 CI 或本地复制执行：

1. 配表加载 + hash + KERNEL_VERSION 一致性
2. 确定性自检：同一 seed 两次 create/reduce 得到相同 digest（写时构造不泄漏）
3. 默认局冒烟：stage 1 开打 + 不变式 + legalActions
4. 守卫自检（只扫 env 目录，秒级）
5. golden 烟雾对拍（若 node 与 golden 文件可用；全量对拍见 ``pytest tests/golden -q``）

任何一项失败返回退出码 1，全部通过返回 0。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .digest import digest
from .engine import GameEngine, KERNEL_VERSION, OBS_SCHEMA, apply_difficulty
from .error import KernelError
from .loader import GAME_CONTENT_DIR, load_tables
from .state import assert_invariants

__all__ = ["run_selftest"]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GOLDEN_DIR = _REPO_ROOT / "src/game/tests/golden"


def run_selftest(seed: int = 1) -> int:
    print(f"gamer_rsi env selftest --seed {seed}")
    failures: list[str] = []

    # ---- 1. 配表加载 ------------------------------------------------------
    print("[1/5] 配表加载与 hash")
    try:
        tables = load_tables()
        engine = GameEngine(tables)
        assert tables["schemaVersion"] and KERNEL_VERSION
        print(f"  ✔ load_tables OK（{len(tables)} 张表，kernel={KERNEL_VERSION}，"
              f"content={GAME_CONTENT_DIR.name}）")
    except Exception as exc:  # noqa: BLE001
        return _report([f"load_tables 失败：{exc!r}"])

    # ---- 2. 确定性 --------------------------------------------------------
    print("[2/5] 确定性自检（同 seed 双跑）")
    try:
        cfg = {"difficulty": "hard", "seed": seed, "toggles": {}}
        a, b = engine.create(cfg), engine.create(cfg)
        assert digest(a) == digest(b), "两次 create digest 不一致"
        # 非法动作：原对象返回 + 零副作用
        illegal = engine.reduce(a, {"kind": "play_face", "dieId": "d-nope", "slotId": "slot_1"})
        assert illegal["illegal"] is True and illegal["state"] is a, "非法动作未原对象返回"
        before = digest(a)
        assert digest(a) == before, "非法动作改变了 state"
        # 合法动作：写时拷贝（新对象），但不得修改入参
        first = next(x for x in engine.legal_actions(a) if x["kind"] != "abandon_run")
        ra = engine.reduce(a, first)
        assert ra["state"] is not a, "合法动作未产生新 state（写时构造语义）"
        assert digest(a) == before, "reduce 修改了入参 state（违反 K-1）"
        rb = engine.reduce(b, first)
        assert digest(ra["state"]) == digest(rb["state"]), "同输入双跑 reduce 漂移"
        print("  ✔ create/reduce 确定性 + 写时拷贝 + 非法动作零副作用")
    except AssertionError as exc:
        failures.append(f"确定性自检失败：{exc}")
    except KernelError as exc:
        failures.append(f"确定性自检抛 KernelError({exc.code})：{exc}")

    # ---- 3. 默认局冒烟 ------------------------------------------------------
    print("[3/5] 默认局冒烟（hard）")
    try:
        state = engine.create(apply_difficulty({"seed": seed}, "hard", tables))
        first = next(x for x in engine.legal_actions(state) if x["kind"] == "map_choose")
        state = engine.reduce(state, first)["state"]
        assert state["phase"] == "player_turn", f"进入战斗失败：phase={state['phase']}"
        assert_invariants(state)
        legal = engine.legal_actions(state)
        assert legal and "abandon_run" in [x["kind"] for x in legal]
        assert engine.observe(state)["schemaVersion"] == OBS_SCHEMA
        print(f"  ✔ stage 1 开打：phase=player_turn，{len(legal)} 个合法动作，不变式全部成立")
    except (AssertionError, KernelError) as exc:
        failures.append(f"默认局冒烟失败：{exc!r}")

    # ---- 4. 守卫自检 ------------------------------------------------------
    print("[4/5] 守卫自检（env 分层与确定性）")
    try:
        from gamer_rsi.infra import guards as g

        env_dir = Path(__file__).resolve().parent
        findings = [f for f in g.scan_all(env_dir) if not f.exempted]
        if findings:
            failures.extend(f"{Path(f.path).name}:{f.line} {f.code} {f.message}" for f in findings)
        else:
            print("  ✔ gamer_rsi/env 守卫干净（INV-1 分层 / INV-6 确定性 / P-14 无外部源）")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"守卫自检异常：{exc!r}")

    # ---- 5. golden 烟雾对拍 ------------------------------------------------------
    print("[5/5] golden 烟雾对拍（JS/Python）")
    if not shutil.which("node"):
        print("  · 跳过：node 不可用（golden 对拍需 JS 参照）")
    elif not _GOLDEN_DIR.exists():
        print(f"  · 跳过：golden 目录不存在（{_GOLDEN_DIR}）")
    else:
        try:
            ok, steps = _golden_smoke(engine)
            if ok:
                print(f"  ✔ golden 烟雾对拍通过（g1 前 {steps} 步；"
                      f"全量对拍见 `pytest tests/golden -q`）")
            else:
                failures.append(f"golden 烟雾对拍失败（第 {steps} 步即分歧）")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"golden 对拍异常：{exc!r}")

    return _report(failures)


def _golden_smoke(engine: GameEngine, max_steps: int = 60) -> tuple[bool, int]:
    """对 g1 前 max_steps 步做快速摘要对拍（全量对拍交给 pytest）。"""
    path = _GOLDEN_DIR / "g1_all_off_medium.jsonl"
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    if lines[0].get("kernelVersion") and lines[0]["kernelVersion"] != KERNEL_VERSION:
        print(f"  ✗ golden kernelVersion({lines[0]['kernelVersion']}) 与 Python({KERNEL_VERSION}) 不一致")
        return False, 0
    state = engine.create(lines[0]["config"])
    steps = 0
    for line in lines[1:]:
        if line.get("type") == "run_end":
            break
        state = engine.reduce(state, line["action"])["state"]
        steps += 1
        got = (digest(engine.observe(state)), digest(state["rng"]), digest(state))
        if got != (line["obsDigest"], line["rngDigest"], line["stateDigest"]):
            print(f"  ✗ step {line['step']} 摘要分歧：action={line['action']}")
            print(f"    golden={(line['obsDigest'], line['rngDigest'], line['stateDigest'])}")
            print(f"    python={got}")
            return False, steps
        if steps >= max_steps:
            break
    return True, steps


def _report(failures: list[str]) -> int:
    print()
    if failures:
        print(f"selftest 失败（{len(failures)} 项）：")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("selftest 全部通过")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gamer_rsi env selftest", description="环境层自检（PRD G6）")
    p.add_argument("--seed", type=int, default=1)
    return run_selftest(p.parse_args(argv).seed)


if __name__ == "__main__":
    sys.exit(main())
