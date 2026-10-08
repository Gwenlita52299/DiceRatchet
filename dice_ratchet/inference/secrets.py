"""API key 的本地解析（Issue #1「认证与网络」/ 真跑 B·C 臂的前置管道）。

设计目标（按重要性排序）：

1. **key 绝不进 Git、run 工件、事件或日志**：本模块只把 key 交回给调用方（内存里），
   从不写盘、从不打印、从不 ``os.environ[...] = ...``。
2. **只读注册表声明过的那个名字**：调用方传入的 ``name`` 来自
   ``configs/models.yaml`` 的 ``api_key_env``（例如 ``DEEPSEEK_API_KEY``），文件里其它
   行**永远不会**被读取或生效 —— 因此 ``.env`` 不能被用来偷偷改配置
   （例如往里写 ``DICERATCHET_EXPERIMENT__ARM=C1`` 是无效的，见单测）。
3. **真实环境变量优先**：``export DEEPSEEK_API_KEY=...`` 的语义不变；``.env`` 只是
   「不想每次 export」的本地便利，不覆盖已存在的环境变量。
4. **不改进程环境**：只做查找（``lookup_api_key``），绝不 ``os.environ.update(...)`` ——
   这样受限子进程、MOD-01 的配表加载、``config.json`` 的环境快照都看不到这个 key。

文件格式（``inference.api_key_file``，默认 ``.env``，空字符串=关闭）：

    # 注释
    DEEPSEEK_API_KEY=sk-xxxx          # 行内注释
    export OTHER_KEY="带空格的值"

同名多行**后者生效**（与 shell 一致）；无法解析的行直接忽略（不抛错、不猜测）。
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, Mapping

__all__ = ["DEFAULT_API_KEY_FILE", "ApiKeyRef", "parse_env_file", "env_file_path",
           "lookup_api_key", "permissions_warning"]

#: ``inference.api_key_file`` 的代码默认值（仓库根下的本地文件，已被 .gitignore 忽略）。
DEFAULT_API_KEY_FILE: Final[str] = ".env"

#: 合法的环境变量名（与 shell 一致）。
_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

ApiKeySource = Literal["environment", "env_file", "missing"]


@dataclass(frozen=True)
class ApiKeyRef:
    """一次 key 查找的结果（``value`` 只在内存里流转，``repr`` 已屏蔽）。"""

    name: str
    source: ApiKeySource
    value: str = ""
    env_file: Path | None = None

    @property
    def present(self) -> bool:
        return self.source != "missing"

    def __repr__(self) -> str: # pragma: no cover - 防误打印
        return (f"ApiKeyRef(name={self.name!r}, source={self.source!r}, "
                f"value={'***' if self.value else ''}, env_file={self.env_file})")


def parse_env_file(text: str) -> dict[str, str]:
    """解析 ``KEY=VALUE`` 文本（注释 / 空行 / ``export`` / 引号 / 行内注释）。

    解析失败的行**忽略**（本地便利文件不该因为一行手滑就让整个 run 起不来）；
    同名多次出现取**最后**一次（shell 语义）。
    """
    out: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if not _NAME_RE.match(name):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        elif " #" in value: # 未加引号时才允许行内注释
            value = value.split(" #", 1)[0].rstrip()
        out[name] = value
    return out


def env_file_path(config: Any = None, *, default: str = DEFAULT_API_KEY_FILE) -> Path | None:
    """从 ``inference.api_key_file`` 解析出文件路径；``""``（显式关闭）→ ``None``。

    相对路径按**当前工作目录**解析（仓库根运行 CLI 的既有约定，与其它相对路径一致）。
    """
    raw = getattr(config, "api_key_file", default)
    if raw is None:
        raw = default
    text = str(raw).strip()
    if not text:
        return None
    return Path(text)


def lookup_api_key(name: str, *, env_file: Path | str | None = None,
                   environ: Mapping[str, str] | None = None) -> ApiKeyRef:
    """查 key：**真实环境变量优先**，其次 ``env_file``；都没有 → ``source="missing"``。

    绝不修改 ``environ``；文件不存在/不可读一律当作「没有」交给上层 fail-closed
    （MOD-03 会抛 ``E_LLM_UNAVAILABLE`` 并给出该填哪里）。
    """
    if not name:
        return ApiKeyRef(name="", source="missing")
    env = os.environ if environ is None else environ
    from_env = env.get(name)
    if from_env:
        return ApiKeyRef(name=name, source="environment", value=from_env)
    path = Path(env_file) if env_file is not None else None
    if path is None:
        return ApiKeyRef(name=name, source="missing", env_file=None)
    try:
        values = parse_env_file(path.read_text(encoding="utf-8"))
    except OSError:
        return ApiKeyRef(name=name, source="missing", env_file=path)
    value = values.get(name, "")
    if not value:
        return ApiKeyRef(name=name, source="missing", env_file=path)
    return ApiKeyRef(name=name, source="env_file", value=value, env_file=path)


def permissions_warning(path: Path | str | None) -> str | None:
    """文件对「组/其他」可读时返回人话警告（posix）；否则 ``None``。

    只是提醒（``chmod 600 .env``），不阻断：与项目其它「可观测性缺失 → 降级 + 告警」
    的处理一致（Spec §8.5）。
    """
    if path is None or os.name != "posix":
        return None
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return None
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        return (f"{path} 对同组/其他用户可读（mode={stat.S_IMODE(mode):04o}）；"
                f"建议 chmod 600 {path}")
    return None
