"""T-06 INV-7 结构检查：主指标计算路径不得出现 shaped 分数字段标识符。

用 **AST**（非正则/非子串）扫描 ``dice_ratchet/evals/metrics.py``：任何 ``Name`` /
``Attribute`` / ``arg`` / ``keyword`` 标识符命中 ``reward`` / ``cum_reward`` 即失败。
另守：``EpisodeResult`` 无对应字段；``metrics`` 不 import ``reward``。
"""

from __future__ import annotations

import ast
from pathlib import Path

from dice_ratchet.evals.metrics import EpisodeResult

_REPO = Path(__file__).resolve().parents[3]
_METRICS = _REPO / "dice_ratchet" / "evals" / "metrics.py"
_BANNED = {"reward", "cum_reward", "shaped_reward", "mean_cum_reward_debug"}


def _identifiers(tree: ast.AST) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.append(node.id)
        elif isinstance(node, ast.Attribute):
            found.append(node.attr)
        elif isinstance(node, ast.arg):
            found.append(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            found.append(node.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                found.append(alias.name.split(".")[-1])
                found.append(alias.asname or "")
    return found


def test_metrics_module_has_no_reward_identifiers():
    tree = ast.parse(_METRICS.read_text(encoding="utf-8"))
    hits = sorted(set(_identifiers(tree)) & _BANNED)
    assert hits == [], f"INV-7：metrics.py 出现 shaped 分数字段标识符 {hits}"


def test_episode_result_has_no_reward_fields():
    field_names = set(EpisodeResult.__dataclass_fields__)
    assert field_names & _BANNED == set()
    assert "reward" not in field_names and "cum_reward" not in field_names


def test_metrics_module_does_not_import_reward_module():
    source = _METRICS.read_text(encoding="utf-8")
    assert "import reward" not in source
    assert "from .reward" not in source and "from . import reward" not in source
