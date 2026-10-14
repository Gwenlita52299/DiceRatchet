"""判据 1 的判定器（M3 验收；F-M3-9，Spec v2.13 / B-69）。

**为什么需要这个模块**：MOD-08 §12 判定表第 1 行与 [M3 检查单 §1](../../docs/tasks/2026-10-11-M3验收前置条件确认.md)
的通过条件写的是「`ci_lo(g) ≥ win_rate(g−1) − 0.02` **或** `P(gap>0) ≥ 0.90`，
`monotonic_ok=true`，且 ≥1 次 accept」，但 ``grep -rn monotonic_ok dice_ratchet/ schemas/ scripts/``
在 2026-10-11 之前是 **0 命中**（该标识符只在文档里出现 3 次）——即**口径定了、判定器没写**。
M3 跑完 3 代后「判据 1 通过/不通过」这句话无法由代码产出，只能人肉读 csv，
而那正是 F46-A 刚修掉的病（台账数字必须有物理含义，判据也必须有机读结论）。

**三处必须写清的口径**（D-M3-4 裁决 A 的同类纪律：口径不能靠读者猜）：

1. **`g−1` 指「上一个已存在且 ``split="heldout"`` 的代」**，不是字面 ``g−1``：
   ``gen-000`` 是**基线**（``decision="baseline"``），拿它当「上一代」会把
   「基线 vs 第一代」混进单调性判定——那是判据 2 的活；中断过的 run 也可能缺代。
2. **`P(gap>0)` 的定义（本模块补的定义，写回 MOD-08 §12）**：**相邻两代 heldout
   的 ``mean_stages_cleared`` 的配对 bootstrap** 中 ``Δ > 0`` 的比例，``B`` 取
   ``eval.bootstrap_B``、RNG 用 ``bootstrap_seed(run_id, generation, "acceptance")``。
   **不得**读成 ``GainVerdict.win_rate_gap`` 的那个 ``gap``（它是 ``train − heldout``）：
   ``P(train > heldout)`` 恰恰是判据 1 **不**想要的（判据 1 要「heldout 不降」）。
   选连续指标而非 ``win_rate``：相隔一代后的赢/输**不是配对样本**，且 n=40 的
   ``win_rate`` 只能判 ≥15pp（D-3）。
3. **接受记录以各代 ``report.json`` 的 ``decision`` 为准**（``VERDICT_DECISIONS`` 同口径），
   指标值以 ``metrics/generations.csv`` 为准——台账是判据的机读来源（F46-A），
   而判决记录的唯一权威是报告（F43 的「重算不覆盖已裁决的 decision」）。

**fail-closed**：任一已存在的 heldout 代缺行 / ``n < min_heldout_n`` / 代序断裂 ⇒
``ok=false`` + ``reasons``，**不静默跳过**（与 F30/F47/D-M3-3 同一条纪律）。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

import numpy as np

from dice_ratchet.infra.errors import E, InfraError
from dice_ratchet.infra.io import read_json

from .metrics import bootstrap_seed
from .harness import (PARENT_ARM, VERDICT_DECISIONS, arm_of_row,
                      generation_report_path)

__all__ = ["ACCEPTANCE_VERSION", "CRITERION_TOL", "P_GAP_THRESHOLD",
           "GenerationAcceptance", "AcceptanceReport", "evaluate_acceptance",
           "write_acceptance"]

#: 判据 1 的口径版本（产物里记录它，便于日后对比「哪一版口径下的结论」）。
ACCEPTANCE_VERSION: Final[str] = "acceptance-v1"
#: `ci_lo(g) ≥ win_rate(g−1) − CRITERION_TOL`（MOD-08 §12 的 0.02）。
CRITERION_TOL: Final[float] = 0.02
#: `P(gap>0) ≥ P_GAP_THRESHOLD` 的替代通过路径（MOD-08 §12 的 0.90）。
P_GAP_THRESHOLD: Final[float] = 0.90
#: 判据 1 的**主判定量**（D-M3-1 裁决 A：`win_rate` 是第一判定量）。
PRIMARY_METRIC: Final[str] = "win_rate"
#: `P(gap>0)` 用的连续指标（D-3：n=40 下 `win_rate` 只能判 ≥15pp）。
CONTINUOUS_METRIC: Final[str] = "mean_stages_cleared"


@dataclass(frozen=True)
class GenerationAcceptance:
    """一代的判据 1 明细（**逐代给「为什么过/不过」**——只给一个布尔无法复核）。"""

    generation: int
    win_rate: float
    ci_lo: float
    n: int
    prev_generation: int | None = None
    prev_win_rate: float | None = None
    threshold: float | None = None          # prev_win_rate - CRITERION_TOL
    pass_ci: bool | None = None
    p_gap_gt_0: float | None = None         # 相邻两代连续指标的配对 bootstrap
    p_gap_metric: str | None = None
    p_gap_n_pairs: int | None = None
    pass_p_gap: bool | None = None
    decision: str | None = None             # 该代 report.json 的判决
    harness_version: int | None = None      # 该代台账行的 harness_version
    prev_harness_version: int | None = None
    #: D-M3-7（2026-10-14）：该代 / 上一代 report.json 里的**在役 play 面指纹**。
    #: ``not_applicable`` 的自 F57 起由「版本号相同」改判为「play_hash 相同」——
    #: 拒绝时版本号也推进（D2=A），版本号不再是起点的可靠代理。
    play_hash: str = ""
    prev_play_hash: str = ""
    #: F57：相邻两代的 ``harness_version`` **相同** ⇒ 起点没变（本代没有接受任何补丁），
    #: 「held-out 是否不降」在两个**完全相同的起点**之间**无处可判**——不是通过，也不是失败。
    #: 计入失败会让「补丁被拒」被记成「单调性破坏」（现场 gen-001 正是如此：`0.10` 的
    #: 来源就是 gen-000 的 v0，两代起点同为 v0）；计入通过则成了「退化通过」。
    not_applicable: bool = False
    #: **F70（2026-10-14，用户裁决「只做 ② 标注」）**：本对的**基线**是「选择后的」测量吗？
    #: 是——当上一代 ``decision="accept"`` 时，本代的起点基线就是**那个被接受候选自己的
    #: heldout 测量**。它是在 40 个 seed 上「通过了门禁」才被留下的，因此含**选择偏差**
    #: （winner's curse）。这不是缺陷而是「不重跑起点」的统计代价，**必须显式标注**，
    #: 否则读者会把这条基线当成无偏参考。
    baseline_selected: bool = False
    #: 该对基线的出处（``inherited:gen-00X`` / ``measured`` / ``""``）：F70 的取证字段。
    baseline_source: str = ""

    @property
    def passed(self) -> bool:
        return bool(self.pass_ci or self.pass_p_gap)

    def as_dict(self) -> dict[str, Any]:
        return {
            "generation": int(self.generation),
            "n": int(self.n),
            "win_rate": round(float(self.win_rate), 4),
            "ci_lo": round(float(self.ci_lo), 4),
            "harness_version": self.harness_version,
            "prev_generation": (None if self.prev_generation is None
                                else int(self.prev_generation)),
            "prev_win_rate": (None if self.prev_win_rate is None
                              else round(float(self.prev_win_rate), 4)),
            "prev_harness_version": self.prev_harness_version,
            "play_hash": self.play_hash,
            "prev_play_hash": self.prev_play_hash,
            "threshold": (None if self.threshold is None else round(float(self.threshold), 4)),
            "pass_ci": self.pass_ci,
            "p_gap_gt_0": (None if self.p_gap_gt_0 is None
                           else round(float(self.p_gap_gt_0), 4)),
            "p_gap_metric": self.p_gap_metric,
            "p_gap_n_pairs": self.p_gap_n_pairs,
            "pass_p_gap": self.pass_p_gap,
            "decision": self.decision,
            "not_applicable": bool(self.not_applicable),
            "baseline_selected": bool(self.baseline_selected),
            "baseline_source": self.baseline_source,
            "passed": self.passed,
        }


@dataclass(frozen=True)
class AcceptanceReport:
    """判据 1 的结论（供 `eval report` 的 ext 段与 `eval acceptance` 使用）。"""

    run_id: str
    generations: tuple[GenerationAcceptance, ...] = ()
    accepts: int = 0
    decisions: Mapping[str, int] = field(default_factory=dict)  # 各 enabled 判决的计数
    reasons: tuple[str, ...] = ()
    version: str = ACCEPTANCE_VERSION

    @property
    def comparable(self) -> tuple[GenerationAcceptance, ...]:
        """**可比较**的代：有上一代可比、且**不是同版本起点**的那些。

        ``not_applicable``（F57）不算一对：那一代的起点与上一代**逐字节相同**，
        「不降」在这两行之间是恒真命题，判它没有信息量（却又会被当成一次「通过」）。
        """
        return tuple(g for g in self.generations
                     if g.prev_generation is not None and not g.not_applicable)

    @property
    def not_applicable_pairs(self) -> tuple[GenerationAcceptance, ...]:
        """因起点未变而**跳过**判定的代（F57；产物里逐代列出，不许静默）。"""
        return tuple(g for g in self.generations if g.not_applicable)

    @property
    def selected_baseline_pairs(self) -> tuple[GenerationAcceptance, ...]:
        """基线是「选择后的」那些**可比**对（F70；见 ``GenerationAcceptance.baseline_selected``）。"""
        return tuple(g for g in self.comparable if g.baseline_selected)

    @property
    def baseline_selection_note(self) -> str:
        """F70 的诚实边界（只有真的存在选择后的基线时才非空）。

        **为什么必须写进产物**：这条基线是「下一代棘轮的门槛」，而它是被上一代的
        accept 决策**选出来**的测量（n=40 下 winner's curse 约 +5–10 pp 量级）。
        不标注的话，「held-out 单调不降」这句话的参考点就成了一个偏乐观的数。
        """
        selected = self.selected_baseline_pairs
        if not selected:
            return ""
        gens = "、".join(f"gen-{g.generation:03d}←gen-{g.prev_generation:03d}"
                        for g in selected)
        return (f"F70：{len(selected)} 个可比对的基线是**选择后的**测量（{gens}）——"
                "上一代被 accept ⇒ 本代起点基线就是那个候选自己的 heldout 测量，"
                "含 winner's curse（n=40 下约 +5–10 pp 量级）。本 run 的「单调不降」"
                "必须带上这条边界读；来源：用户裁决「只做 ② 标注」（2026-10-14）")

    @property
    def monotonic_ok(self) -> bool:
        """held-out 单调不降。

        **第一代不参与判定**（它没有可比对象）——把 `passed=False` 的默认值算进去会让
        「只有一代」永远判失败，那是语义错误而非严格。但**至少要有一对可比**：
        否则「单调不降」这句话无从谈起（只有 gen-000 基线的 run 不能算通过判据 1）。
        **起点未变的代**（``not_applicable``）同样不参与，也不占「可比」名额。
        """
        pairs = self.comparable
        if not pairs:
            return False
        if any(not g.passed for g in pairs):
            return False
        # 代序必须严格递增（否则跳代会被当成通过）。
        gens = [g.generation for g in self.generations]
        return gens == sorted(gens) and len(set(gens)) == len(gens)

    @property
    def ok(self) -> bool:
        """判据 1 整体：单调不降 **且** ≥1 次 accept（MOD-07 §12）。"""
        return bool(self.monotonic_ok and self.accepts > 0 and not self.reasons)

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "run_id": self.run_id,
            "criterion": "M3 判据 1：held-out 单调不降 + ≥1 次 accept",
            "primary_metric": PRIMARY_METRIC,
            "p_gap_metric": CONTINUOUS_METRIC,
            "criterion_tol": CRITERION_TOL,
            "p_gap_threshold": P_GAP_THRESHOLD,
            "monotonic_ok": self.monotonic_ok,
            "comparable_pairs": len(self.comparable),
            "not_applicable_pairs": len(self.not_applicable_pairs),
            #: F70（2026-10-14）：基线的**选择偏差**标注（用户裁决「只做 ② 标注」）。
            "baseline_selection": {
                "selected_pairs": len(self.selected_baseline_pairs),
                "note": self.baseline_selection_note,
            },
            "accepts": int(self.accepts),
            "decisions": dict(self.decisions),
            "ok": self.ok,
            "reasons": list(self.reasons),
            "per_generation": [g.as_dict() for g in self.generations],
        }


# =====================================================================
# 输入读取
# =====================================================================

def _heldout_rows(run_dir: Path) -> list[dict[str, Any]]:
    """读台账里**起点臂**的 ``split="heldout"`` 行，按代升序、**按代去重取末条**。

    两个过滤都是必需的：

    - ``split``：F48 之后台账是「每代每 split 一行」——**不过滤 split 会把 train 行
      当成第二个数据点**。
    - ``arm``（F57）：同一代**可能同时有 parent 与候选两套测量**。判据 1 比的是
      「这一代的**起点**在哪」，所以只读 ``arm="parent"``（缺列的旧行由
      ``arm_of_row`` 归为 parent——那时每代只有一行）。不过滤会按写入顺序任取一个，
      实测就是这么翻的面：`loop run` 写 parent `0.10`，`eval report` 写候选 `0.35`。

    去重取末条与 ``infra.plotting`` 同口径（历史台账有过同代重复行）。
    """
    path = Path(run_dir) / "metrics" / "generations.csv"
    if not path.is_file():
        raise InfraError(E.E_IO_STATE_MISSING,
                         f"判据 1 判定器需要台账：{path}",
                         hint="先跑 `eval report` 或 `loop run` 产出 metrics/generations.csv")
    with path.open(encoding="utf-8", newline="") as handle:
        rows = [dict(r) for r in csv.DictReader(handle)]
    by_gen: dict[int, dict[str, Any]] = {}
    for row in rows:
        if str(row.get("split") or "").strip() not in ("", "heldout"):
            continue
        if arm_of_row(row) != PARENT_ARM:
            continue
        raw = row.get("generation")
        if raw in (None, ""):
            continue
        by_gen[int(float(raw))] = row
    if not by_gen:
        raise InfraError(E.E_IO_STATE_MISSING,
                         f"台账里没有起点臂（arm={PARENT_ARM!r}）的 heldout 行：{path}",
                         hint="F48 之后台账含 train 行、F57 之后含候选行——判定器必须"
                              f"按 split=\"heldout\" 且 arm=\"{PARENT_ARM}\" 过滤")
    return [by_gen[g] for g in sorted(by_gen)]


def _int_or_none(value: Any) -> int | None:
    """台账列 → ``int``；缺失/空/非数字 → ``None``（**不得**默默当 0：0 是 v0 这个真实版本）。

    F57 的豁免依赖这个区分：``harness_version`` 读不到时**不做**豁免（fail-closed，
    宁可照旧判单调性，也不要因为列缺失而把两代当成「同一起点」）。
    """
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _decision_of(run_dir: Path, generation: int) -> str | None:
    """该代 ``report.json`` 的 ``decision``（缺失/损坏 → ``None``，不抛）。"""
    path = generation_report_path(run_dir, generation)
    if not path.is_file():
        return None
    try:
        return str(read_json(path).get("decision") or "") or None
    except InfraError: # pragma: no cover - 损坏报告按「无判决」处理
        return None


def _heldout_source_of(run_dir: Path, generation: int) -> str:
    """该代 ``report.json`` 里 ``heldout.source``（F70 的取证字段）。

    取值形如 ``measured``（本代真测）/ ``inherited:gen-00X``（跨代继承，D-M3-7）。
    缺失/损坏 → ``""``（**不猜**：标注里宁可空着）。
    """
    path = generation_report_path(run_dir, generation)
    if not path.is_file():
        return ""
    try:
        return str((read_json(path).get("heldout") or {}).get("source") or "")
    except InfraError: # pragma: no cover - 损坏报告按「无出处」处理
        return ""


def _play_hash_of(run_dir: Path, generation: int) -> str:
    """该代 ``report.json`` 里**在役 harness 的 play 面指纹**（D-M3-7，2026-10-14）。

    缺字段（旧 run）→ ``""``：调用方据此回落到「版本号相同」那条旧判据，而不是把
    两代当成同起点（少豁免 ⇒ 更严格，不会静默放宽判据 1）。
    """
    path = generation_report_path(run_dir, generation)
    if not path.is_file():
        return ""
    try:
        return str(read_json(path).get("harness_play_hash") or "")
    except InfraError: # pragma: no cover - 损坏报告按「无指纹」处理
        return ""


def _inherited_origin(run_dir: Path, generation: int, *, _depth: int = 0) -> int:
    """本代 ``heldout`` 局的**真实所在代**（跟随 ``inherited:gen-00X`` 链）。

    为什么需要（D-M3-7）：代际继承时本代**没有跑局**，那批局躺在来源代的
    ``episodes/heldout/`` 下。判据 1 的 ``P(gap>0)`` 需要逐局连续量，若直接在
    「本代」下找会得到空集 ⇒ ``p_gap=None`` ⇒ 只剩 ``pass_ci`` 一条路（白白弱化判据）。
    链深上限 8 与环检测都是 fail-closed：推不出来就用本代（结果是空集，不猜）。
    """
    if _depth >= 8:
        return int(generation)
    path = generation_report_path(run_dir, generation)
    if not path.is_file():
        return int(generation)
    try:
        source = str((read_json(path).get("heldout") or {}).get("source") or "")
    except InfraError: # pragma: no cover
        return int(generation)
    if not source.startswith("inherited:"):
        return int(generation)
    raw = source.split(":", 1)[1].strip()
    try:
        origin = int(raw.removeprefix("gen-").lstrip("0") or "0")
    except ValueError:
        return int(generation)
    if origin == int(generation):
        return int(generation)
    return _inherited_origin(run_dir, origin, _depth=_depth + 1)


def _episode_stages(run_dir: Path, generation: int, *, seeds: Sequence[int] | None = None
                    ) -> dict[int, int]:
    """该代 heldout 局的 ``{seed: stages_cleared}``（配对 bootstrap 的输入）。

    读不到任何一局时返回 ``{}``——调用方据此把 ``p_gap_gt_0`` 记为 ``None``
    （**不是** 0.0：0.0 会被读成「显著退步」）。

    ``generation`` 先经 ``_inherited_origin`` 归一：继承来的 heldout 段在本代**没有局**，
    那批局在来源代的目录下（D-M3-7）。
    """
    from .harness import load_episode_results

    # **不过滤臂**，但要能说清取的是谁：同代同 seed 可能同时存在 `parent` 与 `cand-<id>`
    # 两局（候选臂的 heldout 是另一套口径）。配对 bootstrap 只需要**同一批 seed 上的
    # 逐局连续量**，且在 M3 的判据 1 语境下要的是「这一代相对上一代」——两代都取
    # **同代内的全部非基线臂局**并由 `_paired_p_gap` 在**共有 seed** 上配对，
    # 这比绑定某个臂名更稳（臂名会漂移，F46-A 刚为此把「臂显式化」做进 CLI）。
    origin = _inherited_origin(run_dir, int(generation))
    results = load_episode_results(run_dir, "heldout", generation=int(origin),
                                   seeds=seeds, exclude_arms=_BASELINE_ARMS_FOR_ACCEPTANCE)
    if not results:
        return {}
    merged: dict[int, int] = {}
    for r in results:
        # 同 seed 多臂时取**最大** stages（对「这一代能到多远」的保守乐观估计）
        # ——此处只作为 P(gap>0) 的输入，不影响 win_rate/CI 那两条主判定路径。
        merged[int(r.seed)] = max(merged.get(int(r.seed), 0), int(r.stages_cleared))
    return merged


#: 基线臂不参与代际比较（它们是外部对照，不是「这一代」）。
_BASELINE_ARMS_FOR_ACCEPTANCE: Final[tuple[str, ...]] = (
    "random", "heuristic", "fixed_prompt", "baseline-random", "baseline-heuristic",
    "baseline-fixed_prompt",
)


def _paired_p_gap(a: Mapping[int, int], b: Mapping[int, int], *, seed: int,
                  B: int) -> tuple[float | None, int]:
    """相邻两代 ``mean_stages_cleared`` 的配对 bootstrap：``P(mean_b − mean_a > 0)``。

    只在**两代共有的 seed** 上配对（配对检验的前提）；共有 seed 为 0 时返回
    ``(None, 0)`` ——「无法计算」与「算出 0」必须区分开。
    """
    common = sorted(set(a) & set(b))
    if not common:
        return None, 0
    # 随机性口径：`numpy.random.default_rng(seed)`（INV-6 的指定写法；`import random`
    # 会被 `E_IO_GUARD_RANDOM` 拦下）。`seed` 由 `bootstrap_seed(run_id, generation, ...)`
    # 派生 ⇒ 同 run 同代重跑**逐位相同**（可从归档复算，NFR-08-01）。
    diffs = np.asarray([float(b[s] - a[s]) for s in common], dtype=float)
    rng = np.random.default_rng(int(seed))
    n = int(diffs.size)
    draws = rng.integers(0, n, size=(int(B), n))
    means = diffs[draws].sum(axis=1)
    wins = int(np.count_nonzero(means > 0.0))
    return wins / float(B), n


# =====================================================================
# 判定
# =====================================================================

def evaluate_acceptance(run_dir: Path | str, *, run_id: str = "",
                        min_heldout_n: int = 20, bootstrap_B: int = 10000
                        ) -> AcceptanceReport:
    """算判据 1（MOD-08 §12 判定表第 1 行）。

    - ``min_heldout_n``：来自 ``evolve.min_heldout_n``（默认 20）——不足即 fail-closed。
    - ``bootstrap_B``：来自 ``eval.bootstrap_B``（默认 10000）。
    """
    run_path = Path(run_dir)
    rows = _heldout_rows(run_path)

    reasons: list[str] = []
    decisions: dict[str, int] = {}
    per_gen: list[GenerationAcceptance] = []
    stages_cache: dict[int, dict[int, int]] = {}

    for index, row in enumerate(rows):
        generation = int(float(row["generation"]))
        n = int(float(row.get("n") or 0))
        win_rate = float(row.get("win_rate") or 0.0)
        ci_lo = float(row.get("ci_lo") or 0.0)
        hver = _int_or_none(row.get("harness_version"))
        decision = _decision_of(run_path, generation)
        ph = _play_hash_of(run_path, generation)
        if decision:
            decisions[decision] = decisions.get(decision, 0) + 1

        prev = rows[index - 1] if index > 0 else None
        entry = GenerationAcceptance(
            generation=generation, win_rate=win_rate, ci_lo=ci_lo, n=n,
            decision=decision, harness_version=hver, play_hash=ph)
        if n < int(min_heldout_n):
            reasons.append(
                f"gen-{generation:03d} 的 heldout n={n} < min_heldout_n={min_heldout_n}"
                "（fail-closed，不静默跳过）")
        if prev is None:
            # 第一代没有可比对象：单调性对它**无处可判**，记为通过但把
            # `prev_generation=None` 写进产物（读者能看出这一代没做比较）。
            per_gen.append(entry)
            continue

        prev_gen = int(float(prev["generation"]))
        prev_win = float(prev.get("win_rate") or 0.0)
        prev_hver = _int_or_none(prev.get("harness_version"))
        prev_ph = _play_hash_of(run_path, prev_gen)
        # F70（2026-10-14，用户裁决「只做 ② 标注」）：**基线的选择偏差**。
        # 上一代被 accept ⇒ 本代的起点基线就是**那个候选自己的 heldout 测量**；它是在这
        # 40 个 seed 上「通过了门禁」才留下的 ⇒ 含 winner's curse。不是缺陷，是「不重跑
        # 起点」（D-M3-7）的统计代价——但必须标注，否则读者会把这条基线当成无偏参考。
        prev_decision = _decision_of(run_path, prev_gen)
        selected = str(prev_decision or "") == "accept"
        baseline_source = _heldout_source_of(run_path, prev_gen)
        # D-M3-7（2026-10-14）：`not_applicable` 的判据从「版本号相同」换成
        # **「play 面指纹相同」**。原因：拒绝时版本号也会推进（D2=A），版本号相同
        # 不再是「起点逐字节相同」的充分条件；反过来版本号不同也不再意味着起点变了。
        #   · 两侧都有 play_hash ⇒ 以它为准；
        #   · 任一侧缺（旧 run 的 report.json）⇒ 回落「版本号相同」（保守：宁可判单调性，
        #     也不因为字段缺失而静默放宽）。
        if ph and prev_ph:
            same_origin = ph == prev_ph
        else:
            same_origin = (hver is not None and prev_hver is not None and hver == prev_hver)
        if same_origin:
            # F57 豁免：相邻两代的**起点逐字节相同**（本代没有接受任何补丁）。
            # 「held-out 不降」在这两行之间是恒真命题，判它既会把「合法拒绝」误记成
            # 「单调性破坏」（现场 gen-001：`ci_lo=0.0279 < 0.10−0.02`，而两代的 0.10 与
            # 0.15 只是同一 harness 的**两次重测**，5pp 是本 run 的噪声底），也会被
            # 当成一次「通过」而虚增可比对数。记 `not_applicable` 并**逐代列出**。
            per_gen.append(GenerationAcceptance(
                generation=generation, win_rate=win_rate, ci_lo=ci_lo, n=n,
                prev_generation=prev_gen, prev_win_rate=prev_win,
                harness_version=hver, prev_harness_version=prev_hver,
                play_hash=ph, prev_play_hash=prev_ph,
                decision=decision, not_applicable=True,
                baseline_selected=selected, baseline_source=baseline_source))
            continue

        threshold = prev_win - CRITERION_TOL
        pass_ci = ci_lo >= threshold
        stages_cache[generation] = stages_cache.get(generation) or _episode_stages(
            run_path, generation)
        stages_cache[prev_gen] = stages_cache.get(prev_gen) or _episode_stages(
            run_path, prev_gen)
        p_gap, n_pairs = _paired_p_gap(
            stages_cache[prev_gen], stages_cache[generation],
            seed=bootstrap_seed(run_id or run_path.name, generation, "acceptance"),
            B=int(bootstrap_B))
        entry = GenerationAcceptance(
            generation=generation, win_rate=win_rate, ci_lo=ci_lo, n=n,
            prev_generation=prev_gen, prev_win_rate=prev_win, threshold=threshold,
            pass_ci=pass_ci, p_gap_gt_0=p_gap,
            p_gap_metric=(CONTINUOUS_METRIC if p_gap is not None else None),
            p_gap_n_pairs=n_pairs,
            pass_p_gap=(None if p_gap is None else bool(p_gap >= P_GAP_THRESHOLD)),
            decision=decision, harness_version=hver, prev_harness_version=prev_hver,
            play_hash=ph, prev_play_hash=prev_ph,
            baseline_selected=selected, baseline_source=baseline_source)
        if not entry.passed:
            reasons.append(
                f"gen-{generation:03d} 单调性不成立：ci_lo={ci_lo:.4f} < "
                f"prev {prev_win:.4f} − {CRITERION_TOL} = {threshold:.4f}，"
                f"且 P({CONTINUOUS_METRIC} 上升)={p_gap if p_gap is None else round(p_gap, 4)}"
                f" < {P_GAP_THRESHOLD}")
        per_gen.append(entry)

    accepts = int(decisions.get("accept", 0))
    if accepts <= 0:
        reasons.append("接受率 = 0：每代都拒绝也能满足「单调不降」，属**退化通过**"
                       "（MOD-07 §12 要求 ≥1 次 accept）")

    return AcceptanceReport(run_id=str(run_id or run_path.name),
                            generations=tuple(per_gen), accepts=accepts,
                            decisions=decisions, reasons=tuple(reasons))


def write_acceptance(run_dir: Path | str, report: AcceptanceReport) -> Path:
    """把判据 1 的结论写 ``metrics/acceptance.json``（与各类 ``*.ext.json`` 同级）。"""
    from dice_ratchet.infra.io import write_json_atomic

    path = Path(run_dir) / "metrics" / "acceptance.json"
    write_json_atomic(path, report.as_dict())
    return path
