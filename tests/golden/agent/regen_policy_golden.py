"""显式重新生成 MOD-04 policy prompt golden（G2/NFR-04-3）。

用法（在仓库根执行）::

    .venv/bin/python tests/golden/agent/regen_policy_golden.py

产出（``tests/golden/agent/``）：

- ``policy_prompt_<hash16>.txt``：固定输入下渲染出的 prompt 正文（逐字节）；
- ``policy_prompt.json``：``prompt_hash`` / ``template_hash`` / ``token_estimate`` 等元数据。

golden 只在**显式**运行本脚本时更新；测试只读比对。改动模板或组装逻辑必须显式更新快照
并在 PR 说明（G2）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
for entry in (str(HERE), str(REPO_ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from policy_case import (HERE as OUT, golden_context, golden_filename,  # noqa: E402
                         golden_spec)
from gamer_rsi.agent.prompts.assembler import assemble_prompt  # noqa: E402


def main() -> int:
    bundle = assemble_prompt(golden_spec(), golden_context())
    for stale in OUT.glob("policy_prompt_*.txt"):
        stale.unlink()
    target = OUT / golden_filename(bundle.prompt_hash)
    target.write_text(bundle.text, encoding="utf-8")
    metadata = {
        "prompt_hash": bundle.prompt_hash,
        "template_hash": bundle.template_hash,
        "token_estimate": bundle.token_estimate,
        "memory_ids": list(bundle.memory_ids),
        "source_ids": list(bundle.source_ids),
        "truncated": [record.to_dict() for record in bundle.truncated],
        "unknown_placeholders": list(bundle.unknown_placeholders),
        "file": target.name,
    }
    (OUT / "policy_prompt.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    print(f"wrote {target} ({len(bundle.text)} chars, {bundle.token_estimate} tokens)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
