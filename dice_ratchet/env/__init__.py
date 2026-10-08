"""MOD-01 env-rules：GAME 规则内核的纯 Python 逐位等价移植（MOD-01 PRD v1.0）。

- 对外门面：``GameEngine``（create/reduce/legal_actions/is_legal/observe/play_hints/
  native_result/replay）。
- 模块级函数与 ``src/game/kernel/*.js`` 一一对应（FR-01-1），函数名 snake_case，
  入参与返回的 dict 键保持 camelCase 不变。
- 零依赖（NFR-01-3）：仅标准库；不 import ``dice_ratchet`` 其他子包（NFR-01-4）。

对拍基准：``src/game/tests/golden/*.jsonl``（JS 侧生成，本模块只读）。
"""

from __future__ import annotations

from .digest import config_hash, digest
from .engine import (ACTION_KINDS, KERNEL_VERSION, OBS_SCHEMA, TOGGLE_KEYS, GameEngine,
                     EnvError, apply_difficulty, allowed_stage_counts, config_schema,
                     load_tables_checked, map_error_code, native_result, state_hash,
                     validate_config)
from .error import KernelError
from .loader import GAME_CONTENT_DIR, build_tables, filter_by_toggles, load_tables
from .rng import STREAM_NAMES

__all__ = [
    "GameEngine", "KernelError", "EnvError",
    "KERNEL_VERSION", "OBS_SCHEMA", "ACTION_KINDS", "TOGGLE_KEYS", "STREAM_NAMES",
    "GAME_CONTENT_DIR",
    "digest", "config_hash", "state_hash", "native_result", "map_error_code",
    "apply_difficulty", "allowed_stage_counts", "validate_config", "config_schema",
    "load_tables", "load_tables_checked", "build_tables", "filter_by_toggles",
]
