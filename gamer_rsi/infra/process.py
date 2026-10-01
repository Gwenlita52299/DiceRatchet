"""进程级确定性环境固化（FR-09-17）。

- ``PYTHONHASHSEED=0``（未设置则 re-exec 一次，使 hash 随机化对本进程生效）
- ``TZ=UTC``、``LC_ALL=C.UTF-8``
- ``infra.threads=1`` 传给后端（numpy/BLAS 线程数）
- 所有设置记入 ``infra.run_start`` 事件
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Any, Final, Mapping

from . import events

__all__ = ["bootstrap_process", "process_fingerprint", "utcnow_iso"]

_FORCED_ENV: Final[dict[str, str]] = {
    "PYTHONHASHSEED": "0",
    "TZ": "UTC",
    "LC_ALL": "C.UTF-8",
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
}

_REEXEC_FLAG: Final[str] = "GAMERSI_REEXECED"


def utcnow_iso() -> str:
    """UTC ISO-8601，带 ``Z``（轨迹行/事件统一时间戳格式，FR-09-8）。"""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def bootstrap_process(threads: int = 1, *, reexec: bool = True,
                      module: str | None = None) -> Mapping[str, Any]:
    """固化进程环境，返回写入 ``run_start`` 的环境快照。

    ``reexec=True`` 时若 ``PYTHONHASHSEED`` 此前未设置，会 ``os.execv`` 重启自身一次，
    使 hash 随机化对本进程生效。``module`` 非空时以 ``python -m <module>`` 重放
    （保留包上下文，相对 import 才可用）；仅在 ``__main__`` 入口调用。
    库/测试调用传 ``reexec=False``。
    """
    if reexec and os.environ.get(_REEXEC_FLAG) != "1":
        needs_reexec = "PYTHONHASHSEED" not in os.environ
        for key, value in _FORCED_ENV.items():
            os.environ[key] = value
        os.environ[_REEXEC_FLAG] = "1"
        os.environ["GAMERSI_THREADS"] = str(threads)
        if needs_reexec:
            argv = [sys.executable, *sys.argv]
            if module:
                argv = [sys.executable, "-m", module, *sys.argv[1:]]
            os.execv(sys.executable, argv)
    else:
        for key, value in _FORCED_ENV.items():
            os.environ.setdefault(key, value)
        os.environ.setdefault("GAMERSI_THREADS", str(threads))

    snapshot = dict(_FORCED_ENV)
    snapshot["threads"] = threads
    snapshot["python"] = sys.version.split()[0]
    snapshot["argv0"] = sys.argv[0] if sys.argv else ""
    events.emit("infra.run_start", env=snapshot, threads=threads,
                python=sys.version.split()[0])
    return snapshot


def process_fingerprint() -> Mapping[str, Any]:
    """供 ``run_start`` 补充的进程指纹（不参与任何 hash）。"""
    return {
        "pythonhashseed": os.environ.get("PYTHONHASHSEED"),
        "tz": os.environ.get("TZ"),
        "locale": os.environ.get("LC_ALL"),
        "threads": int(os.environ.get("GAMERSI_THREADS", "1")),
    }
