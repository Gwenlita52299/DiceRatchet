"""显式重新生成 MOD-05 memory golden（G2：改动必须显式更新并说明）。

用法（在仓库根执行）::

    .venv/bin/python tests/golden/memory/regen_memory_golden.py

产出（``tests/golden/memory/``）：

- ``retrieve_seed1042_g0.json``：固定反思 + ``tick=12`` 下 ``retrieve()`` 的可比对视图
  （条目、分数、tokens、dropped 原因、渲染文本、conflict 组）；
- ``memory_v0_sha256.txt``：``memory_v0.jsonl`` 条目段的 ``content_sha256``。

golden 只在**显式**运行本脚本时更新；测试只读比对（PRD §12 G2）。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
for entry in (str(HERE), str(REPO_ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from memory_case import RETRIEVE_GOLDEN, SHA256_GOLDEN, build  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        case = build(Path(tmp))
    RETRIEVE_GOLDEN.write_text(
        json.dumps(case.retrieve, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    SHA256_GOLDEN.write_text(case.snapshot_sha256 + "\n", encoding="utf-8")
    print(f"wrote {RETRIEVE_GOLDEN.name}: {len(case.retrieve['entries'])} entries, "
          f"tokens={case.retrieve['tokens']}")
    print(f"wrote {SHA256_GOLDEN.name}: {case.snapshot_sha256[:16]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
