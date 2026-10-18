"""把决策模板重排成「前缀缓存友好」的顺序（2026-10-17 缓存修复）。

**只动段落顺序，不改规则语义**：

* 静态块（任务 / 运行环境 / 回合预算 / 输出契约）全部前移；
* 动态块（观测 / 合法动作 / 反思 / 记忆 / 约束）全部后移；
* 末尾加一句很短的静态提醒，保留「贴近生成点」的格式约束。

DeepSeek 的前缀缓存按 64-token 块匹配**最长公共前缀**。原模板把 1,900+ 字符的
「输出契约」放在 `{{observation}}` 之后 ⇒ 这段静态文本永远不可能命中；重排后
可缓存前缀从「模板头 + task」扩到「模板头 + task + 回合预算 + 输出契约」。

本模块是**纯文本变换**，不读配置、不读 run；调用方负责决定写到哪个 harness。
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["STATIC_SECTIONS", "DYNAMIC_SECTIONS", "REQUIRED_SECTIONS", "FINAL_SECTION",
           "FINAL_REMINDER", "CacheLayoutError", "reorder_for_prefix_cache", "is_reordered"]

#: 前移的静态段（按输出顺序）。
STATIC_SECTIONS: Final[tuple[str, ...]] = (
    "任务说明", "运行环境", "回合预算", "输出契约",
)
#: 后移的动态段（按输出顺序；观测必须最先，后面全是逐步变化的上下文）。
DYNAMIC_SECTIONS: Final[tuple[str, ...]] = (
    "当前观测", "当前可选动作", "近期反思", "策略记忆", "约束与纠错",
)
#: 必须具备的段落（``回合预算`` 是 P1 起才有的可选静态段，裸 v0 没有它）。
REQUIRED_SECTIONS: Final[tuple[str, ...]] = (
    "任务说明", "运行环境", "输出契约",
    "当前观测", "当前可选动作", "近期反思", "策略记忆", "约束与纠错",
)
FINAL_SECTION: Final[str] = "最终提醒"
FINAL_REMINDER: Final[str] = (
    "## 最终提醒\n"
    "只输出一个 JSON 对象；`kind` 与所有 id 必须逐字取自上面的观测 / legal_actions，不要自创。\n"
)

_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


class CacheLayoutError(ValueError):
    """模板结构不符合重排契约（缺段/重复段/未知段）。"""


def _key(title: str) -> str:
    """``当前观测（唯一事实来源）`` → ``当前观测``。"""
    return title.split("（", 1)[0].strip()


def _split(text: str) -> tuple[str, dict[str, str]]:
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        raise CacheLayoutError("模板里没有任何 ``##`` 段落，无法重排")
    header = text[:matches[0].start()]
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        title = _key(match.group(1))
        if title in sections:
            raise CacheLayoutError(f"段落重复：{title!r}")
        sections[title] = text[match.start():end]
    return header, sections


def _join(blocks: list[str]) -> str:
    clean = [block.strip("\n") for block in blocks if block.strip()]
    return "\n\n".join(clean) + "\n"


def reorder_for_prefix_cache(text: str) -> str:
    """返回重排后的模板文本（幂等；重复调用不会叠加最终提醒）。"""
    header, sections = _split(text)
    known = set(STATIC_SECTIONS) | set(DYNAMIC_SECTIONS) | {FINAL_SECTION}
    unknown = sorted(set(sections) - known)
    if unknown:
        raise CacheLayoutError(f"未知段落（拒绝猜测归属）：{unknown}")
    missing = [name for name in REQUIRED_SECTIONS if name not in sections]
    if missing:
        raise CacheLayoutError(f"缺少必需段落：{missing}")

    # 规则 5 的「上面的观测」在重排后指向会反过来；只改这一处指代。
    contract = sections["输出契约"]
    contract = contract.replace("上面的观测", "下面的观测")

    final = sections.get(FINAL_SECTION, FINAL_REMINDER)
    blocks = [header]
    blocks.extend(sections[name] for name in STATIC_SECTIONS
                  if name in sections and name != "输出契约")
    blocks.append(contract)
    blocks.extend(sections[name] for name in DYNAMIC_SECTIONS)
    blocks.append(final)
    return _join(blocks)


def is_reordered(text: str) -> bool:
    """幂等检查：重排后的文本再跑一次应逐字节不变。"""
    try:
        return reorder_for_prefix_cache(text) == text
    except CacheLayoutError:
        return False
