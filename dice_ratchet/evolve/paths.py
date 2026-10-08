"""MOD-07 路径与工件布局（PRD §6.4；Spec v1.4 B-29/B-32）。

**已裁决（Spec v1.4 B-29/B-32/B-33）**：不可变快照与过程工件**分家**——

``harness/``（当前生效工件，MOD-04 只读）
    ``version.json``  ``prompts/``  ``tools/``  ``config.json``
    ``versions/v{N}/``  每次 accept 的不可变快照：``version.json`` + ``diff.patch``
                        + 全量工件（``prompts/`` ``tools/`` ``config.json``）

``runs/<run_id>/generations/gen-{NNN}/``（过程与裁决，随 run 存续）
    ``report.json``  ``gate.json``  ``decision.jsonl``
    ``candidates/{candidate_id}.json``  ``patches/{candidate_id}.patch``

本模块只做**路径计算**，不做 IO——IO 一律走 ``infra.io`` 的原子写入口（FR-09-18）。
``harness_dir`` 可注入（测试用 ``tmp_path``），默认走 cwd 下的 ``harness/``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final, Sequence

__all__ = [
    "HARNESS_SUBDIRS", "LAYOUT_SCHEMA_VERSION", "EvolveContext", "GenPaths", "RunPaths",
    "candidate_path", "decision_log_path", "expires_at_from", "gate_path", "gen_dir",
    "generation_dir_name", "harness_dir", "harness_version_dir",
    "looks_like_candidate_id", "patch_path", "report_path", "snapshot_entries", "utc_now",
]

LAYOUT_SCHEMA_VERSION: Final[str] = "evolve-layout-1.0.0"

#: 会被快照/回滚的 harness 工件子目录（``version.json`` 是唯一入口，端口不进子目录表）。
HARNESS_SUBDIRS: Final[tuple[str, ...]] = ("prompts", "tools")

#: ``versions/v{N}/`` 内允许出现的名字（回滚只认这些，防止把过程工件卷进快照）。
_SNAPSHOT_ENTRIES: Final[tuple[str, ...]] = ("version.json", "diff.patch", "revert.patch",
                                             "config.json", "prompts", "tools")
_ID_LEN: Final[int] = 12


def harness_dir(base: Path | str | None = None) -> Path:
    """``harness/`` 根（默认 cwd 下的 ``harness/``）。"""
    return Path(base) if base is not None else Path("harness")


def harness_version_dir(version: int, *, harness: Path | str | None = None) -> Path:
    """``harness/versions/v{N}/``（不可变快照；N 不可复用、不可重写历史）。"""
    return harness_dir(harness) / "versions" / f"v{int(version)}"


def utc_now(now: datetime | None = None) -> str:
    """``YYYY-MM-DDTHH:MM:SSZ``（UTC）。

    ``now`` 显式传入时以它为准 —— NFR-07-1 的 ``--freeze-time`` 由此注入，
    测试可得到逐字节可复现的 ``created_at``。
    """
    moment = now or datetime.now(tz=timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def expires_at_from(created_at: str, hours: float) -> str:
    """``created_at + hours``（HITL 超时时刻；解析失败则按当前时刻兜底）。"""
    try:
        base = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except (TypeError, ValueError):
        base = datetime.now(tz=timezone.utc)
    return utc_now(base + timedelta(hours=float(hours)))


# =====================================================================
# run 侧路径
# =====================================================================

def generation_dir_name(generation: int) -> str:
    """``gen-{N:03d}``（PRD §6.4；gen-000 是 MOD-06 写的 v0 基线）。"""
    return f"gen-{int(generation):03d}"


def gen_dir(run_dir: Path | str, generation: int) -> Path:
    return Path(run_dir) / "generations" / generation_dir_name(generation)


def report_path(run_dir: Path | str, generation: int) -> Path:
    return gen_dir(run_dir, generation) / "report.json"


def gate_path(run_dir: Path | str, generation: int) -> Path:
    return gen_dir(run_dir, generation) / "gate.json"


def candidate_path(run_dir: Path | str, generation: int, candidate_id: str) -> Path:
    return gen_dir(run_dir, generation) / "candidates" / f"{_safe_id(candidate_id)}.json"


def patch_path(run_dir: Path | str, generation: int, candidate_id: str) -> Path:
    return gen_dir(run_dir, generation) / "patches" / f"{_safe_id(candidate_id)}.patch"


def decision_log_path(run_dir: Path | str, generation: int) -> Path:
    return gen_dir(run_dir, generation) / "decision.jsonl"


def _safe_id(candidate_id: str) -> str:
    """``candidate_id`` 只允许 12 位 hex 进文件名（防路径穿越，FR-07-4）。"""
    text = str(candidate_id)
    if not looks_like_candidate_id(text):
        raise ValueError(f"非法 candidate_id（要求 12 位 hex）：{text!r}")
    return text


def looks_like_candidate_id(candidate_id: str) -> bool:
    text = str(candidate_id)
    return len(text) == _ID_LEN and all(c in "0123456789abcdef" for c in text)


@dataclass(frozen=True)
class GenPaths:
    """``gen-{N}/`` 的惰性视图（属性访问不建目录；``ensure()`` 才建）。"""

    root: Path

    @classmethod
    def of(cls, run_dir: Path | str, generation: int) -> "GenPaths":
        return cls(root=gen_dir(run_dir, generation))

    @property
    def report(self) -> Path:
        return self.root / "report.json"

    @property
    def report_sha(self) -> Path:
        return self.root / "report.sha256"

    @property
    def gate(self) -> Path:
        return self.root / "gate.json"

    @property
    def decisions(self) -> Path:
        return self.root / "decision.jsonl"

    @property
    def candidates(self) -> Path:
        return self.root / "candidates"

    @property
    def patches(self) -> Path:
        return self.root / "patches"

    def candidate(self, candidate_id: str) -> Path:
        return self.candidates / f"{_safe_id(candidate_id)}.json"

    def patch(self, candidate_id: str) -> Path:
        return self.patches / f"{_safe_id(candidate_id)}.patch"

    def candidate_ids(self) -> tuple[str, ...]:
        """已落盘的候选 id（字典序；缺失目录 → 空）。"""
        if not self.candidates.is_dir():
            return ()
        return tuple(sorted(p.stem for p in self.candidates.glob("*.json")))

    def ensure(self) -> "GenPaths":
        for path in (self.root, self.candidates, self.patches):
            path.mkdir(parents=True, exist_ok=True)
        return self

    def as_dict(self) -> dict[str, Any]:
        return {"schema_version": LAYOUT_SCHEMA_VERSION, "root": str(self.root),
                "report": str(self.report), "gate": str(self.gate),
                "candidates": str(self.candidates), "patches": str(self.patches)}


@dataclass(frozen=True)
class RunPaths:
    """``runs/<run_id>/`` 的演化侧视图（与 MOD-09 ``infra.run.RunPaths`` 互补）。"""
    root: Path

    @classmethod
    def of(cls, run_dir: Path | str) -> "RunPaths":
        return cls(root=Path(run_dir))

    @property
    def generations(self) -> Path:
        return self.root / "generations"

    @property
    def metrics(self) -> Path:
        return self.root / "metrics"

    @property
    def audit(self) -> Path:
        return self.root / "audit"

    def gen(self, generation: int) -> GenPaths:
        return GenPaths.of(self.root, generation)

    def generations_present(self) -> tuple[int, ...]:
        """已落盘的代数（``gen-NNN`` 目录名反解；缺失 → 空元组）。"""
        if not self.generations.is_dir():
            return ()
        out: list[int] = []
        for path in sorted(self.generations.glob("gen-*")):
            suffix = path.name[4:]
            if path.is_dir() and suffix.isdigit():
                out.append(int(suffix))
        return tuple(sorted(out))


@dataclass(frozen=True)
class EvolveContext:
    """演化运行的**全部根路径**（替代裸 ``cfg`` 传递，避免 ``runs_root`` 漂移）。

    ``cfg`` 侧只关心 ``evolve.*`` 的阈值；根目录属于 MOD-09，因此由调用方显式给出：
    ``EvolveContext.of(cfg, run_id=..., harness=...)`` 从 ``cfg.infra.runs_root``
    与 ``run_ctx.harness_dir`` 一次性解析，之后 staging / 账本 / 报告三处路径同源。
    """

    run_id: str
    root: Path
    harness: Path
    runs_root: Path

    @classmethod
    def of(cls, cfg: Any, *, run_id: str, runs_root: Path | str | None = None,
           harness: Path | str | None = None) -> "EvolveContext":
        resolved_runs = Path(runs_root) if runs_root is not None else Path(
            getattr(getattr(cfg, "infra", cfg), "runs_root", None) or "runs")
        return cls(run_id=str(run_id), root=resolved_runs / str(run_id),
                   harness=harness_dir(harness), runs_root=resolved_runs)

    def gen(self, generation: int) -> GenPaths:
        return GenPaths.of(self.root, generation)

    @property
    def staging_root(self) -> Path:
        return self.root / "generations"

    def ledger_path(self, generation: int) -> Path:
        return self.gen(generation).root / "version_ledger.json"

    def as_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "root": str(self.root),
                "harness": str(self.harness), "runs_root": str(self.runs_root)}


def snapshot_entries(*, extra: Sequence[str] = ()) -> tuple[str, ...]:
    """快照目录允许的条目名（回滚/拷贝的过滤白名单）。"""
    return tuple(_SNAPSHOT_ENTRIES) + tuple(str(e) for e in extra)
