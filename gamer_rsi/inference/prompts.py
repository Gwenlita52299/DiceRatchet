"""固定 prompt 的来源（probe / bench / robustness）。

M1 的真实 200 条固定 prompt 应来自 **MOD-04 决策模板快照**（PRD §11）。MOD-04 未落地前，
本模块提供确定性合成兜底：同 ``n`` 必得同一组 prompt（INV-6），并如实记录来源
（``format_robustness.json#prompt_source``）。真实快照落地后只需把 JSONL 放到
``tests/fixtures/inference/robustness_prompts.jsonl``（或经 ``--prompts`` 指定）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final, Sequence

from .backend import Message

__all__ = ["DEFAULT_PROMPTS_PATH", "load_robustness_prompts", "probe_messages",
           "synthetic_prompts", "PROBE_MESSAGES"]

#: 默认快照路径（JSONL，每行 ``{"messages": [{"role": ..., "content": ...}, ...]}``）。
DEFAULT_PROMPTS_PATH: Final[str] = "tests/fixtures/inference/robustness_prompts.jsonl"

#: probe/bench 的固定单轮消息（确定性，不依赖环境）。
#:
#: 2026-10-04（M1 验收 F16）：这里必须**同时**给出输出契约与可选动作清单，否则真实 8B
#: 只能瞎猜 ``kind``：先后实测到 ``plan`` 被写成一段自然语言（首答 100% 校验失败）与
#: ``kind`` 编造成 ``use_card`` / ``attack``（3 次尝试全部失败）。真实 MOD-04 prompt
#: 会带上 ``legal_actions`` 原文，模型只需照抄——probe/bench 也必须等价，否则量到的是
#: 「提示词欠约束」，不是「模型格式鲁棒性」。
PROBE_MESSAGES: Final[tuple[Message, ...]] = (
    Message(role="system",
            content="你是《破咒骰》的决策器。只输出一个 JSON 对象，字段为 reasoning（≤200 字）与 "
                    "plan（1..12 个动作，每个动作至少含 kind）。kind 只能取上面所列的"
                    "legal_actions 里出现过的值，禁止自创。"),
    Message(role="user",
            content=("phase=player_turn stage=1 hand=[d1,d2] slots=[s1,s2] enemies=[e1]\n"
                     'legal_actions=[{"kind":"play_face","dieId":"d1","slotId":"s1","targetId":"e1"},'
                     '{"kind":"play_face","dieId":"d2","slotId":"s2"},'
                     '{"kind":"discard_die","dieId":"d1"},'
                     '{"kind":"end_turn"}]\n'
                     "请给出下一步动作计划。")),
)

_SYSTEM: Final[str] = (
    "你是《破咒骰》的决策器。只输出一个 JSON 对象，字段为 reasoning（≤200 字）与 "
    "plan（1..12 个动作，每个动作至少含 kind）。"
)


def probe_messages() -> list[Message]:
    """probe/bench 用的固定消息列表。"""
    return list(PROBE_MESSAGES)


def synthetic_prompts(n: int) -> list[list[Message]]:
    """确定性合成 ``n`` 条 prompt（同 ``n`` 必得同一结果；无随机源，INV-6）。"""
    phases = ("player_turn", "map", "shop", "event", "treasure", "forge", "shrine", "rest")
    prompts: list[list[Message]] = []
    for index in range(max(0, int(n))):
        phase = phases[index % len(phases)]
        stage = index % 6 + 1
        prompts.append([
            Message(role="system", content=_SYSTEM),
            Message(role="user",
                    content=(f"第 {index + 1} 个决策点：phase={phase} stage={stage} "
                             f"turn={index % 12} hand=[d{index % 5 + 1},d{index % 3 + 1}] "
                             f"slots=[s1,s2,s3] enemies=[e{index % 2 + 1}] "
                             f"legalActionKinds=[play_face,discard_die,end_turn]；"
                             f"请给出下一步动作计划。")),
        ])
    return prompts


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _to_messages(row: Any) -> list[Message] | None:
    if isinstance(row, list):
        raw = row
    elif isinstance(row, dict):
        raw = row.get("messages")
    else:
        return None
    if not isinstance(raw, list) or not raw:
        return None
    out: list[Message] = []
    for item in raw:
        if not isinstance(item, dict) or "role" not in item or "content" not in item:
            return None
        out.append(Message(role=str(item["role"]), content=str(item["content"]))) # type: ignore[arg-type]
    return out


def load_robustness_prompts(n: int = 200,
                            path: str | Path | None = None) -> tuple[list[list[Message]], str]:
    """返回 ``(prompts, source)``；文件缺失时用确定性合成兜底。

    文件中的条数不足 ``n`` 时循环使用，并附加一条递增的 user 标记，保证 prompt_digest 两两不同。
    """
    target = Path(path) if path is not None else Path(DEFAULT_PROMPTS_PATH)
    base: list[list[Message]] = []
    if target.is_file():
        for row in _load_jsonl(target):
            messages = _to_messages(row)
            if messages is not None:
                base.append(messages)
    source = f"file:{target}" if base else "synthetic:fallback"
    if not base:
        base = synthetic_prompts(min(max(int(n), 1), 64))
    prompts: list[list[Message]] = []
    for index in range(max(0, int(n))):
        messages = list(base[index % len(base)])
        if index >= len(base):
            messages = messages + [Message(role="user", content=f"（决策点 #{index + 1}）")]
        prompts.append(messages)
    return prompts, source


def message_role_names(prompts: Sequence[Sequence[Message]]) -> list[str]:
    """调试辅助：prompt 集合的 role 序列（不变式检查用）。"""
    return [m.role for prompt in prompts for m in prompt]
