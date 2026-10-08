"""MOD-07 版本化：staging / accept / rollback / 血缘（PRD FR-07-9 … FR-07-10；§8.4）。

**不可变版本**（Spec B-29）：``accept()`` 生成 ``harness/versions/v{N}/``（全量拷贝 +
``version.json`` + ``diff.patch``），更新 ``harness/version.json`` 指针，经 MOD-09
``git_ops.commit_harness`` 提交到当前分支，并打 tag ``harness-v{N}``（**不建同名分支**，
Spec B-22）。``N`` = 现有最大 +1，**不可复用、不可重写历史**。

**回滚语义**（FR-07-10 / Spec §8.4）：``rollback(to_version)`` 用 ``versions/v{to_version}/``
覆盖 ``harness/``，写 ``version.json``（``parent = current_version``，``version = current + 1``
——编号**不回退**），生成 ``revert.patch``，提交 ``revert(harness): back to v{N}``。

**幂等**（NFR-07-6）：``accept()`` / ``rollback()`` 以 ``(run_id, generation)`` 为幂等键，
重复调用返回首次结果，**不产生第二个版本号**——账本落在
``runs/<run_id>/generations/gen-{NNN}/version_ledger.json``。

**失败保守**：git 失败 → ``E_EVOLVE_GIT_FAILED`` 并把 ``harness/`` 与 ``version.json``
恢复到提交前的字节（``decision="reject"``，PRD §10）。
"""

from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any, Final, Mapping, Sequence

from dice_ratchet.infra import events
from dice_ratchet.infra.errors import E, InfraError
from dice_ratchet.infra.io import read_json, write_json_atomic

from .errors import GitFailedError, PatchInvalidError, VersionConflictError
from .paths import (EvolveContext, GenPaths, harness_dir as default_harness_dir,
                    harness_version_dir, snapshot_entries, utc_now)
from .proposer import apply_unified_diff
from .schemas import (Candidate, CandidateReport, GateState, HarnessRef, Verdict,
                      validate_gate_state, validate_harness_ref)

__all__ = [
    "LEDGER_NAME", "VERSION_FILE", "accept", "artifact_hash_of", "build_decision_sections",
    "commit_harness_version", "copy_harness_tree", "current_version", "diff_trees",
    "extract_version_file", "gate_state_for", "load_version", "next_version",
    "read_ledger", "rollback", "snapshot_harness", "stage_candidate", "staging_dir_for",
    "tree_digest", "unified_diff", "write_candidate_patch", "write_ledger",
    "write_version_pointer",
]

VERSION_FILE: Final[str] = "version.json"
LEDGER_NAME: Final[str] = "version_ledger.json"

#: ``version.json`` 的键全集（B-33：``base_model`` 预留给 MOD-10，M0–M3 恒为 null）。
#: ``version.json`` 的键全集（B-33：``base_model`` 预留给 MOD-10，M0–M3 恒为 null）。
#: ``diff_path`` 是本模块的扩展键（PRD §6.4 的 ``diff.patch`` 回链；``HarnessRef`` 需要）。
_VERSION_KEYS: Final[tuple[str, ...]] = ("version", "parent", "git_commit", "artifact_hash",
                                         "created_at", "reason", "accepted_by",
                                         "base_model", "diff_path")

#: 不进 ``versions/v{N}/`` 的条目（过程工件与历史快照）。
_EXCLUDED_FROM_SNAPSHOT: Final[tuple[str, ...]] = ("versions",)


# =====================================================================
# 目录/文件原语
# =====================================================================

def staging_dir_for(cfg: Any, *, run_id: str, generation: int, candidate_id: str) -> Path:
    """候选 staging 目录：``runs/<run_id>/generations/gen-{N}/staging/<id>/``。

    staging 里是**可执行**的 harness 副本（供 ``guard.gate`` 与 ``evaluate_arm`` 使用），
    与 ``versions/`` 的不可变快照互不影响。
    ``cfg`` 可以是 ``EvolveContext``（推荐）或任何带 ``runs_root`` 的对象。
    """
    root = getattr(cfg, "runs_root", None)
    base = Path(root) if root else Path("runs")
    return (base / str(run_id) / "generations" / f"gen-{int(generation):03d}"
            / "staging" / str(candidate_id))


def _iter_artifact_files(root: Path) -> list[Path]:
    """harness 工件的文件清单（排除 ``versions/`` 与隐藏文件）。"""
    out: list[Path] = []
    if not root.is_dir():
        return out
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.split("/", 1)[0] in _EXCLUDED_FROM_SNAPSHOT or rel.startswith("."):
            continue
        out.append(path)
    return out


def copy_harness_tree(src: Path, dst: Path, *,
                      exclude: Sequence[str] = _EXCLUDED_FROM_SNAPSHOT) -> list[str]:
    """全量拷贝 harness 工件（``src`` → ``dst``）；返回相对路径列表（字典序）。"""
    src, dst = Path(src), Path(dst)
    if not src.is_dir():
        raise VersionConflictError(f"harness 目录不存在：{src}",
                                   hint="先初始化 harness/（MOD-07 versioning 负责）")
    dst.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    skip = {str(name) for name in exclude}
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if rel.parts and rel.parts[0] in skip:
            continue
        if any(part.startswith(".") for part in rel.parts):
            continue
        target = dst / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied.append(rel.as_posix())
    return sorted(copied)


def tree_digest(root: Path, *, exclude: Sequence[str] = _EXCLUDED_FROM_SNAPSHOT) -> str:
    """``harness/`` 树的指纹（与 ``config.artifact_hash`` 同口径，但可排除 ``versions/``）。"""
    root = Path(root)
    skip = {str(name) for name in exclude}
    items: list[tuple[str, str]] = []
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if rel.split("/", 1)[0] in skip or rel.startswith("."):
                continue
            items.append((rel, hashlib.sha256(path.read_bytes()).hexdigest()))
    payload = json.dumps(items, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def artifact_hash_of(root: Path) -> str:
    """``harness/`` 的 artifact_hash（**不含** ``versions/``，Spec §5 的可演化层指纹）。"""
    return tree_digest(root, exclude=_EXCLUDED_FROM_SNAPSHOT)


# =====================================================================
# diff
# =====================================================================

def unified_diff(old: str, new: str, *, path: str) -> str:
    """产出 unified diff（``a/<path>`` / ``b/<path>`` 前缀，相对 harness/）。"""
    rel = str(path).replace("\\", "/").lstrip("./")
    lines = difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{rel}", tofile=f"b/{rel}", n=3)
    text = "".join(line if line.endswith("\n") else line + "\n" for line in lines)
    return text


def diff_trees(old_root: Path, new_root: Path) -> str:
    """两棵 harness 树的差异（``revert.patch`` / ``diff.patch`` 的离线重建口径）。"""
    old_root, new_root = Path(old_root), Path(new_root)
    names = sorted({p.relative_to(old_root).as_posix()
                    for p in _iter_artifact_files(old_root)}
                   | {p.relative_to(new_root).as_posix()
                      for p in _iter_artifact_files(new_root)})
    chunks: list[str] = []
    for name in names:
        left = old_root / name
        right = new_root / name
        old_text = left.read_text(encoding="utf-8") if left.is_file() else ""
        new_text = right.read_text(encoding="utf-8") if right.is_file() else ""
        if old_text == new_text:
            continue
        chunks.append(unified_diff(old_text, new_text, path=name))
    return "".join(chunks)


# =====================================================================
# 版本读写
# =====================================================================

def _read_version_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = read_json(path)
    except InfraError:
        return None
    return dict(raw) if isinstance(raw, Mapping) else None


def _version_payload(*, version: int, parent: int | None, git_commit: str,
                     artifact_hash: str, created_at: str, reason: str,
                     accepted_by: str, base_model: str | None = None,
                     diff_path: str = "") -> dict[str, Any]:
    return {"version": int(version), "parent": None if parent is None else int(parent),
            "git_commit": str(git_commit), "artifact_hash": str(artifact_hash),
            "created_at": str(created_at), "reason": str(reason),
            "accepted_by": str(accepted_by), "base_model": base_model,
            "diff_path": str(diff_path)}


def write_version_pointer(root: Path, payload: Mapping[str, Any]) -> str:
    """写 ``harness/version.json``（唯一写入口；先自校验再落盘）。"""
    body = {key: payload.get(key) for key in _VERSION_KEYS}
    ref = HarnessRef.from_dict({**body, "dir": Path(root).name or str(root),
                                "diff_path": ""})
    validate_harness_ref(ref.as_dict())
    return write_json_atomic(Path(root) / VERSION_FILE, body)


def current_version(harness: Path | str | None = None) -> int:
    """``harness/version.json`` 的当前版本号（缺失 → 0，v0 基线）。"""
    raw = _read_version_file(default_harness_dir(harness) / VERSION_FILE)
    if raw is None:
        return 0
    try:
        return max(0, int(raw.get("version", 0) or 0))
    except (TypeError, ValueError): # pragma: no cover - 损坏的 version.json
        return 0


def _existing_versions(root: Path) -> list[int]:
    versions_dir = Path(root) / "versions"
    out: list[int] = []
    if versions_dir.is_dir():
        for path in sorted(versions_dir.glob("v*")):
            suffix = path.name[1:]
            if path.is_dir() and suffix.isdigit():
                out.append(int(suffix))
    return sorted(out)


def next_version(root: Path) -> int:
    """``N`` = 现有最大 +1（不可复用；缺失 → 1）。"""
    versions = _existing_versions(Path(root))
    pointer = current_version(root)
    return max([pointer, *versions], default=0) + 1


def _ref_of(root: Path, *, version: int, parent: int | None, git_commit: str = "",
            created_at: str = "", reason: str = "", accepted_by: str = "auto",
            artifact_hash: str = "", diff_path: str = "") -> HarnessRef:
    return HarnessRef(version=int(version), dir=str(Path(root)), parent=parent,
                      git_commit=git_commit, created_at=created_at, reason=reason,
                      accepted_by=accepted_by, artifact_hash=artifact_hash,
                      base_model=None, diff_path=diff_path)


def load_version(v: int, *, harness: Path | str | None = None) -> HarnessRef:
    """载入某个版本快照的 ``HarnessRef``；``v == current`` 时读生效工件。

    目标既不是当前版本也无快照 → ``E_EVOLVE_VERSION_CONFLICT``（不自动 merge）。
    """
    root = default_harness_dir(harness)
    target = int(v)
    pointer = current_version(root)
    if target == pointer:
        raw = _read_version_file(root / VERSION_FILE) or {}
        recorded = str(raw.get("diff_path", "") or "")
        if recorded:
            candidate_path = Path(root).resolve().parent / recorded
            if candidate_path.is_file():
                recorded = str(candidate_path)
        return _ref_of(root, version=pointer, parent=raw.get("parent"),
                       git_commit=str(raw.get("git_commit", "")),
                       created_at=str(raw.get("created_at", "")),
                       reason=str(raw.get("reason", "")),
                       accepted_by=str(raw.get("accepted_by", "auto")),
                       artifact_hash=str(raw.get("artifact_hash", "")),
                       diff_path=recorded)
    snap = harness_version_dir(target, harness=root)
    if not snap.is_dir():
        raise VersionConflictError(
            f"版本 v{target} 不存在（当前 v{pointer}）",
            hint=f"可用版本：{[pointer, *_existing_versions(root)]}；"
                 "版本号不可复用、不可重写历史（FR-07-9）")
    raw = _read_version_file(snap / VERSION_FILE) or {}
    recorded = str(raw.get("diff_path", "") or "")
    if not recorded:
        recorded = str(snap / ("revert.patch" if (snap / "revert.patch").is_file()
                               else "diff.patch"))
    return _ref_of(snap, version=target, parent=raw.get("parent"),
                   git_commit=str(raw.get("git_commit", "")),
                   created_at=str(raw.get("created_at", "")),
                   reason=str(raw.get("reason", "")),
                   accepted_by=str(raw.get("accepted_by", "auto")),
                   artifact_hash=str(raw.get("artifact_hash", "")),
                   diff_path=recorded)


def extract_version_file(root: Path | str) -> dict[str, Any]:
    """读 ``harness/version.json``（不存在 → 空字典，便于血缘比较）。"""
    raw = _read_version_file(default_harness_dir(root) / VERSION_FILE)
    return dict(raw or {})


# =====================================================================
# staging（FR-07-13 的前置：候选代码必须在独立目录里被扫描与加载）
# =====================================================================

def stage_candidate(cand: Candidate, *, run_id: str, cfg: Any,
                    harness: Path | str | None = None,
                    generation: int = 0) -> Path:
    """把候选落到 ``staging/<candidate_id>/`` 并应用 diff（返回 staging 目录）。

    步骤：① 全量拷贝生效工件 → ② ``apply_unified_diff`` 落改动 → ③ 回写
    ``version.json``（``version`` 保持 parent 值：候选尚未 accept）。
    diff 无法应用 → ``E_EVOLVE_PATCH_INVALID``（该候选 reject，**不进**冒烟/heldout）。
    """
    root = default_harness_dir(harness)
    staging = staging_dir_for(cfg, run_id=run_id, generation=generation,
                              candidate_id=cand.candidate_id)
    if staging.exists():
        shutil.rmtree(staging)
    copy_harness_tree(root, staging)
    apply_unified_diff(staging, cand.diff, paths=cand.touched_files or ())
    events.emit("evolve.validate", candidate_id=cand.candidate_id, status="pass",
                diff_lines=int(cand.diff_lines), touched_files=list(cand.touched_files))
    return staging


def write_candidate_patch(cand: Candidate, *, run_root: Path | str, gen: int) -> Path:
    """把候选 diff 落成 ``patches/{candidate_id}.patch``（FR-07-12：文本进 git 便于审计）。"""
    path = GenPaths.of(run_root, gen).patch(cand.candidate_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_ensure_newline(cand.diff), encoding="utf-8")
    return path


# =====================================================================
# 账本（幂等键：run_id + generation）
# =====================================================================

def _ledger_path(ctx: Any, *, run_id: str, generation: int) -> Path:
    ev = _as_context(ctx, run_id=run_id, generation=generation)
    return ev.ledger_path(generation)


def _as_context(ctx: Any, *, run_id: str, generation: int) -> EvolveContext:
    """把 ``EvolveContext`` / ``AppConfig`` / ``run_ctx`` 统一归一到根路径视图。"""
    if isinstance(ctx, EvolveContext):
        return ctx
    paths = getattr(ctx, "paths", None)
    root = getattr(paths, "root", None) if paths is not None else None
    harness = getattr(ctx, "harness_dir", None)
    if root is not None:
        return EvolveContext(run_id=str(getattr(ctx, "run_id", run_id)),
                             root=Path(root), harness=Path(harness or "harness"),
                             runs_root=Path(root).parent)
    return EvolveContext.of(ctx, run_id=run_id, harness=harness)


def read_ledger(path: Path) -> dict[str, Any]:
    raw = _read_version_file(path)
    return dict(raw or {})


def write_ledger(path: Path, payload: Mapping[str, Any]) -> str:
    return write_json_atomic(path, dict(payload))


def _ledger_entry(ledger: Mapping[str, Any], generation: int) -> Mapping[str, Any] | None:
    entry = ledger.get(f"gen-{int(generation):03d}")
    return entry if isinstance(entry, Mapping) else None


# =====================================================================
# git 封装（只经 MOD-09 git_ops；禁裸 subprocess）
# =====================================================================

def _git_ops() -> Any:
    from dice_ratchet.infra import git_ops

    return git_ops


def _repo_root(harness: Path) -> Path:
    """harness 的仓库根（``harness/`` 的父目录）。"""
    return Path(harness).resolve().parent


def commit_harness_version(harness: Path, *, version: int, message: str,
                           tag: bool = True, allow_dirty: bool = True) -> str:
    """经 MOD-09 ``commit_harness`` 提交 ``harness/`` 并打 tag ``harness-v{N}``。

    git 不可用 → 降级（``E_IO_GIT_UNAVAILABLE``，返回空 sha，不阻断演化）；
    git 可用但命令失败 → ``E_EVOLVE_GIT_FAILED``（PRD §10：丢弃未提交 accept）。
    """
    ops = _git_ops()
    repo = _repo_root(harness)
    try:
        if not ops.git_available(repo):
            return ""
    except InfraError:
        return ""
    try:
        sha = ops.commit_harness(repo, int(version), str(message))
        if tag:
            ops.tag_version(repo, int(version))
    except InfraError as exc:
        raise GitFailedError(f"harness 提交失败（v{version}）：{exc}",
                             hint="git commit/tag 失败：不自动 merge，人工介入后重试"
                             ) from exc
    return str(sha)


# =====================================================================
# accept（FR-07-9 / NFR-07-6）
# =====================================================================

def accept(cand: Candidate, verdict: Verdict, *, run_id: str, cfg: Any,
           harness: Path | str | None = None, generation: int | None = None,
           decided_by: str | None = None, reason: str | None = None,
           now: Any = None) -> HarnessRef:
    """把候选落成不可变版本（幂等：``(run_id, generation)``）。

    ``generation`` 缺省时取 ``verdict.generation``；``decided_by`` 缺省时取
    ``verdict.decided_by``（HITL 时人工裁决会写 ``"human"``，FR-07-11）。
    """
    root = default_harness_dir(harness)
    gen = int(verdict.generation if generation is None else generation)
    by = str(decided_by or verdict.decided_by or "auto")
    ledger_path = _ledger_path(cfg, run_id=run_id, generation=gen)
    ledger = read_ledger(ledger_path)
    prior = _ledger_entry(ledger, gen)
    if prior and prior.get("action") == "accept" and prior.get("ref"):
        return HarnessRef.from_dict(prior["ref"])

    # ---- 幂等：同代同候选已产生版本号 → 复用（不产生第二个版本号）-------------------
    existing = {int(v) for v in (prior or {}).get("versions", [])} if prior else set()
    if existing: # pragma: no cover - 由账本分支覆盖，保留防御
        return load_version(max(existing), harness=root)

    parent = current_version(root)
    new_version = next_version(root)
    reason_text = str(reason if reason is not None else cand.rationale)[:300]
    created_at = utc_now(now)
    diff_path_rel = f"harness/versions/v{new_version}/diff.patch"

    # ---- 1) 应用候选到生效工件（先备份，任何失败都恢复）-----------------------------
    backup = Path(root) / f".backup-v{parent}"
    if backup.exists(): # pragma: no cover - 上次崩溃遗留
        shutil.rmtree(backup, ignore_errors=True)
    copy_harness_tree(root, backup)
    previous_version_file = _read_version_file(root / VERSION_FILE)
    sha = ""
    try:
        apply_unified_diff(root, cand.diff, paths=cand.touched_files or ())
        # ---- 2) 写不可变快照（全量拷贝 + version.json + diff.patch）-----------------
        snap = harness_version_dir(new_version, harness=root)
        if snap.exists():
            raise VersionConflictError(f"版本目录已存在：{snap}",
                                       hint="版本号不可复用（FR-07-9）")
        copy_harness_tree(root, snap)
        artifact = artifact_hash_of(root)
        payload = _version_payload(
            version=new_version, parent=parent,
            git_commit=str((previous_version_file or {}).get("git_commit", "")),
            artifact_hash=artifact, created_at=created_at, reason=reason_text,
            accepted_by=by, diff_path=diff_path_rel)
        write_version_pointer(root, payload)
        write_json_atomic(snap / VERSION_FILE, dict(payload))
        (snap / "diff.patch").write_text(_ensure_newline(cand.diff), encoding="utf-8")

        # ---- 3) git 提交 + tag ------------------------------------------------------
        message = _commit_message(gen=gen, version=new_version, kind=cand.kind,
                                  rationale=reason_text)
        sha = commit_harness_version(root, version=new_version, message=message)
        payload["git_commit"] = sha
        write_version_pointer(root, payload)
        write_json_atomic(snap / VERSION_FILE, dict(payload))
    except VersionConflictError:
        raise
    except GitFailedError:
        _restore(root, backup, previous_version_file)
        events.emit("evolve.version", from_version=parent, to_version=new_version,
                    git_commit="", action="failed")
        raise
    except (PatchInvalidError, InfraError):
        _restore(root, backup, previous_version_file)
        raise
    except Exception:
        _restore(root, backup, previous_version_file)
        raise
    finally:
        shutil.rmtree(backup, ignore_errors=True)

    ref = _ref_of(root, version=new_version, parent=parent, git_commit=sha,
                  created_at=created_at, reason=reason_text, accepted_by=by,
                  artifact_hash=payload["artifact_hash"], diff_path=diff_path_rel)
    events.emit("evolve.version", from_version=parent, to_version=new_version,
                git_commit=sha, action="accept")
    events.emit("evolve.decision", gen=gen, candidate_id=cand.candidate_id,
                decision="accept", decided_by=by, reason=reason_text[:200],
                error_code=None)
    ledger[f"gen-{gen:03d}"] = {"action": "accept", "generation": gen,
                                "candidate_id": cand.candidate_id,
                                "versions": sorted({*existing, new_version}),
                                "ref": ref.as_dict(), "at": created_at,
                                "decided_by": by, "reason": reason_text}
    write_ledger(ledger_path, ledger)
    return ref


def _ensure_newline(text: str) -> str:
    body = str(text)
    return body if body.endswith("\n") else body + "\n"


def _commit_message(*, gen: int, version: int, kind: str, rationale: str) -> str:
    """``harness(v{N}): gen-{G} {kind} {short_rationale}``（FR-07-9）。"""
    short = " ".join(str(rationale).split())[:72]
    return f"gen-{int(gen)} {kind} {short}".strip()


def _rollback_files(root: Path, backup: Path) -> None:
    """用备份目录恢复 ``harness/`` 工件（失败路径）。"""
    if not backup.is_dir(): # pragma: no cover - 备份缺失
        return
    for path in sorted(root.iterdir()) if root.is_dir() else []:
        if path.name in ("versions", ".git") or path.name.startswith(".backup-"):
            continue
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    copy_harness_tree(backup, root)


def _restore(root: Path, backup: Path, previous_version_file: Mapping[str, Any] | None
             ) -> None:
    """把 ``harness/`` 与 ``version.json`` 恢复到操作之前的字节（失败保守，PRD §10）。"""
    try:
        _rollback_files(root, backup)
        if previous_version_file is not None:
            write_json_atomic(Path(root) / VERSION_FILE, dict(previous_version_file))
    except Exception: # noqa: BLE001 - 恢复路径不得再抛（否则掩盖原始错误）
        pass


# =====================================================================
# rollback（FR-07-10 / §8.4）
# =====================================================================

def rollback(to_version: int, *, run_id: str, reason: str, decided_by: str,
             cfg: Any, harness: Path | str | None = None, generation: int = 0,
             now: Any = None) -> HarnessRef:
    """回滚到 ``versions/v{to_version}/``（幂等：``(run_id, generation)``）。

    校验 ``v`` 存在且 ``v <= current``；替换 ``harness/`` 工件；``version = current + 1``
    （编号不回退）；生成 ``revert.patch``；提交并打 tag ``harness-v{new}``。
    """
    root = default_harness_dir(harness)
    gen = int(generation)
    ledger_path = _ledger_path(cfg, run_id=run_id, generation=gen)
    ledger = read_ledger(ledger_path)
    prior = _ledger_entry(ledger, gen)
    if prior and prior.get("action") == "rollback" and prior.get("ref"):
        return HarnessRef.from_dict(prior["ref"])

    target = int(to_version)
    parent = current_version(root)
    if target > parent:
        raise VersionConflictError(
            f"回滚目标 v{target} 高于当前 v{parent}：不允许向前回滚",
            hint="rollback 只能回到已存在的历史版本（FR-07-10）")
    if target == parent:
        # 已在该版本：不产生新版本号，直接返回当前引用（幂等）
        return load_version(parent, harness=root)

    snap = harness_version_dir(target, harness=root)
    if not snap.is_dir():
        raise VersionConflictError(
            f"回滚目标快照不存在：{snap}",
            hint=f"可用版本：{_existing_versions(root)}")

    new_version = next_version(root)
    created_at = utc_now(now)
    created_by = str(decided_by or "human")
    reason_text = str(reason or "manual_rollback")[:300]

    backup = Path(root) / f".backup-v{parent}"
    if backup.exists(): # pragma: no cover
        shutil.rmtree(backup, ignore_errors=True)
    copy_harness_tree(root, backup)
    previous_version_file = _read_version_file(root / VERSION_FILE)
    try:
        revert_patch = diff_trees(root, snap)
        # 先算出新指针内容，再替换工件（revert.patch 基于 current → target）
        payload = _version_payload(
            version=new_version, parent=parent,
            git_commit=str((previous_version_file or {}).get("git_commit", "")),
            artifact_hash="", created_at=created_at, reason=reason_text,
            accepted_by=created_by,
            diff_path=f"harness/versions/v{new_version}/revert.patch")
        _replace_artifacts(root, snap)
        artifact = artifact_hash_of(root)
        payload["artifact_hash"] = artifact
        write_version_pointer(root, payload)

        new_snap = harness_version_dir(new_version, harness=root)
        copy_harness_tree(root, new_snap)
        (new_snap / "revert.patch").write_text(_ensure_newline(revert_patch),
                                               encoding="utf-8")
        write_json_atomic(new_snap / VERSION_FILE, dict(payload))

        message = (f"revert(harness): back to v{target} — {reason_text[:60]}".strip())
        try:
            sha = commit_harness_version(root, version=new_version, message=message)
        except GitFailedError:
            _restore(root, backup, previous_version_file)
            events.emit("evolve.rollback", to_version=target, git_commit="",
                        action="failed")
            raise
        except Exception:
            _restore(root, backup, previous_version_file)
            raise
        payload["git_commit"] = sha
        write_version_pointer(root, payload)
        write_json_atomic(new_snap / VERSION_FILE, dict(payload))
    finally:
        shutil.rmtree(backup, ignore_errors=True)

    ref = HarnessRef(version=new_version, dir=str(root), parent=parent, git_commit=sha,
                     created_at=created_at, reason=reason_text, accepted_by=created_by,
                     artifact_hash=payload["artifact_hash"],
                     diff_path=f"harness/versions/v{new_version}/revert.patch")
    events.emit("evolve.rollback", to_version=target, from_version=parent,
                new_version=new_version, git_commit=sha, decided_by=created_by)
    events.emit("evolve.decision", gen=gen, candidate_id="", decision="rollback",
                decided_by=created_by, reason=reason_text[:200],
                error_code=(None if reason_text != "hitl_timeout"
                            else str(E.E_EVOLVE_GATE_TIMEOUT)))
    ledger[f"gen-{gen:03d}"] = {"action": "rollback", "generation": gen,
                                "to_version": target, "ref": ref.as_dict(),
                                "at": created_at, "decided_by": created_by,
                                "reason": reason_text}
    write_ledger(ledger_path, ledger)
    return ref


def _replace_artifacts(root: Path, snap: Path) -> None:
    """用快照覆盖生效工件（保留 ``versions/`` 与隐藏文件）。"""
    for path in sorted(root.iterdir()) if root.is_dir() else []:
        if path.name in _EXCLUDED_FROM_SNAPSHOT or path.name.startswith("."):
            continue
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    copy_harness_tree(snap, root)


def snapshot_harness(version: int, *, harness: Path | str | None = None,
                     source: Path | str | None = None, cfg: Any = None,
                     run_id: str = "", reason: str = "manual snapshot",
                     accepted_by: str = "human", now: Any = None) -> HarnessRef:
    """把当前（或 ``source``）工件落成 ``versions/v{N}/`` 快照（**不提交 git**）。

    M2 冒烟与 M1 收口用：``harness/versions/v0/`` 由它建立（B-29 的全量快照形态）。
    """
    root = default_harness_dir(harness)
    src = Path(source) if source is not None else root
    snap = harness_version_dir(version, harness=root)
    if snap.exists():
        raise VersionConflictError(f"版本目录已存在：{snap}（版本号不可复用）")
    copy_harness_tree(src, snap)
    payload = _version_payload(
        version=int(version), parent=_parent_of(root), git_commit="",
        artifact_hash=artifact_hash_of(src), created_at=utc_now(now),
        reason=str(reason), accepted_by=str(accepted_by))
    write_json_atomic(snap / VERSION_FILE, dict(payload))
    return _ref_of(snap, version=int(version), parent=payload["parent"],
                   created_at=payload["created_at"], reason=str(reason),
                   accepted_by=str(accepted_by), artifact_hash=payload["artifact_hash"])


def _parent_of(root: Path) -> int | None:
    raw = _read_version_file(Path(root) / VERSION_FILE)
    if raw is None or raw.get("parent") is None:
        return None
    try:
        return int(raw["parent"])
    except (TypeError, ValueError): # pragma: no cover
        return None


# =====================================================================
# 决策段（report.json 的 candidates/decision/decided_by/reason）
# =====================================================================

def build_decision_sections(gen: int, reports: Sequence[CandidateReport],
                            winner: CandidateReport | None,
                            gate: GateState | None) -> dict[str, Any]:
    """只产出 ``candidates`` / ``decision`` / ``decided_by`` / ``reason`` 四段（§6.3）。

    多候选时记录最终接受者（或最后尝试者），其余写 ``candidates/`` 明细；
    ``gate.status == "pending"`` → ``decision="pending"``（HITL 挂起，FR-07-11）。
    """
    entries: list[dict[str, Any]] = []
    for report in reports:
        verdict = report.verdict
        decision = (verdict.decision if verdict is not None
                    else ("reject" if report.error_code else "reject"))
        reason = report.reason or (verdict.reason if verdict is not None else "")
        entries.append({
            "candidate_id": report.candidate_id,
            "kind": report.candidate.kind,
            "decision": decision,
            "decided_by": (verdict.decided_by if verdict is not None else "auto"),
            "reason": reason,
            "error_code": report.error_code,
            "expected_effect": report.candidate.expected_effect.as_tuple(),
        })

    pending = gate is not None and gate.status == "pending"
    if pending:
        decision, decided_by = "pending", (gate.resolved_by or "auto")
        reason = gate.reason or "hitl_pending"
    elif gate is not None and gate.status == "timeout":
        decision, decided_by, reason = "rollback", "auto", "hitl_timeout"
    elif gate is not None and gate.status in ("approved", "rejected"):
        decision = "accept" if gate.status == "approved" else "rollback"
        decided_by, reason = (gate.resolved_by or "human"), gate.reason
    elif winner is not None:
        decision = "accept"
        decided_by = (winner.verdict.decided_by if winner.verdict is not None else "auto")
        reason = ""
    elif entries:
        last = entries[-1]
        decision, decided_by, reason = "reject", last["decided_by"], last["reason"]
    else:
        decision, decided_by, reason = "reject", "auto", "no_candidate"

    return {"candidates": entries, "decision": decision, "decided_by": decided_by,
            "reason": str(reason or "")}


def gate_state_for(*, generation: int, status: str, harness_version: int,
                   parent_version: int, diff_path: Path | str = "",
                   report_path: Path | str = "", expires_at: str = "",
                   created_at: str = "", resolved_by: str = "", reason: str = "",
                   candidate_id: str = "", error_code: str | None = None) -> GateState:
    """构造并校验一个 ``GateState``（``gate.json`` 的落盘形态）。"""
    state = GateState(generation=int(generation), status=str(status),
                      harness_version=int(harness_version),
                      parent_version=int(parent_version), diff_path=str(diff_path),
                      report_path=str(report_path), expires_at=str(expires_at),
                      created_at=str(created_at), resolved_by=str(resolved_by),
                      reason=str(reason), candidate_id=str(candidate_id),
                      error_code=error_code)
    validate_gate_state(state.as_dict())
    return state
