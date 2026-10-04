"""显式重新生成 MOD-03 robustness 的真实固定 prompt 快照（M1 验收 F2）。

用法（在仓库根执行）::

    .venv/bin/python tests/golden/inference/regen_robustness_prompts.py

产出：``tests/fixtures/inference/robustness_prompts.jsonl``（每行一条 prompt）：

- 内容 = **MOD-04 在真实 ``SandboxEnv`` 观测上组装出的决策 prompt**（``INV-3`` 的唯一
  注入出口 ``PolicyAgent.prompt_bundle()``），即 MOD-03 真实入参的逐字节快照；
- 种子来自 **train** split（INV-5：heldout/test 不得出现在任何 agent 可见出口）；
- 生成时对每条 prompt 跑一遍泄漏扫描，出现任何 finding 即拒绝写盘（守卫不能空转）。

生成命令与 PRD §11 的「200 条真实固定 prompt（MOD-04 决策模板快照）」一一对应；
``load_robustness_prompts()`` 的 ``prompt_source`` 由 ``synthetic:fallback`` 变为
``file:tests/fixtures/inference/robustness_prompts.jsonl``。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
for entry in (str(REPO_ROOT),):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from gamer_rsi.agent.heuristic import heuristic_policy  # noqa: E402
from gamer_rsi.agent.policy import EpisodeContext, PolicyAgent  # noqa: E402
from gamer_rsi.infra.config import load_config  # noqa: E402
from gamer_rsi.sandbox.api import EpisodeConfig, make_default_env  # noqa: E402
from gamer_rsi.sandbox.audit import (VisibleSet, build_forbidden,  # noqa: E402
                                     load_forbidden_seeds, scan_text)

OUT = REPO_ROOT / "tests/fixtures/inference/robustness_prompts.jsonl"
CONFIG_DIR = REPO_ROOT / "configs"
#: 目标条数（PRD §11 / Spec §8.6 的固定 prompt 集规模）。
N_PROMPTS = 200
#: 采集用的训练 seed（train split，INV-5 允许 agent 可见）。
TRAIN_SEEDS = tuple(range(1000, 1020))


def _collect() -> tuple[list[dict[str, object]], int]:
    cfg = load_config(config_dir=CONFIG_DIR)
    agent = PolicyAgent(cfg.agent.policy, harness_dir=REPO_ROOT / "harness")
    episode_cfg = EpisodeConfig.from_any(getattr(cfg.env, "episode", None))
    budget = int(getattr(cfg.agent.policy, "prompt_token_budget", 3072))
    seen: set[str] = set()
    rows: list[dict[str, object]] = []
    oversize = 0
    for seed in TRAIN_SEEDS:
        if len(rows) >= N_PROMPTS:
            break
        env = make_default_env(episode_config=episode_cfg)
        try:
            obs = env.reset(int(seed))
            ctx = EpisodeContext(run="regen-robustness", episode=f"e-{seed:06d}-g000-train",
                                 seed=int(seed), split="train", mode="play", tick=int(seed))
            agent.reset(ctx)
            while not obs.get("outcome") and len(rows) < N_PROMPTS:
                legal = env.legal_actions()
                if not legal:
                    break
                bundle = agent.prompt_bundle(obs, ctx=ctx, legal=legal)
                if bundle.prompt_hash in seen:
                    pass
                elif bundle.token_estimate > budget:
                    # 超预算的 prompt 不属于「模型真会收到的固定 prompt 集」：真实 8B 的
                    # context 只有 8192，这类观测会被 E_LLM_CONTEXT_OVERFLOW 拒掉。
                    # 见 M1 验收报告 F13（观测块未被 prompt_token_budget 裁剪）。
                    oversize += 1
                else:
                    seen.add(bundle.prompt_hash)
                    rows.append({
                        "prompt_hash": bundle.prompt_hash,
                        "template_hash": bundle.template_hash,
                        "token_estimate": bundle.token_estimate,
                        "seed": int(seed),
                        "decision_index": int(obs.get("decisionIndex") or 0),
                        "messages": [{"role": str(m.role), "content": str(m.content)}
                                     for m in bundle.messages],
                    })
                step = env.step(heuristic_policy(obs, legal, env.hints()))
                obs = step.observation
                if step.terminated or step.truncated:
                    break
        finally:
            env.close()
    return rows, oversize


def _assert_no_leak(rows: list[dict[str, object]]) -> None:
    """每条 prompt 都必须干净（否则拒绝写盘：不能把泄漏面固化进 fixture）。"""
    forbidden = build_forbidden(
        VisibleSet(obs_paths=(), obs_digests=(), memory_texts=(), harness_version=0,
                   seeds_seen=()),
        seeds=load_forbidden_seeds(CONFIG_DIR))
    findings = []
    for index, row in enumerate(rows):
        text = "\n".join(str(m["content"]) for m in row["messages"])  # type: ignore[index]
        findings.extend(scan_text(text, forbidden=forbidden, surface="prompt",
                                  episode_id=str(row["prompt_hash"]), step=index))
    if findings:
        sample = [(f.leak_class, f.evidence) for f in findings[:5]]
        raise SystemExit(f"拒绝写盘：{len(findings)} 条 prompt 触发泄漏扫描，样例={sample}")
    print(f"泄漏扫描：{len(rows)} 条 prompt 全部 clean")


def main() -> int:
    rows, oversize = _collect()
    if len(rows) < N_PROMPTS:
        raise SystemExit(f"只采集到 {len(rows)} 条预算内唯一 prompt（需要 {N_PROMPTS}），"
                         f"请扩大 TRAIN_SEEDS")
    rows = rows[:N_PROMPTS]
    _assert_no_leak(rows)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"wrote {OUT} ({len(rows)} prompts)")
    if oversize:
        print(f"注意：另有 {oversize} 个决策点的 prompt 超出 prompt_token_budget，已跳过"
              f"（见 M1 验收报告 F13：观测块未被预算裁剪）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
