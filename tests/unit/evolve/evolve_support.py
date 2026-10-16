"""MOD-07 单测夹具的共享构造器（PRD §11 的「给定-当-则」用例直接复用）。

三组能力：

1. ``write_harness()`` —— 造一个最小可用的 ``harness/``（``prompts/`` + ``tools/`` +
   ``config.json`` + ``version.json`` + ``versions/v0/``），所有版本化/门禁用例的起点；
2. ``make_config()`` —— 造 ``EvolveConfig``（真实 dataclass，阈值可逐项覆盖），
   避免测试手写 20 个字段；
3. ``StubProposer`` / ``FakeArm`` —— LLM 与 MOD-08 的替身：``propose`` 与
   ``evaluate_candidate`` 都不触网、不调模型（NFR-07-6 依赖全部可注入）。

放在 ``tests/unit/evolve/`` 下的同目录模块，由 ``conftest.py`` 以 ``import
evolve_support`` 使用（与 ``loop_support.py`` / ``evals_support.py`` 同一约定）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from dice_ratchet.evolve.schemas import Candidate, CandidateReport, Verdict

RUN_ID = "1111111-abcdef12-20260930T120000Z"

DECISION_MD = """# 决策模板 v0

只输出一个 JSON 对象。

## 输出契约
{"reasoning": "不超过 2 句", "plan": []}
"""

REFLECTION_MD = """# 反思模板 v0

只输出一个 JSON 对象。
"""

TOOL_PY = '''"""harness 工具示例（@harness_tool 纯函数）。"""
import math


@harness_tool
def score_face(obs, candidates, memory):
    """按点数给动作打分。"""
    return [math.floor(len(candidates) / 2)]
'''

CONFIG_JSON = '{\n  "retrieval": {"top_k": 12},\n  "sampling": {"temperature": 0.0}\n}\n'

V0_VERSION = {
    "version": 0, "parent": None, "git_commit": "",
    "artifact_hash": "sha256:" + "0" * 64,
    "created_at": "2026-09-30T00:00:00Z",
    "reason": "v0 初始 harness", "accepted_by": "human", "base_model": None,
}


# =====================================================================
# 配置
# =====================================================================

def make_config(**overrides: Any) -> Any:
    """真实 ``EvolveConfig`` + 覆盖（``staging`` 相关字段由 EvolveContext 承载）。"""
    from dataclasses import replace

    from dice_ratchet.infra.config import EvolveConfig

    base = EvolveConfig()
    fields = set(getattr(base, "__dataclass_fields__", {}))
    known = {k: v for k, v in overrides.items() if k in fields}
    cfg = replace(base, **known) if known else base
    cfg.validate()
    return cfg


def make_app_config(**overrides: Any) -> Any:
    """``AppConfig``（MOD-08 ``evaluate_generation`` 需要 ``evals.*`` 段）。"""
    from dataclasses import replace

    from dice_ratchet.infra.config import AppConfig

    base = AppConfig()
    return replace(base, **{k: v for k, v in overrides.items()
                            if k in getattr(base, "__dataclass_fields__", {})})


# =====================================================================
# harness 骨架
# =====================================================================

def write_harness(root: Path | str, *, version: int = 0,
                  decision: str = DECISION_MD, reflection: str = REFLECTION_MD,
                  tool: str = TOOL_PY, config: str = CONFIG_JSON,
                  with_v0: bool = True, harness_name: str = "harness") -> Path:
    """在 ``root`` 下写出一个完整可用的 ``harness/``，返回其路径。"""
    base = Path(root) / harness_name
    (base / "prompts").mkdir(parents=True, exist_ok=True)
    (base / "tools").mkdir(parents=True, exist_ok=True)
    (base / "prompts" / "decision.md").write_text(decision, encoding="utf-8")
    (base / "prompts" / "reflection.md").write_text(reflection, encoding="utf-8")
    (base / "tools" / "score.py").write_text(tool, encoding="utf-8")
    (base / "config.json").write_text(config, encoding="utf-8")
    payload = dict(V0_VERSION)
    payload["version"] = int(version)
    base.joinpath("version.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    if with_v0:
        from dice_ratchet.evolve.versioning import snapshot_harness

        snapshot_harness(int(version), harness=base, reason="v0 初始 harness")
    return base


def context_for(root: Path, *, run_id: str = RUN_ID, harness: Path | None = None,
                runs_root: Path | str | None = None) -> Any:
    """``EvolveContext``（staging / 账本 / 报告三处路径同源）。"""
    from dice_ratchet.evolve.paths import EvolveContext

    return EvolveContext.of(None, run_id=run_id,
                            runs_root=(runs_root if runs_root is not None
                                       else Path(root) / "runs"),
                            harness=harness)


# =====================================================================
# 候选构造（PRD §11 的用例几乎都要一个 diff）
# =====================================================================

def prompt_diff(*, path: str = "harness/prompts/decision.md", added: str = "新增一行",
                context: Sequence[str] = (), start: int = 1) -> str:
    """造一个针对 prompt 文件的 unified diff（默认在文件头部插入一行）。"""
    body = [f"+{added}"]
    lines = [f"--- a/{path}", f"+++ b/{path}",
             f"@@ -{start},0 +{start},{len(body)} @@"]
    return "\n".join([*lines, *body]) + "\n"


def tool_diff(*, path: str = "harness/tools/score.py", added: Sequence[str] = (),
              removed: Sequence[str] = (), base: str = TOOL_PY) -> str:
    """造一个**可应用**的 diff（默认在文件末尾追加一行）。

    ``base`` 是目标文件的原始文本（用来算 hunk 头与上下文），因此测试里的 diff 不会
    因为 hunk 计数不符而在 ``apply_unified_diff`` 上失败——需要构造"坏 diff"的用例
    请直接手写字符串。
    """
    plus = list(added) or ["# 调整打分权重"]
    minus = list(removed)
    original = base.split("\n")
    if original and original[-1] == "":
        original.pop()
    lines = [f"--- a/{path}", f"+++ b/{path}"]
    if minus:
        start = next((i for i, line in enumerate(original) if line in minus), 0)
        lines.append(f"@@ -{start + 1},{len(minus)} +{start + 1},{len(plus)} @@")
        lines += [f"-{line}" for line in minus]
    else:
        start = len(original)
        lines.append(f"@@ -{start},0 +{start + 1},{len(plus)} @@")
        for line in original[max(0, start - 3):start]:
            lines.append(f" {line}")
    lines += [f"+{line}" for line in plus]
    return "\n".join(lines) + "\n"


def make_candidate(*, kind: str = "prompt_patch", diff: str | None = None,
                   rationale: str = "依据 traj:train#0 的失败归因",
                   effect: Sequence[Any] = ("win_rate", "up", 2),
                   touched: Sequence[str] | None = None,
                   gen_id: str = "g001") -> Candidate:
    """规范构造一个候选（``candidate_id`` 自动算出）。"""
    text = diff if diff is not None else prompt_diff()
    files = touched
    if files is None:
        from dice_ratchet.evolve.schemas import parse_diff_paths

        files = parse_diff_paths(text)
    return Candidate.create(kind=kind, diff=text, rationale=rationale,
                            expected_effect=list(effect), touched_files=list(files),
                            gen_id=gen_id)


# =====================================================================
# MOD-08 替身（ArmResult 的最小鸭子类型）
# =====================================================================

class FakeArm:
    """``evals.harness.ArmResult`` 的最小替身（``metrics`` + ``error_codes``）。"""

    def __init__(self, *, win_rate: float = 0.30, n: int = 40,
                 illegal_rate: float = 0.0, fallback_rate: float = 0.0,
                 truncation_rate: float = 0.0, mean_stages_cleared: float = 1.0,
                 error_codes: Sequence[str] = (), arm: str = "cand",
                 harness_version: int = 0, seeds: Sequence[int] = (),
                 entropy_slope: float | None = None,
                 collapse_flag: bool | None = None,
                 curve: Sequence[float | None] = (0.50, 0.52, 0.54),
                 results: Sequence[Any] = ()) -> None:
        self.arm = arm
        self.error_codes = tuple(error_codes)
        self.harness_version = int(harness_version)
        self.seeds = tuple(seeds)
        #: D-M3-7（2026-10-14）：``ArmResult`` 的逐局结果。``evaluate_generation`` 会把它
        #: 传给 ``judge_gain``（``candidate=``/``parent=``）；缺这个属性会让整段指标不可用
        #: ——原先的替身没有它，于是 §4.5 的指标段在这些用例里**一直**退化成骨架
        #: （``heldout.win_rate`` 恒 0.0），而用例只断言 decision/harness_version，
        #: 所以从没被发现。空元组 = 「没有逐局结果」。
        self.results = tuple(results)
        extra: dict[str, Any] = {"action_entropy_curve": list(curve)}
        if entropy_slope is not None:
            extra["entropy_slope"] = float(entropy_slope)
        if collapse_flag is not None:
            extra["collapse_flag"] = bool(collapse_flag)
        self.metrics = _Metrics(win_rate=win_rate, n=n, illegal_rate=illegal_rate,
                                fallback_rate=fallback_rate,
                                truncation_rate=truncation_rate,
                                mean_stages_cleared=mean_stages_cleared, **extra)

    def as_dict(self) -> dict[str, Any]:
        return {"arm": self.arm, "metrics": self.metrics.as_dict(),
                "error_codes": list(self.error_codes)}


class _Metrics:
    def __init__(self, **values: Any) -> None:
        self._values = {"basket": "H1", "n_dropped": 0, "low_confidence": False,
                        # F69：CI 必须**自洽**（原先写死 [0.0, 0.0] 而 win_rate=0.30 ⇒
                        # 绘图算出的 yerr 为负，matplotlib 抛 ValueError 炸掉整代）。
                        # 用一个最宽的区间：[0, 1] 永远包含点估计。
                        "win_rate_ci95": [0.0, 1.0], "mean_hearts_left": 1.0,
                        "mean_battles_won": 1.0, "mean_decisions": 10.0,
                        "degraded_n": 0, "entropy_mean_norm": 0.5,
                        "action_entropy_curve": [], "split": "heldout", **values}

    def as_dict(self) -> dict[str, Any]:
        return dict(self._values)

    def __getattr__(self, name: str) -> Any:
        try:
            return self._values[name]
        except KeyError as exc: # pragma: no cover
            raise AttributeError(name) from exc


def heldout_dict(*, win_rate: float = 0.35, n: int = 40, illegal_rate: float = 0.0,
                 fallback_rate: float = 0.0, truncation_rate: float = 0.0,
                 mean_stages_cleared: float = 1.2) -> dict[str, Any]:
    """``CandidateReport.heldout`` 的字典形态（判定表用例直接用）。"""
    return {"basket": "H1", "n": int(n), "win_rate": float(win_rate),
            "mean_stages_cleared": float(mean_stages_cleared),
            "mean_hearts_left": 1.0, "illegal_rate": float(illegal_rate),
            "fallback_rate": float(fallback_rate),
            "truncation_rate": float(truncation_rate),
            "win_rate_ci95": [0.0, 0.0], "entropy_slope": 0.0,
            "collapse_flag": False}


def paired_dict(*, delta: float = 0.05, significant: bool = True,
                direction: str = "up", metric: str = "win_rate",
                p: float = 0.01) -> dict[str, Any]:
    return {"method": "mcnemar", "metric": metric, "p": float(p), "delta": float(delta),
            "significant": bool(significant), "direction": direction, "n_pairs": 40}


def make_report(*, candidate: Candidate | None = None, smoke: Mapping[str, Any] | None = None,
                heldout: Mapping[str, Any] | None = None,
                paired: Mapping[str, Any] | None = None,
                train: Mapping[str, Any] | None = None,
                diversity: Mapping[str, Any] | None = None,
                verdict: Verdict | None = None, error_code: str | None = None,
                reason: str = "") -> CandidateReport:
    """构造 ``CandidateReport``（默认是一个"六项全通过"的干净样本）。"""
    cand = candidate or make_candidate()
    return CandidateReport(
        candidate=cand, smoke=dict(smoke or {"n": 2, "win_rate": 0.5}),
        heldout=dict(heldout or heldout_dict()),
        paired=dict(paired or paired_dict()),
        train=dict(train or {"win_rate": 0.35}),
        diversity=dict(diversity or {"entropy_slope": 0.0, "collapse_flag": False}),
        verdict=verdict, error_code=error_code, reason=reason)


class ScriptedArms:
    """按 ``arm`` 名返回既定 ``FakeArm`` 的 MOD-08 替身（可脚本化）。"""

    def __init__(self, arms: Mapping[str, Any] | None = None, *,
                 parent_heldout: Any = None, parent_smoke: Any = None,
                 paired: Any = None) -> None:
        self.arms = dict(arms or {})
        self.parent_heldout = parent_heldout or FakeArm(arm="parent", win_rate=0.30)
        self.parent_smoke = parent_smoke or FakeArm(arm="parent-smoke", win_rate=0.4, n=2)
        self.paired = paired or _Paired(delta=0.05, significant=True, direction="up")
        self.calls: list[dict[str, Any]] = []

    def parent_arms(self, parent_dir: Any, *, parent_version: int,
                    play_hash: str = "", **kwargs: Any) -> tuple[Any, Any]:
        self.calls.append({"arm": "parent", "dir": str(parent_dir),
                           "version": int(parent_version), "play_hash": str(play_hash)})
        return self.parent_heldout, self.parent_smoke

    def candidate_arms(self, staging_dir: Any, *, candidate: Candidate,
                       harness_version: int, play_hash: str = "",
                       **kwargs: Any) -> tuple[Any, Any, dict[str, Any]]:
        self.calls.append({"arm": f"cand-{candidate.candidate_id}",
                           "dir": str(staging_dir), "version": int(harness_version),
                           "play_hash": str(play_hash)})
        smoke = self.arms.get("smoke") or FakeArm(arm="smoke", win_rate=0.4, n=2)
        heldout = self.arms.get("heldout") or FakeArm(
            arm=f"cand-{candidate.candidate_id}", win_rate=0.35)
        return smoke, heldout, {}


class _Paired:
    def __init__(self, **values: Any) -> None:
        self._values = {"method": "mcnemar", "metric": "win_rate", "p": 0.01,
                        "delta": 0.05, "significant": True, "direction": "up",
                        "n_pairs": 40, **values}

    def as_dict(self) -> dict[str, Any]:
        return dict(self._values)

    def __getattr__(self, name: str) -> Any:
        try:
            return self._values[name]
        except KeyError as exc: # pragma: no cover
            raise AttributeError(name) from exc


# =====================================================================
# LLM 替身
# =====================================================================

class StubLLM:
    """MOD-03 ``generate_json`` 的替身：按脚本顺序返回 payload（可抛异常）。"""

    def __init__(self, payloads: Sequence[Any]) -> None:
        self.payloads = list(payloads)
        self.calls: list[dict[str, Any]] = []

    def generate_json(self, messages: Sequence[Any], schema: Mapping[str, Any], **kwargs: Any
                      ) -> Any:
        self.calls.append({"messages": list(messages), "schema": dict(schema),
                           "kwargs": dict(kwargs)})
        if not self.payloads:
            raise AssertionError("StubLLM 没有更多脚本 payload")
        payload = self.payloads.pop(0)
        if isinstance(payload, BaseException):
            raise payload
        return payload


def proposal_payload(*, kind: str = "prompt_patch", diff: str | None = None,
                     rationale: str = "依据 traj:train#0", effect: Sequence[Any] | None = None,
                     touched: Sequence[str] | None = None) -> dict[str, Any]:
    """一条 LLM 候选（``propose`` 的解析输入）。"""
    text = diff if diff is not None else prompt_diff()
    files = list(touched) if touched is not None else list(
        __import__("dice_ratchet.evolve.schemas", fromlist=["parse_diff_paths"])
        .parse_diff_paths(text))
    return {"kind": kind, "diff": text, "rationale": rationale,
            "expected_effect": list(effect or ("win_rate", "up", 2)),
            "touched_files": files}


def digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# =====================================================================
# 状态/上下文替身（run_one_generation 用）
# =====================================================================

class FakeState:
    """``loop.state.RunState`` 的最小替身（MOD-07 只读 ``generation`` + ``budget``）。"""

    def __init__(self, *, generation: int = 0, run_id: str = RUN_ID) -> None:
        self.generation = int(generation)
        self.run_id = run_id

        class _Budget:
            eval_episodes_used = 0

        self.budget = _Budget()


class FakeRunContext:
    """``loop.runner.RunContext`` 的最小替身。"""

    def __init__(self, root: Path, *, run_id: str = RUN_ID, app_config: Any = None,
                 harness: Path | None = None, memory: Any = None,
                 protocol: Any = None, episode_config: Any = None,
                 memory_version: int = 1) -> None:
        from dice_ratchet.infra.run import RunPaths

        self.run_id = run_id
        self.paths = RunPaths(Path(root))
        self.config = getattr(app_config, "loop", app_config)
        self.app_config = app_config
        self.episode_config = episode_config
        self.harness_dir = Path(harness) if harness is not None else Path("harness")
        self.memory_version = int(memory_version)
        self.memory = memory
        self.client = None
        self.protocol = protocol

    def harness_version(self) -> int:
        from dice_ratchet.evolve.versioning import current_version

        return current_version(self.harness_dir)


__all__ = [
    "CONFIG_JSON", "DECISION_MD", "REFLECTION_MD", "RUN_ID", "TOOL_PY", "V0_VERSION",
    "FakeArm", "FakeRunContext", "FakeState", "ScriptedArms", "StubLLM",
    "context_for", "digest", "heldout_dict", "make_app_config", "make_candidate",
    "make_config", "make_report", "paired_dict", "prompt_diff", "proposal_payload",
    "tool_diff", "write_harness",
]
