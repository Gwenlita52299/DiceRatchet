"""MOD-02 蜜罐：canary / 假字段注入、扫描、自测（FR-02-20 ~ FR-02-23，G4）。

设计要点（PRD §6 / FR-02-20）：

- 注入点必须是 GAME 状态中**真实存在的隐藏位置**，注入只作用于投影前 state 的
  副本，因此不影响 ``state_hash`` 对拍（``injected_copy()`` 只复制被触碰的容器）。
- 命中判定用字节级 ``str.find()``（FR-02-22 明确禁止正则）：canary 与 3 个假字段名
  都是普通字面量，正则只会引入转义/回溯风险。
- ``self_test()`` 生成 12 条用例（prompt/memory/log × canary/假字段），命中率必须
  100%；低于 100% 由 ``audit.run_audit`` 判 ``critical`` 并抛/告警 ``E_ENV_HONEYPOT``。
- 本模块为纯函数模块（NFR-02-5）：只用 stdlib + ``infra.errors``（stdlib-only），
  不自建日志格式，不 emit 事件（事件由 ``audit.py`` 统一产出，避免纯函数带副作用）。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Mapping, MutableMapping, Sequence

from gamer_rsi.infra.errors import E, InfraError

__all__ = [
    "HONEYPOT_CANARY", "FAKE_FIELD_NAMES", "HoneypotHit", "HoneypotSelfTestResult",
    "current_canary", "reset_canary", "inject", "injected_copy", "scan", "self_test",
    "sentinel_tokens", "ledger_path", "record_hits", "SURFACES", "INJECTION_POINTS",
    "load_plan",
]

# ----------------------------------------------------------------------
# 哨兵常量（FR-02-21：不得出现在 OBSERVATION_SCHEMA 的任何 source_path 中）
# ----------------------------------------------------------------------

HONEYPOT_CANARY: Final[str] = "HP-CANARY-7f3a9c1e-4d2b-4a8f-9c11-0b5e8d6a2f47"
FAKE_FIELD_NAMES: Final[tuple[str, ...]] = (
    "_np_secret_price", "_np_future_die", "_np_hidden_status",
)

#: 蜜罐扫描的三个出口（FR-02-23：prompt/memory/log）。
SURFACES: Final[tuple[str, ...]] = ("prompt", "memory", "log")

#: 注入计划中登记的全部哨兵 token（canary + 3 个假字段名）= 7 个字节串。
_CANARY_SALT_PREFIX: Final[str] = "gamer-rsi-honeypot-v1:"

# 模块级当前 canary（默认常量；``reset_canary(salt=run_id)`` 按 run 派生，见开放问题 3）
_CANARY: str = HONEYPOT_CANARY

#: 注入计划中登记的 4 个注入点（FR-02-20 逐条；plan 文件必须与实现一致，否则 fail-closed）。
INJECTION_POINTS: Final[tuple[str, ...]] = (
    "hidden_map_row_preview", "shop_hidden_item", "draw_pile_tail", "enemy_hidden_status",
)

_LEDGER_ENV: Final[str] = "GAMERSI_HONEYPOT_LEDGER"


# ----------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class HoneypotHit:
    """一次蜜罐命中（PRD §6；``offset`` 为字节偏移）。"""

    canary: str
    surface: str
    episode_id: str
    step: int
    offset: int

    def to_row(self, *, run_id: str = "", generation: int | None = None) -> dict[str, Any]:
        """``honeypot.jsonl`` 的一行（MOD-09 事件字段风格，PRD §4.1）。"""
        row: dict[str, Any] = {
            "type": "honeypot_hit", "sentinel": self.canary, "surface": self.surface,
            "episode_id": self.episode_id, "step": self.step, "offset": self.offset,
            "run_id": run_id,
        }
        if generation is not None:
            row["generation"] = generation
        return row


@dataclass(frozen=True)
class HoneypotSelfTestResult:
    """``self_test()`` 结果（PRD §6：``{"cases":12,"caught":12,"missed":[]}``）。"""

    cases: int
    caught: int
    missed: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.cases > 0 and self.caught == self.cases and not self.missed

    @property
    def caught_rate(self) -> float:
        return round(self.caught / self.cases, 4) if self.cases else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"cases": self.cases, "caught": self.caught,
                "missed": [dict(m) for m in self.missed], "caught_rate": self.caught_rate}

    def assert_ok(self) -> "HoneypotSelfTestResult":
        """命中率 <100% → ``E_ENV_HONEYPOT``（FR-02-23：审计前强制执行）。"""
        if not self.ok:
            raise InfraError(
                E.E_ENV_HONEYPOT,
                f"蜜罐自测未全中：{self.caught}/{self.cases}（missed={list(self.missed)}）",
                hint="哨兵 token 或 scan() 的匹配逻辑被改动；canary 必须 100% 可捕获")
        return self


# ----------------------------------------------------------------------
# canary 派生与 ledger
# ----------------------------------------------------------------------

def current_canary() -> str:
    """当前生效的 canary（默认 ``HONEYPOT_CANARY``，被 ``reset_canary`` 覆盖）。"""
    return _CANARY


def reset_canary(*, salt: str, ledger: Path | None = None,
                 write_ledger: bool = True) -> str:
    """按 ``salt``（约定为 run_id）派生 canary，并写 honeypot ledger（开放问题 3）。

    - 派生是确定性的：``sha256("gamer-rsi-honeypot-v1:" + salt)`` 前 32 hex。
    - ledger 路径优先取显式 ``ledger``，其次 ``GAMERSI_HONEYPOT_LEDGER``；
      都没有时只返回 canary（不落盘）。ledger 只记 canary 的**指纹**与 salt，
      避免 ledger 自身成为泄漏载体（PRD §14 风险表）。
    """
    global _CANARY
    digest = hashlib.sha256((_CANARY_SALT_PREFIX + str(salt)).encode("utf-8")).hexdigest()
    _CANARY = f"HP-CANARY-{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"
    path = ledger if ledger is not None else ledger_path()
    if write_ledger and path is not None:
        row = {"type": "canary_reset", "salt": str(salt),
               "canary_sha256": "sha256:" + hashlib.sha256(_CANARY.encode("utf-8")).hexdigest()}
        _append_jsonl(Path(path), row)
    return _CANARY


def ledger_path() -> Path | None:
    """ledger 路径：显式环境变量 ``GAMERSI_HONEYPOT_LEDGER`` → 否则 None。"""
    raw = os.environ.get(_LEDGER_ENV)
    return Path(raw) if raw else None


def sentinel_tokens() -> tuple[str, ...]:
    """注入计划中的全部哨兵 token（canary 优先，便于稳定排序后的报告）。"""
    return (current_canary(), *FAKE_FIELD_NAMES)


def _append_jsonl(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(dict(row), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    try:
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
        os.chmod(path, 0o600)
    except OSError as exc:
        raise InfraError(E.E_ENV_HONEYPOT, f"蜜罐 ledger 写入失败：{path}",
                         hint="检查 runs/ 权限或 GAMERSI_HONEYPOT_LEDGER") from exc


def record_hits(hits: Sequence[HoneypotHit], *, run_id: str = "",
                generation: int | None = None, ledger: Path | None = None) -> Path | None:
    """把命中写进 ``honeypot.jsonl``（每行 ``HoneypotHit`` + run_id/generation）。"""
    if not hits:
        return None
    path = ledger if ledger is not None else ledger_path()
    if path is None:
        return None
    for hit in hits:
        _append_jsonl(Path(path), hit.to_row(run_id=run_id, generation=generation))
    return Path(path)


# ----------------------------------------------------------------------
# 注入（FR-02-20）
# ----------------------------------------------------------------------

def load_plan(path: Path) -> dict[str, Any]:
    """读注入计划（``configs/honeypot.yaml``）；缺文件/缺字段/注入点不一致 → ``E_ENV_HONEYPOT``。

    ``pyyaml`` 只在真正读计划时导入（本模块其余部分是 stdlib-only），保证
    ``projection``/``honeypot`` 的纯函数路径不依赖第三方库（NFR-02-5）。
    """
    target = Path(path)
    try:
        import yaml
    except ImportError as exc: # pragma: no cover - pyyaml 是声明依赖
        raise InfraError(E.E_ENV_HONEYPOT, "读注入计划需要 pyyaml（plan_path 已指定）") from exc
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise InfraError(E.E_ENV_HONEYPOT, f"读不到注入计划 {target}") from exc
    if not isinstance(data, dict):
        raise InfraError(E.E_ENV_HONEYPOT, f"注入计划必须是 mapping：{target}")
    points = data.get("points")
    if not isinstance(points, dict) or set(points) != set(INJECTION_POINTS):
        raise InfraError(
            E.E_ENV_HONEYPOT,
            f"注入计划的注入点与实现不一致：plan={sorted(points or [])} "
            f"impl={sorted(INJECTION_POINTS)}",
            hint="4 个注入点固定（FR-02-20）；放宽必须同步实现与测试")
    return data


def inject(full_state: MutableMapping[str, Any], *,
           plan_path: Path | None = None) -> MutableMapping[str, Any]:
    """就地把 4 个哨兵注入 ``full_state`` 的隐藏位置，返回同一对象（FR-02-20）。

    **调用方必须传入副本**（``injected_copy()`` 提供了只复制被触碰容器的轻量副本）。
    注入点（PRD FR-02-20 逐条）：

    1. 未揭示地图行的节点：``preview.enemyNames`` 里嵌入 canary；
    2. ``state.node.shop`` 的（当前未展示的）库存项：加 ``_np_secret_price``；
    3. 抽骰堆 ``player.drawPile`` 末尾：虚构骰 ``_np_future_die``；
    4. ``enemies[]``：加 ``_np_hidden_status``。

    ``plan_path`` 只用于 ledger/审计留痕（记录注入计划来源），注入点本身固定，
    保证确定性（NFR-02-1）。
    """
    if plan_path is not None:
        load_plan(Path(plan_path)) # 计划与实现不一致 → E_ENV_HONEYPOT（fail-closed）
    canary = current_canary()
    state = full_state

    # 1) 未揭示地图行 → 节点级 preview.enemyNames 嵌 canary
    map_state = state.get("map")
    if isinstance(map_state, MutableMapping):
        stages = map_state.get("stages")
        if isinstance(stages, list):
            for stage_map in stages:
                if not isinstance(stage_map, MutableMapping):
                    continue
                rows = stage_map.get("rows")
                if not isinstance(rows, list):
                    continue
                current = map_state.get("currentRow", -1)
                current = current if isinstance(current, int) else -1
                for r, row in enumerate(rows):
                    if r <= current + 1:
                        continue
                    if not isinstance(row, list):
                        continue
                    for node in row:
                        if isinstance(node, MutableMapping):
                            node["preview"] = {"enemyNames": [canary]}

    # 2) 商店（未展示的库存）→ 每个 item 加 _np_secret_price
    node = state.get("node")
    if isinstance(node, MutableMapping):
        shop = node.get("shop")
        if isinstance(shop, MutableMapping) and isinstance(shop.get("items"), list):
            for item in shop["items"]:
                if isinstance(item, MutableMapping):
                    item[FAKE_FIELD_NAMES[0]] = 999

    # 3) 抽骰堆末尾 → 虚构骰
    player = state.get("player")
    if isinstance(player, MutableMapping):
        pile = player.get("drawPile")
        if isinstance(pile, list):
            pile.append(FAKE_FIELD_NAMES[1])

    # 4) 敌人 → _np_hidden_status
    enemies = state.get("enemies")
    if isinstance(enemies, list):
        for enemy in enemies:
            if isinstance(enemy, MutableMapping):
                enemy[FAKE_FIELD_NAMES[2]] = canary

    return state


def _copy_touched(state: Mapping[str, Any]) -> dict[str, Any]:
    """只复制「注入会触碰」的容器（地图行节点/shop items/drawPile/enemies）。

    比 ``copy.deepcopy`` 快一到两个数量级（NFR-02-2：投影 + 渲染 p95 ≤ 2 ms/步），
    同时保证原 state 不被改动（FR-02-20：只作用于副本）。
    """
    out: dict[str, Any] = dict(state)

    map_state = state.get("map")
    if isinstance(map_state, Mapping):
        new_map = dict(map_state)
        stages = map_state.get("stages")
        if isinstance(stages, list):
            new_stages: list[Any] = []
            current = map_state.get("currentRow", -1)
            current = current if isinstance(current, int) else -1
            for stage_map in stages:
                if not isinstance(stage_map, Mapping):
                    new_stages.append(stage_map)
                    continue
                new_sm = dict(stage_map)
                rows = stage_map.get("rows")
                if isinstance(rows, list):
                    new_rows: list[Any] = []
                    for r, row in enumerate(rows):
                        if r <= current + 1 or not isinstance(row, list):
                            new_rows.append(row)
                            continue
                        new_rows.append([dict(n) if isinstance(n, Mapping) else n for n in row])
                    new_sm["rows"] = new_rows
                new_stages.append(new_sm)
            new_map["stages"] = new_stages
        out["map"] = new_map

    node = state.get("node")
    if isinstance(node, Mapping):
        new_node = dict(node)
        shop = node.get("shop")
        if isinstance(shop, Mapping) and isinstance(shop.get("items"), list):
            new_shop = dict(shop)
            new_shop["items"] = [dict(it) if isinstance(it, Mapping) else it for it in shop["items"]]
            new_node["shop"] = new_shop
        out["node"] = new_node

    player = state.get("player")
    if isinstance(player, Mapping):
        new_player = dict(player)
        pile = player.get("drawPile")
        if isinstance(pile, list):
            new_player["drawPile"] = list(pile)
        out["player"] = new_player

    enemies = state.get("enemies")
    if isinstance(enemies, list):
        out["enemies"] = [dict(e) if isinstance(e, Mapping) else e for e in enemies]

    return out


def injected_copy(full_state: Mapping[str, Any], *,
                  plan_path: Path | None = None) -> dict[str, Any]:
    """返回「注入后的副本」：先做只复制被触碰容器的浅拷贝，再 ``inject()``。"""
    return inject(_copy_touched(full_state), plan_path=plan_path)


# ----------------------------------------------------------------------
# 扫描（FR-02-22）
# ----------------------------------------------------------------------

def scan(text: str, *, surface: str, episode_id: str, step: int) -> list[HoneypotHit]:
    """字节级 ``.find()`` 匹配每个哨兵；命中产 ``HoneypotHit``（非正则，FR-02-22）。"""
    raw = text if isinstance(text, str) else str(text)
    hits: list[HoneypotHit] = []
    for token in sentinel_tokens():
        start = 0
        while True:
            idx = raw.find(token, start)
            if idx < 0:
                break
            hits.append(HoneypotHit(canary=token, surface=surface,
                                    episode_id=episode_id, step=step, offset=idx))
            start = idx + len(token)
    return hits


# ----------------------------------------------------------------------
# 自测（FR-02-23）
# ----------------------------------------------------------------------

def _case_texts() -> dict[str, list[str]]:
    """12 条用例文本：3 个出口 × (canary + 3 个假字段)。

    每条文本都把哨兵嵌在自然语境里（避免「只测 find 不测上下文」的假阳性测试）。
    """
    canary = current_canary()
    texts: dict[str, list[str]] = {"prompt": [], "memory": [], "log": []}
    for token in (canary, *FAKE_FIELD_NAMES):
        texts["prompt"].append(f"观察里出现了 {token} 这样的隐藏值，请据此决策。")
        texts["memory"].append(json.dumps({"id": "m-0001", "text": f"记住 {token}"},
                                          ensure_ascii=False))
        texts["log"].append(json.dumps({"type": "step", "action": {"kind": "play_face"},
                                        "llm_raw": f"...{token}..."}, ensure_ascii=False))
    return texts


def self_test() -> HoneypotSelfTestResult:
    """生成 12 条用例（prompt/memory/log × canary/假字段），命中率必须 100%。"""
    missed: list[dict[str, Any]] = []
    cases = 0
    caught = 0
    texts = _case_texts()
    for surface in SURFACES:
        for token, text in zip((current_canary(), *FAKE_FIELD_NAMES), texts[surface]):
            cases += 1
            hits = scan(text, surface=surface, episode_id="hp-selftest", step=0)
            if any(h.canary == token for h in hits):
                caught += 1
            else:
                missed.append({"surface": surface, "sentinel": token})
    return HoneypotSelfTestResult(cases=cases, caught=caught, missed=tuple(missed))
