"""M3 彩排的机器可读合格判据（D-M3-5 裁决 A；Spec v2.14 / B-70）。

**为什么要有这个模块**：M2 的验收判据是「机制性失败为零」（[M2 检查单 §3.2](../../docs/tasks/2026-10-06-M2开工检查单.md) 的
正向白名单），那套判据是**人读文档 + 人核对**。M3 的彩排要回答的是「正式实验起跑前
全链路能不能走通」，如果判据还靠人眼，彩排本身就可能给出假阳性——`F48` 刚证明过这件事：
「5 张 PNG 存在」这条判据**会被空白图通过**。

因此本模块把彩排判据写成**可执行**的：

1. **机制性失败为零** —— 逐字复刻 M2 §3.2 的 5 条不合格条件 + 裁判拒绝正向白名单；
2. **5 张 PNG 有数据点** —— 把台账重渲染一遍，**数每个 `Line2D`/`BarContainer` 的实际点数**。
   （不用「文件大小」也不用「非白像素比例」：实测空白图仍有 3.4% 墨迹，来自坐标轴与图例，
   两者都分不开空白图。）
3. **台账含 heldout 与 train 行** —— F48 修的正是「train 一行都没有」；
4. **判据 1 判定器能给出结论** —— `eval acceptance` 在彩排数据上跑得出 `acceptance.json`
   （`ok` 是真是假不重要，重要的是**判定器能产出结论**，且至少有一对可比）；
5. **每代臂归档 `metrics/arms/` 有文件** —— F42-A 的产物在真实链路里确实落盘；
6. **一次性资源未被消耗** —— 扫 `episodes/heldout/` 的实际 seed：**不得**落在
   H1(731000–731039) / H2(731040–731079) / test(947000–947099)；且不得有 `test_attestation.json`。
   这一条把「不碰一次性资源」从文档纪律变成**代码断言**（D-M3-5 的核诉求）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

from dice_ratchet.infra.errors import E, InfraError

__all__ = ["REHEARSAL_VERSION", "REJECT_WHITELIST", "CheckResult", "RehearsalReport",
           "run_rehearsal_checks", "PLOT_FILES_REQUIRED"]

REHEARSAL_VERSION: Final[str] = "rehearsal-v1"

#: 裁判**拒绝**的白名单（正向枚举，M2 §3.2 第 4 条；只认裁判结论）。
#: 其余 `E_EVOLVE_*` 一律算机制性失败——正向枚举是刻意的：负向死名单已经漏过一次
#: （`E_EVOLVE_GIT_FAILED` / `E_EVOLVE_VERSION_CONFLICT` 都不在旧清单里，且退出码为 0）。
REJECT_WHITELIST: Final[frozenset[str]] = frozenset({
    "E_EVOLVE_NO_GAIN",           # C1–C3
    "E_EVOLVE_OVERFIT_SUSPECT",   # C3
    "E_EVOLVE_COLLAPSE",          # C4
    "E_EVOLVE_ILLEGAL_RATE",      # C5（含 fallback / truncation 轴）
    "E_EVOLVE_GATE_TIMEOUT",      # HITL 超时 → rollback
})

#: 彩排必须产出的 5 张图（MOD-09 FR-09-13）。
PLOT_FILES_REQUIRED: Final[tuple[str, ...]] = (
    "win_rate.png", "stages_cleared.png", "hearts_left.png",
    "entropy.png", "train_heldout_gap.png",
)

#: 一次性资源的 seed 区间（**彩排里绝不允许出现**）。
_FORBIDDEN_ZONES: Final[tuple[tuple[str, int, int], ...]] = (
    ("H1", 731000, 731039),
    ("H2", 731040, 731079),
    ("test", 947000, 947099),
)
#: 彩排**允许**的 heldout 区间（H3，reserve，可重复用）。
_ALLOWED_HELDOUT: Final[tuple[str, int, int]] = ("H3", 731080, 731099)


@dataclass(frozen=True)
class CheckResult:
    """一条判据的结果。``detail`` 必须能解释「为什么过/不过」。"""

    name: str
    ok: bool
    detail: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"check": self.name, "ok": bool(self.ok), "detail": str(self.detail),
                "evidence": dict(self.evidence)}


@dataclass(frozen=True)
class RehearsalReport:
    """彩排合格判据的整体结论。"""

    run_id: str
    checks: tuple[CheckResult, ...] = ()
    version: str = REHEARSAL_VERSION

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    @property
    def failed(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if not c.ok)

    def as_dict(self) -> dict[str, Any]:
        return {"version": self.version, "run_id": self.run_id, "ok": self.ok,
                "failed": [c.name for c in self.failed],
                "checks": [c.as_dict() for c in self.checks]}


# =====================================================================
# 判据 1：机制性失败为零
# =====================================================================

def _decision_records(run_dir: Path) -> list[dict[str, Any]]:
    """各代 ``report.json`` 的判决记录（缺失/损坏 → 跳过并留痕）。"""
    out: list[dict[str, Any]] = []
    for path in sorted((run_dir / "generations").glob("gen-*/report.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            out.append({"__broken__": str(path)})
            continue
        raw["__path__"] = str(path)
        out.append(raw)
    return out


def _mechanical_failures(records: Sequence[Mapping[str, Any]], run_dir: Path
                         ) -> list[str]:
    """M2 §3.2 的 5 条不合格条件（正向白名单版）。"""
    problems: list[str] = []
    for rec in records:
        path = str(rec.get("__path__", "?"))
        gen = rec.get("generation")
        if rec.get("__broken__"):
            problems.append(f"{path}: report.json 不可解析")
            continue
        decision = str(rec.get("decision") or "")
        # ① error_reject = 基础设施失败的唯一出口
        if decision == "error_reject":
            problems.append(f"gen-{gen}: decision=error_reject（基础设施失败）"
                            f" code={rec.get('error_code')}")
        # ③ E_IO_* 出现在任一 error_code 上（含候选级）
        codes = [str(rec.get("error_code") or "")]
        for cand in (rec.get("candidates") or []):
            codes.append(str(cand.get("error_code") or ""))
        for code in codes:
            if code.startswith("E_IO_"):
                problems.append(f"gen-{gen}: E_IO_* 出现在跑完的 run 里（{code}）")
        # ④ E_EVOLVE_* 且不在裁判拒绝白名单内
        for code in codes:
            if code.startswith("E_EVOLVE_") and code not in REJECT_WHITELIST:
                problems.append(f"gen-{gen}: {code} 不在裁判拒绝白名单内（机制性失败）")
        # ⑤ 幻影 accept：decision=accept 但 harness/versions/ 没有对应新版本
        if decision == "accept":
            version = rec.get("harness_version")
            vdir = run_dir.parent / "harness" / "versions" / f"v{version}"
            if version is not None and not vdir.exists():
                problems.append(f"gen-{gen}: decision=accept 但 {vdir} 不存在（幻影 accept）")
    return problems


# =====================================================================
# 判据 2：5 张 PNG **有数据点**（不是「文件存在」）
# =====================================================================

def _count_points(fig: Any) -> int:
    """数一张 figure 里**真实画进去的数据点**（Line2D 的 xydata + 柱子的 patches）。"""
    total = 0
    for ax in fig.axes:
        for line in ax.get_lines():
            total += len(line.get_xydata())
        for container in ax.containers:
            total += len(getattr(container, "patches", ()) or ())
    return total


def _plot_data_points(csv_path: Path) -> dict[str, int]:
    """把台账**重渲染一遍**，数每张图真实画进去的数据点数。

    **为什么必须重渲染而不是查文件**：实测空白图 20–27 KB、非白像素比例 3.43%，
    有数据的图 3.60% —— 「文件存在」「文件大小」「像素比例」三个判据**都分不开**空白图
    （坐标轴与图例本身就贡献 3.4% 墨迹）。唯一可靠的是读**画进 figure 的对象**：
    `plot_generations(figures=[...])` 会把 figure 交出来，`Line2D.get_xydata()` 数点数。

    返回 ``{png 文件名: 数据点数}``；0 表示该图没有数据（正是 F48 的病）。
    """
    import matplotlib.pyplot as plt

    from dice_ratchet.infra.plotting import plot_generations

    import tempfile

    figures: list[tuple[str, Any]] = []
    # 画到临时目录：**不覆盖** run 目录下已经落盘的 PNG（那些是交付物，判据只读不写）。
    with tempfile.TemporaryDirectory(prefix="rehearsal-plots-") as tmp:
        try:
            plot_generations(csv_path, Path(tmp), figures=figures)
            counts = {name: _count_points(fig) for name, fig in figures}
        finally:
            plt.close("all")
    return counts


# =====================================================================
# 判据 6：一次性资源未被消耗
# =====================================================================

def _heldout_seeds_used(run_dir: Path) -> set[int]:
    """从 ``episodes/heldout/`` 的实际轨迹里取 seed（**不信任文件名**）。

    文件名可能被重命名/伪造，而行里的 ``seed`` 是 MOD-02/MOD-06 写下的原始值。
    """
    seeds: set[int] = set()
    root = run_dir / "episodes" / "heldout"
    if not root.is_dir():
        return seeds
    for path in root.glob("*.jsonl"):
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("seed") is not None:
                        seeds.add(int(row["seed"]))
                    if row.get("type") == "episode_end" and row.get("seed") is not None:
                        seeds.add(int(row["seed"]))
        except OSError: # pragma: no cover - 读不到的局不参与判定
            continue
    return seeds


# =====================================================================
# 入口
# =====================================================================

def run_rehearsal_checks(run_dir: Path | str, *, run_id: str = "",
                         min_comparable: int = 1) -> RehearsalReport:
    """跑全部彩排判据。**不做任何付费调用**（只读已落盘的产物）。"""
    run_path = Path(run_dir)
    checks: list[CheckResult] = []

    # ① 机制性失败为零
    records = _decision_records(run_path)
    problems = _mechanical_failures(records, run_path)
    checks.append(CheckResult(
        "mechanical_failures_zero", not problems,
        "机制性失败为零" if not problems else f"{len(problems)} 项：{problems[:4]}",
        {"generations": len(records), "problems": problems}))

    # ② 5 张 PNG 有数据点（文件必须存在 + 数据点数必须 > 0）
    plots_dir = run_path / "plots"
    missing = [n for n in PLOT_FILES_REQUIRED if not (plots_dir / n).is_file()]
    counts: dict[str, int] = {}
    plot_err = ""
    if missing:
        plot_err = f"缺 {missing}"
    else:
        csv_path = run_path / "metrics" / "generations.csv"
        try:
            counts = _plot_data_points(csv_path)
        except (InfraError, OSError) as exc:
            plot_err = f"{type(exc).__name__}: {exc}"
        zero = [n for n, c in counts.items() if c <= 0]
        if zero:
            plot_err = f"以下图没有数据点：{zero}"
    checks.append(CheckResult(
        "plots_have_data_points", not plot_err,
        plot_err or "5 张 PNG 均有数据点（不是「文件存在」）",
        {"missing": missing, "data_points": counts}))

    # ③ 台账含 heldout 与 train 行
    csv_path = run_path / "metrics" / "generations.csv"
    splits: dict[str, int] = {}
    ledger_err = ""
    if not csv_path.is_file():
        ledger_err = "缺 metrics/generations.csv"
    else:
        import csv as _csv

        with csv_path.open(encoding="utf-8", newline="") as handle:
            for row in _csv.DictReader(handle):
                key = str(row.get("split") or "").strip() or "heldout"
                splits[key] = splits.get(key, 0) + 1
        if splits.get("heldout", 0) <= 0:
            ledger_err = "台账没有 heldout 行"
        elif splits.get("train", 0) <= 0:
            ledger_err = ("台账没有 train 行（F48 的原始缺陷：train 对照曲线与 "
                          "win_rate_gap 在台账里没有可核验来源）")
    checks.append(CheckResult(
        "ledger_has_both_splits", not ledger_err,
        ledger_err or f"heldout={splits.get('heldout')} / train={splits.get('train')}",
        {"splits": splits}))

    # ④ 判据 1 判定器能给出结论
    from .acceptance import evaluate_acceptance

    acc_err = ""
    comparable = 0
    acc_ok: Any = None
    try:
        rep = evaluate_acceptance(run_path, run_id=run_id or run_path.name)
        comparable = len(rep.comparable)
        acc_ok = rep.ok
        if comparable < int(min_comparable):
            acc_err = (f"可比较的代只有 {comparable} 对（< {min_comparable}）⇒ "
                       "判据 1 判定器给不出「单调性」结论；彩排至少跑 2 代")
        if not (run_path / "metrics" / "acceptance.json").is_file():
            acc_err = acc_err or "缺 metrics/acceptance.json（eval acceptance 未跑过）"
    except InfraError as exc:
        acc_err = f"{exc.code}: {exc.message}"
    checks.append(CheckResult(
        "acceptance_judge_runs", not acc_err,
        acc_err or f"判定器产出结论（comparable={comparable}, ok={acc_ok}）",
        {"comparable": comparable, "acceptance_ok": acc_ok}))

    # ⑤ 每代臂归档存在
    arms_dir = run_path / "metrics" / "arms"
    arm_files = sorted(p.name for p in arms_dir.glob("*.json")) if arms_dir.is_dir() else []
    arm_err = "" if arm_files else "metrics/arms/ 下没有臂归档文件（F42-A 的产物）"
    checks.append(CheckResult(
        "arm_archive_present", not arm_err,
        arm_err or f"{len(arm_files)} 个臂归档",
        {"files": arm_files[:8], "count": len(arm_files)}))

    # ⑥ 一次性资源未被消耗
    used = _heldout_seeds_used(run_path)
    violations: list[str] = []
    for label, lo, hi in _FORBIDDEN_ZONES:
        hit = sorted(s for s in used if lo <= s <= hi)
        if hit:
            violations.append(f"{label}({lo}-{hi}) 被用了 {len(hit)} 个 seed：{hit[:5]}")
    att = list((run_path / "generations").glob("gen-*/test_attestation.json"))
    if att:
        violations.append(f"存在 test_attestation.json：{[str(p) for p in att]}")
    outside = sorted(s for s in used
                     if not (_ALLOWED_HELDOUT[1] <= s <= _ALLOWED_HELDOUT[2])
                     and not (1000 <= s <= 1999))
    if outside:
        violations.append(f"heldout 用了声明之外的 seed：{outside[:5]}"
                          f"（只允许 H3 {_ALLOWED_HELDOUT[1]}–{_ALLOWED_HELDOUT[2]}）")
    checks.append(CheckResult(
        "one_shot_resources_untouched", not violations,
        "；".join(violations) or f"heldout seeds 全在 H3 内（{len(used)} 个），无 test 存证",
        {"heldout_seeds": sorted(used), "violations": violations}))

    return RehearsalReport(run_id=str(run_id or run_path.name), checks=tuple(checks))
