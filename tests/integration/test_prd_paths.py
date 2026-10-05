"""PRD 测试路径守门（Spec §9 G6 / M1 验收报告 §6 F9）—— 登记于 MOD-08 PRD §11 T-18。

背景：各模块 PRD 里的 ``tests/...`` 路径曾与实际目录漂移（MOD-04 的 policy 单测与
golden 少了一层 ``agent/``、MOD-08 的指标快照少了一层 ``evals/``、MOD-01 的差分对拍
脚本名不符），读者按 PRD 复制路径即落空。``docs/prd/*.md`` 是测试路径的唯一登记处，
本用例把该约定变成可执行守门：文档里出现的每个 ``tests/...`` 路径 token 必须满足
以下之一，否则失败并列出「文档:行号 + 写错的路径 + 候选路径」：

1. **存在**：文件或目录存在（``tests/golden/`` 这类目录引用也算存在）；
2. **glob 命中**：通配写法（如 ``tests/unit/agent/test_policy_*.py``）能匹配到文件；
3. **命名模式**：含 ``*`` / ``<...>`` / ``{...}`` 占位符。**放宽理由**：这类 token 是
   命名模式而不是可直接复制的路径——``tests/golden/agent/policy_prompt_<hash>.txt``
   的 hash 随 ``prompt_hash`` 轮换、``tests/unit/sandbox/test_sandbox_{projection,api,
   guard,audit,honeypot}.py`` 是花括号展开写法——仓库里不存在同名文件是设计使然，
   强行要求字面量存在只会把「模式」误判成「漂移」；通配/占位符由各自的生成脚本与
   目录约定保证，不在本用例的可判定范围内；
4. **前瞻登记**：命中 ``_PENDING_MODULE_PATHS`` 白名单——MOD-05/06/07 尚未实现
   （排期 M2），其 PRD 先登记了测试路径。白名单刻意保持最小，并由
   ``test_pending_whitelist_has_no_stale_entries`` 保证模块落地后条目自动失效。
"""

from __future__ import annotations

import difflib
import glob as globlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PRD_GLOB = "docs/prd/*.md"

# 路径 token 允许的右侧字符；`,` 只在花括号内允许（见 _extract_tokens）
_TOKEN_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./*<>{}-"
)
# 左侧可用于「补全前缀目录」的字符（吃掉 src/game/ 这类前缀）
_PREFIX_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./-"
)
_PATTERN_CHARS = "*<>{}"
_TRAILING_PUNCT = ".,:;"

# ---- 白名单（必须尽量小；每条都要写明理由）-----------------------------------
# MOD-05/06/07 尚未实现（排期 §2 归 M2），相关 PRD 已定稿并**先行登记**测试路径。
# 这是「前瞻登记」而非「路径漂移」，所以显式豁免；模块一落地，下面
# test_pending_whitelist_has_no_stale_entries 就会因「路径已存在」而失败，逼维护者删条目。
_PENDING_MODULE_PATHS: dict[str, str] = {
    "tests/unit/memory/": "MOD-05 memory 未实现（M2）",
    "tests/integration/test_loop_memory.py": "MOD-05 memory 未实现（M2）",
    "tests/fakes.py": "MOD-06 loop 未实现（M2）",
    "tests/integration/test_loop_closure.py": "MOD-06 loop 未实现（M2）",
    "tests/unit/evolve/": "MOD-07 evolve 未实现（M2）",
    "tests/integration/evolve/": "MOD-07 evolve 未实现（M2）",
    "tests/unit/evolve/test_malicious_candidates.py": "MOD-07 evolve 未实现（M2）",
}


def _extract_tokens(line: str) -> list[str]:
    """从一行文本里抽出所有 ``tests/...``（含 ``src/game/tests/...``）路径 token。

    左侧会补全紧邻的前缀目录（GAME 侧 golden 真实位置是 ``src/game/tests/golden/``，
    只取 ``tests/`` 尾巴会误报）；右侧遇到空白、反引号、引号、CJK 标点即停，
    但保留 ``{a,b}`` 花括号列表整体（逗号只在花括号深度 > 0 时计入）。
    """
    tokens: list[str] = []
    search_from = 0
    while True:
        hit = line.find("tests/", search_from)
        if hit < 0:
            return tokens
        search_from = hit + len("tests/")

        start = hit
        while start > 0 and line[start - 1] in _PREFIX_CHARS:
            start -= 1

        end = hit + len("tests/")
        depth = 0
        while end < len(line):
            char = line[end]
            if char == "{":
                depth += 1
            elif char == "}":
                depth = max(0, depth - 1)
            elif char == "," and depth == 0:
                break
            elif char not in _TOKEN_CHARS:
                break
            end += 1

        token = line[start:end].rstrip(_TRAILING_PUNCT)
        if token:
            tokens.append(token)


def _is_pattern(token: str) -> bool:
    return any(char in token for char in _PATTERN_CHARS)


def _exists(token: str) -> bool:
    if (REPO_ROOT / token).exists():
        return True
    return bool(globlib.glob(str(REPO_ROOT / token), recursive=True))


def _known_paths() -> list[str]:
    """仓库里所有候选测试路径（``tests/`` 与 ``src/game/tests/`` 下的文件与目录）。"""
    known: list[str] = []
    for root in (REPO_ROOT / "tests", REPO_ROOT / "src/game/tests"):
        if not root.is_dir():
            continue
        known.append(root.relative_to(REPO_ROOT).as_posix() + "/")
        for path in sorted(root.rglob("*")):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(REPO_ROOT).as_posix()
            known.append(rel + "/" if path.is_dir() else rel)
    return known


def _suggest(token: str, known: list[str]) -> list[str]:
    """给出候选路径：先按同名文件/目录，再退回相似度匹配。"""
    basename = token.rstrip("/").rsplit("/", 1)[-1]
    same_name = [p for p in known if p.rstrip("/").rsplit("/", 1)[-1] == basename]
    if same_name:
        return same_name[:4]
    return difflib.get_close_matches(token, known, n=3, cutoff=0.6)


def _collect_problems() -> list[tuple[Path, int, str]]:
    problems: list[tuple[Path, int, str]] = []
    for doc in sorted(REPO_ROOT.glob(PRD_GLOB)):
        lines = doc.read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines, 1):
            if "tests/" not in line:
                continue
            for token in _extract_tokens(line):
                if _exists(token) or token in _PENDING_MODULE_PATHS or _is_pattern(token):
                    continue
                problems.append((doc, lineno, token))
    return problems


def test_prd_docs_are_present() -> None:
    # 防止 glob 落空导致守门空转（docs/prd 改名/移动时必须一起改这里）
    docs = sorted(REPO_ROOT.glob(PRD_GLOB))
    assert docs, f"未找到任何受管文档：{PRD_GLOB}（守门失效，请检查路径）"


def test_prd_test_paths_exist() -> None:
    problems = _collect_problems()
    if not problems:
        return

    known = _known_paths()
    report = [f"PRD 中 {len(problems)} 处 tests/ 路径指向不存在的文件（Spec §9 G6，读者复制即落空）：", ""]
    for doc, lineno, token in problems:
        report.append(f"  · {doc.relative_to(REPO_ROOT)}:{lineno}")
        report.append(f"    写错：{token}")
        candidates = _suggest(token, known)
        if candidates:
            for candidate in candidates:
                report.append(f"    候选：{candidate}")
        else:
            report.append("    候选：（无同名文件，请人工确认实际位置）")
        report.append("")
    report.append("修法：把 PRD 里的路径改成上面的候选之一（或先在实际目录改名，再同步 PRD）。")
    report.append(f"若确属未实现模块的前瞻登记，请加入白名单 {Path(__file__).name}::_PENDING_MODULE_PATHS 并写明理由。")
    pytest.fail("\n".join(report))


def test_pending_whitelist_has_no_stale_entries() -> None:
    # 白名单卫生：模块落地（路径出现）后条目必须删除，否则守门会长期放水
    stale = [f"  · {token}（{reason}）" for token, reason in _PENDING_MODULE_PATHS.items()
             if _exists(token)]
    assert not stale, (
        "白名单条目已不再是「前瞻登记」——对应路径已存在，请从 "
        f"_PENDING_MODULE_PATHS 中删除：\n" + "\n".join(stale)
    )
