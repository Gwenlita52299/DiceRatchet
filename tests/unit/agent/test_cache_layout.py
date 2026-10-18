"""前缀缓存重排（2026-10-17 缓存修复）的纯文本契约。"""

from __future__ import annotations

from pathlib import Path

import pytest

from dice_ratchet.agent.prompts.cache_layout import (CacheLayoutError, is_reordered,
                                                     reorder_for_prefix_cache)

REPO = Path(__file__).resolve().parents[3]
LIVE = REPO / "harness" / "prompts" / "decision.md"


def _synthetic() -> str:
    return (
        "# 模板头\n\n"
        "## 任务说明\n{{task}}\n\n"
        "## 运行环境\n- harness_version: {{harness_version}}\n{{tools}}\n\n"
        "## 当前观测（唯一事实来源）\n{{observation}}\n\n"
        "## 回合预算（先读再决策）\n- 先斩杀。\n\n"
        "## 当前可选动作（legal_actions）\n{{legal_actions}}\n\n"
        "## 近期反思（新→旧）\n{{reflections}}\n\n"
        "## 策略记忆（按相关度排序）\n{{memory}}\n\n"
        "## 约束与纠错\n{{constraints}}\n\n"
        "## 输出契约\n{\"reasoning\": \"x\"}\n"
        "5. 所有 id 必须逐字来自上面的观测或可选动作。\n"
    )


def test_reorder_moves_static_blocks_before_dynamic_blocks():
    out = reorder_for_prefix_cache(_synthetic())
    order = [out.index(name) for name in (
        "## 任务说明", "## 运行环境", "## 回合预算", "## 输出契约",
        "## 当前观测", "## 当前可选动作", "## 近期反思", "## 策略记忆", "## 约束与纠错",
        "## 最终提醒",
    )]
    assert order == sorted(order)
    assert "下面的观测" in out
    # 契约段（观测之前）用「下面的观测」；尾部提醒（观测之后）保留「上面的观测」
    assert out.index("下面的观测") < out.index("{{observation}}")
    assert out.index("上面的观测") > out.index("{{constraints}}")
    # 所有 INV-3 占位符都还在（重排不丢上下文）
    for placeholder in ("{{task}}", "{{harness_version}}", "{{tools}}", "{{observation}}",
                        "{{legal_actions}}", "{{reflections}}", "{{memory}}",
                        "{{constraints}}"):
        assert placeholder in out
    assert out.endswith("不要自创。\n")


def test_reorder_is_idempotent():
    once = reorder_for_prefix_cache(_synthetic())
    twice = reorder_for_prefix_cache(once)
    assert once == twice
    assert is_reordered(once) is True
    assert is_reordered(_synthetic()) is False


def test_reorder_rejects_unknown_or_missing_sections():
    with pytest.raises(CacheLayoutError):
        reorder_for_prefix_cache("# 头\n\n## 任务说明\n{{task}}\n")
    with pytest.raises(CacheLayoutError):
        reorder_for_prefix_cache(_synthetic() + "\n## 未知段\nx\n")


@pytest.mark.skipif(not LIVE.is_file(), reason="live decision.md 不在仓库")
def test_live_template_reorders_and_keeps_contract():
    source = LIVE.read_text(encoding="utf-8")
    out = reorder_for_prefix_cache(source)
    for name in ("任务说明", "运行环境", "输出契约", "当前观测",
                 "当前可选动作", "近期反思", "策略记忆", "约束与纠错"):
        assert f"## {name}" in out
    # 回合预算只是 P1 起才有的可选静态段；v4 是裸 v0，不应要求它存在
    if "## 回合预算" in source:
        assert out.index("## 回合预算") < out.index("{{observation}}")
    assert '{"reasoning"' in out
    assert "5. 所有 id 必须逐字来自下面的观测或可选动作" in out
    assert out.index("## 输出契约") < out.index("{{observation}}")
    assert out.index("{{observation}}") < out.index("{{legal_actions}}")
    assert "下面的观测" in out
    assert "## 最终提醒" in out
