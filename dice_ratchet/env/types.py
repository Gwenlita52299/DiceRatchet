"""GameState / Observation / Action 的类型提示（GAME-01 §2 / GAME-00 §9.4）。

本模块**只提供类型**，不参与运行时逻辑。键名与 JS 逐字相同（camelCase），
``total=False`` 表示字段按需出现。G5：字段一致性由 ``tests/unit/env/test_types.py``
对照 GAME-01 §2 检查。
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

__all__ = ["RngStream", "DieInstance", "Slot", "Player", "Enemy", "MapNode", "MapState",
           "GameState", "GameConfig", "Stats", "Face", "Observation", "Action",
           "ReduceResult", "NativeResult", "LegalCheck", "ReplayResult"]


class RngStream(TypedDict, total=False):
    name: str
    counter: int
    s0: int
    s1: int


class Face(TypedDict, total=False):
    faceIndex: int
    label: str
    kind: str
    value: int
    school: str
    target: str | None
    statusKind: str | None
    selfCorruption: int
    tags: list[str]
    forged: bool


class DieInstance(TypedDict, total=False):
    defId: str
    faceIndex: int
    isCorrupt: bool
    faces: list[Face] | None
    forged: bool
    temporary: bool


class Slot(TypedDict, total=False):
    slotId: str
    slotKind: str
    constraint: str
    bonus: int
    reusable: bool
    filledBy: str | None
    disabledUntilTurn: int | None


class Player(TypedDict, total=False):
    hearts: int
    heartsMax: int
    virtue: int
    virtueMax: int
    ward: int
    starShards: int
    statuses: dict[str, int]
    charge: int
    purifyLeft: int
    abilities: list[dict[str, Any]]
    deck: list[str]
    drawPile: list[str]
    discardPile: list[str]
    hand: list[str]
    dice: dict[str, DieInstance]
    slots: list[Slot]
    relics: list[str]
    removeCount: int
    pendingVirtue: int
    pendingStatuses: dict[str, int]
    pendingDraw: int
    firstPlayDone: bool
    bladeAttackedThisTurn: bool
    arcanaLastTurn: int
    arcanaThisTurn: int
    drainNext: int
    reviveUsed: bool


class Enemy(TypedDict, total=False):
    enemyId: str
    defId: str
    name: str
    tier: str
    hp: int
    hpMax: int
    ward: int
    school: str
    phase: int
    statuses: dict[str, int]
    slots: list[Slot]
    intent: list[dict[str, Any]]
    passives: list[dict[str, Any]]
    dead: bool
    taunt: str


class MapNode(TypedDict, total=False):
    nodeId: str
    kind: str
    row: int
    index: int
    edges: list[str]
    enemyGroupId: str | None
    visited: bool


class MapState(TypedDict, total=False):
    stages: list[dict[str, Any]]
    currentRow: int
    currentNodeId: str | None
    path: list[str]


class Stats(TypedDict, total=False):
    turns: int
    damageDealt: int
    kills: int
    counterHits: int
    skillFaceUses: int
    battlesWon: int
    battlesLost: int
    stagesCleared: int


class GameConfig(TypedDict, total=False):
    configVersion: str
    difficulty: str
    seed: int
    character: str
    toggles: dict[str, bool]
    heartsMax: int
    virtueMax: int
    drawPerTurn: int
    handMax: int
    enemyHpMulPermille: int
    enemyDmgMulPermille: int
    enemyExtraDice: int
    counterMulPermille: dict[str, int]
    monoPenaltyPermille: int
    monoThresholdPermille: int
    monoMinDice: int
    shopPriceMulPermille: int
    goldMulPermille: int
    relicSlots: int
    purifyPerStage: int
    eliteStages: list[int]
    bossPhaseHpBonusPermille: int
    reviveOnce: bool
    stageCount: int
    maxTurnsPerBattle: int


class GameState(TypedDict, total=False):
    stateVersion: str
    runId: str
    configHash: str
    config: GameConfig
    stage: int
    chapter: int
    turn: int
    steps: int
    phase: str
    player: Player
    enemies: list[Enemy]
    map: MapState | None
    node: dict[str, Any] | None
    rng: list[RngStream]
    dieSeq: int
    shopSeq: int
    slotSeq: int
    seenEvents: list[str]
    pendingRelic: dict[str, Any] | None
    stats: Stats
    illegalCount: int
    consecutiveIllegal: int
    outcome: Literal["win", "lose"] | None
    outcomeReason: str | None
    truncationReason: str | None


class Action(TypedDict, total=False):
    kind: str
    dieId: str
    slotId: str
    targetId: str | None
    faceIndex: int
    nodeId: str
    itemId: str
    optionId: str
    defId: str
    candidateId: str
    relicId: str
    abilityId: str
    value: int


class LegalCheck(TypedDict, total=False):
    legal: bool
    code: str | None
    reason: str | None


class NativeResult(TypedDict, total=False):
    outcome: str | None
    truncationReason: str | None
    stagesCleared: int
    heartsLeft: int
    battlesWon: int
    turns: int
    kills: int


class ReduceResult(TypedDict, total=False):
    state: GameState
    events: list[dict[str, Any]]
    illegal: bool
    code: str
    reason: str
    error: dict[str, Any]


class ReplayStep(TypedDict, total=False):
    obsDigest: str
    events: list[dict[str, Any]]


class ReplayResult(TypedDict, total=False):
    steps: list[ReplayStep]
    digests: list[str]
    outcome: str | None
    state: GameState


class Observation(TypedDict, total=False):
    """``observe(state, "full")`` 的输出（GAME-00 §9.4 白名单）。"""
    schemaVersion: str
    runId: str
    phase: str
    stage: int
    chapter: int
    chapterName: str
    turn: int
    difficulty: str
    player: dict[str, Any]
    hand: list[dict[str, Any]]
    piles: dict[str, int]
    deck: list[dict[str, Any]]
    slots: list[dict[str, Any]]
    enemies: list[dict[str, Any]]
    battleKind: str | None
    battleHints: list[str]
    map: dict[str, Any] | None
    stages: list[dict[str, Any]]
    shop: dict[str, Any] | None
    event: dict[str, Any] | None
    treasure: dict[str, Any] | None
    forge: dict[str, Any] | None
    shrine: dict[str, Any] | None
    rest: dict[str, Any] | None
    pendingRelic: dict[str, Any] | None
    toggles: dict[str, bool]
    config: dict[str, Any]
    relics: list[dict[str, Any]]
    stats: Stats
    legalActionKinds: list[str]
    outcome: str | None
    outcomeReason: str | None
    truncationReason: str | None
    seed: None
