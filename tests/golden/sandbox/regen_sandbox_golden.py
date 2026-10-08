"""显式重新生成 MOD-02 sandbox golden（G2：改动必须显式更新并说明）。

用法（在仓库根执行）::

    .venv/bin/python tests/golden/sandbox/regen_sandbox_golden.py

产出：

- ``g5_obs_agent.txt``：g5 默认局逐步 ``obs_digest`` 序列（agent 侧投影，G3）
- ``g5_render.txt``：首个 ``player_turn`` 的 ``render_compact`` 输出（FR-02-3b）
- ``leak-report-clean.json``：干净自测 run 的审计报告（NFR-02-3；除 ``generated_at``）

golden 只在**显式**运行本脚本时更新；测试只读比对。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from conftest import build_sandbox_run  # noqa: E402

from dice_ratchet.env import GameEngine, load_tables  # noqa: E402
from dice_ratchet.sandbox.api import EpisodeConfig, SandboxEnv  # noqa: E402
from dice_ratchet.sandbox.audit import run_audit  # noqa: E402
from dice_ratchet.sandbox.projection import render_compact  # noqa: E402

OUT = Path(__file__).resolve().parent
FIXED_TS = "2026-09-30T12:00:00Z"
CLEAN_RUN_ID = "sandbox-golden-clean"


def replay_g5() -> tuple[list[str], str]:
    """用 g5 的 JS 动作脚本驱动 SandboxEnv，返回 (digest 序列, 首个 player_turn 渲染)。"""
    lines = [json.loads(x) for x in
             (REPO / "src/game/tests/golden/g5_hard_six_stage.jsonl")
             .read_text(encoding="utf-8").splitlines() if x.strip()]
    first = lines[0]
    cfg = first["config"]
    env = SandboxEnv(engine=GameEngine(load_tables()),
                     episode_config=EpisodeConfig(
                         difficulty=cfg["difficulty"], stage_count=cfg["stageCount"],
                         toggles=cfg["toggles"], character=cfg.get("character", "warrior")),
                     episode_id="e-000001-g000-train")
    env.reset(cfg["seed"])
    digests = [env.last_obs_digest()]
    render = ""
    for line in lines[1:]:
        if line.get("type") == "run_end":
            break
        env.step(line["action"])
        digests.append(env.last_obs_digest())
        if not render and env.observe()["phase"] == "player_turn":
            render = render_compact(env.observe())
    env.close()
    return digests, render


def main() -> int:
    import tempfile

    digests, render = replay_g5()
    (OUT / "g5_obs_agent.txt").write_text("\n".join(digests) + "\n", encoding="utf-8")
    (OUT / "g5_render.txt").write_text(render, encoding="utf-8")

    with tempfile.TemporaryDirectory() as tmp:
        built = build_sandbox_run(Path(tmp), run_id=CLEAN_RUN_ID)
        result = run_audit(built.root, generated_at=FIXED_TS)
        report = result.to_dict()
        report["run_id"] = CLEAN_RUN_ID
    (OUT / "leak-report-clean.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    print(f"wrote {len(digests)} digests, render {len(render)} chars, "
          f"verdict={report['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
