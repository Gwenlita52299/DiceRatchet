"""MOD-05 纯函数层：情境键、候选抽取、去重相似度、效用、BM25、渲染（PRD §6.2/§6.3/§6.4）。

**确定性（NFR-05-1 / G3）**：本模块不读墙钟、不 import ``random``（MOD-09 AST 守卫）；
所有排序都显式给出全序键，所有集合迭代先排序；同 seed 同输入两次调用逐字节相同。

**本模块不接收 seed 与 FullState**（PRD §6.2）：输入只有 ``Observation`` 投影与
``ReflectionEntry``，因此不存在把 heldout/test 信息带进记忆的通路。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Final, Mapping, Sequence

from .schemas import (GAME_PHASES, MEMORY_TAGS, MemoryCandidate, MemoryEntry,
                      ReflectionEntry, RetrievedMemory, SituationKey, round6)

__all__ = [
    "tokenize", "estimate_tokens", "char_ngrams", "stage_bucket", "obs_tag_weights",
    "situation_of",
    "situation_key", "key_for_situation", "extract_candidates", "similarity",
    "utility_score", "bm25_score", "doc_tokens", "score_entry", "render_entry",
    "render_entries", "select_with_budget", "OBS_TAG_WEIGHTS_SPEC",
    "CHARTS_PER_TOKEN",
]

CHARTS_PER_TOKEN: Final[int] = 3
MAX_RENDER_CHARS: Final[int] = 400
MAX_LESSON_CHARS: Final[int] = 240

_ASCII_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_]+")
_CJK_RE: Final[re.Pattern[str]] = re.compile(r"[\u4e00-\u9fff]")
_SENTENCE_RE: Final[re.Pattern[str]] = re.compile(r"[^。！？!?\n;；]+[。！？!?;；]?")

#: §6.3 的权重表（drivers → tag 词表的映射权重；仅参与 TagSim 的驱动值）。
OBS_TAG_WEIGHTS_SPEC: Final[Mapping[str, float]] = {
    "phase": 2.0, "intent_kind": 2.0, "counter_arrow": 2.0,
    "virtue_bucket": 1.0, "enemy_school": 1.0, "slot_kind": 1.0,
    "stage_bucket": 0.5, "node_kind": 0.5, "corrupt_count": 0.5,
}


# =====================================================================
# 分词与估算（FR-05-5 / NFR-05-4）
# =====================================================================

def tokenize(text: str) -> list[str]:
    """确定性分词：英文 ``[a-z0-9_]+`` 小写 + 中文单字 + 相邻中文二元组（B-30）。

    零依赖（不引入 jieba）；返回**保序** token 列表（BM25 的 ``f(t,d)`` 需要重复计数）。
    """
    if not isinstance(text, str) or not text:
        return []
    lowered = text.lower()
    out: list[str] = [m.group(0) for m in _ASCII_TOKEN_RE.finditer(lowered)]
    cjk = _CJK_RE.findall(text)
    out.extend(cjk)
    out.extend(cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1))
    return out


def estimate_tokens(text: str) -> int:
    """确定性 token 估算（``chars_div_3``，与 MOD-04 ``assembler.estimate_tokens`` 同口径）。"""
    if not isinstance(text, str) or not text:
        return 0
    return int(math.ceil(len(text) / CHARTS_PER_TOKEN))


def char_ngrams(text: str, n: int = 3) -> list[str]:
    """字符 n-gram（中文友好）；文本短于 n 时退化为整串。"""
    if not text:
        return []
    if len(text) <= n:
        return [text]
    return [text[i:i + n] for i in range(len(text) - n + 1)]


# =====================================================================
# 情境键（FR-05-5 / §6.2）
# =====================================================================

def key_for_situation(situation: SituationKey) -> str:
    return situation.key()


def _dig(obs: Mapping[str, Any], *path: str, default: Any = None) -> Any:
    node: Any = obs
    for key in path:
        if not isinstance(node, Mapping) or key not in node:
            return default
        node = node[key]
    return node


def _as_seq(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return [v for v in value]
    return []


def _phase_of(obs: Mapping[str, Any]) -> str:
    phase = str(obs.get("phase") or "player_turn")
    if phase in ("enemy_turn", "settle"):
        return "player_turn"  # 投影里存在、SituationKey 词表内没有 → 归入战斗回合
    return phase if phase in GAME_PHASES else "player_turn"


def stage_bucket(stage: Any) -> str:
    try:
        value = int(stage or 1)
    except (TypeError, ValueError):
        value = 1
    if value <= 2:
        return "1-2"
    if value <= 4:
        return "3-4"
    if value <= 6:
        return "5-6"
    return "7+"


def _virtue_bucket(obs: Mapping[str, Any]) -> str:
    virtue = _dig(obs, "player", "virtue")
    vmax = _dig(obs, "player", "virtueMax")
    try:
        cur = float(virtue)
        top = float(vmax) if vmax else 0.0
    except (TypeError, ValueError):
        return "mid"
    if top <= 0:
        return "mid"
    ratio = cur / top
    if ratio <= 0.25:
        return "critical"
    if ratio <= 0.50:
        return "low"
    if ratio <= 0.75:
        return "mid"
    return "high"


def _arrow_of(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text if text in ("up", "down", "eq") else None


def _node_kind(obs: Mapping[str, Any]) -> str | None:
    """当前地图节点的 kind（战斗内取当前节点）；obs 无该字段时返回 ``None``。"""
    map_state = obs.get("map")
    if not isinstance(map_state, Mapping):
        return None
    current = map_state.get("currentNodeId")
    for row in _as_seq(map_state.get("rows")):
        for node in _as_seq(row):
            if isinstance(node, Mapping) and node.get("nodeId") == current:
                kind = node.get("kind")
                return str(kind) if kind else None
    return None


def situation_of(obs: Mapping[str, Any] | None) -> SituationKey:
    """从 ``obs-agent-1.0.0`` 投影提取受控情境键（缺字段取保守缺省，不抛异常）。"""
    source: Mapping[str, Any] = obs if isinstance(obs, Mapping) else {}
    hand = _as_seq(source.get("hand"))
    slots = _as_seq(source.get("slots"))
    enemies = _as_seq(source.get("enemies"))

    enemy0: Mapping[str, Any] = enemies[0] if enemies and isinstance(enemies[0], Mapping) else {}
    intents = _as_seq(enemy0.get("intent"))
    intent0: Mapping[str, Any] = intents[0] if intents and isinstance(intents[0], Mapping) else {}

    corrupt = sum(1 for d in hand
                  if isinstance(d, Mapping) and bool(d.get("isCorrupt")))
    slot_kinds = tuple(sorted({str(s.get("slotKind")) for s in slots
                               if isinstance(s, Mapping) and s.get("slotKind")}))

    return SituationKey(
        phase=_phase_of(source),
        stage_bucket=stage_bucket(source.get("stage")),
        node_kind=_node_kind(source),
        virtue_bucket=_virtue_bucket(source),
        hearts_left=_int_or_zero(_dig(source, "player", "hearts")),
        enemy_school=_opt_text(enemy0.get("school")),
        intent_kind=_opt_text(intent0.get("kind")),
        counter_arrow=_arrow_of(intent0.get("arrow")),
        hand_count=min(len(hand), 8),
        corrupt_count=corrupt,
        slot_kinds=slot_kinds,
    )


_SLOT_TAG: Final[Mapping[str, tuple[str, float]]] = {
    "attack": ("combat", 1.0),
    "defense": ("defense", 1.0),
    "heal": ("defense", 1.0),
    "utility": ("ability", 1.0),
    "curse": ("corruption", 1.0),
    "purify": ("low_virtue", 1.0),
}

_PHASE_TAG: Final[Mapping[str, str]] = {
    "map": "map_route", "shop": "shop", "event": "event", "forge": "forge",
    "rest": "end_turn", "shrine": "relic", "treasure": "relic",
    "relic_swap": "relic", "player_turn": "combat",
}

_STATUS_INTENTS: Final[frozenset[str]] = frozenset({
    "poison", "burn", "weak", "vulnerable", "curse", "status", "debuff", "buff",
})


def obs_tag_weights(obs: Mapping[str, Any] | None,
                    tags_hint: Sequence[str] = ()) -> dict[str, float]:
    """观测 → ``{受控 tag: 权重}``（§6.3 的驱动权重映射到 §6.6 的 tag 词表）。

    同一 tag 由多个驱动命中时取**最大**权重（可复现；不累加，避免噪声放大）。
    文档化的映射偏差：``stage_bucket`` 没有对应 tag，只参与 ``situation_key`` 加成，
    不进入 TagSim（PRD §6.3 的驱动表里它是 0.5 的驱动值，但 tag 词表无对应项）。
    """
    source: Mapping[str, Any] = obs if isinstance(obs, Mapping) else {}
    situation = situation_of(source)
    weights: dict[str, float] = {}

    def add(tag: str, weight: float) -> None:
        if tag in MEMORY_TAGS:
            weights[tag] = max(weights.get(tag, 0.0), float(weight))

    add(_PHASE_TAG.get(situation.phase, "combat"), OBS_TAG_WEIGHTS_SPEC["phase"])
    if situation.virtue_bucket in ("critical", "low"):
        add("low_virtue", OBS_TAG_WEIGHTS_SPEC["virtue_bucket"])
    if situation.enemy_school:
        add("school_counter", OBS_TAG_WEIGHTS_SPEC["enemy_school"])
    if situation.counter_arrow:
        add("school_counter", OBS_TAG_WEIGHTS_SPEC["counter_arrow"])
    if situation.intent_kind:
        intent = str(situation.intent_kind).lower()
        add("status_effect" if intent in _STATUS_INTENTS else "combat",
            OBS_TAG_WEIGHTS_SPEC["intent_kind"])
    if situation.corrupt_count > 0:
        add("corruption", OBS_TAG_WEIGHTS_SPEC["corrupt_count"])
    if situation.node_kind:
        kind = situation.node_kind.lower()
        if kind in ("elite", "boss"):
            add(kind, OBS_TAG_WEIGHTS_SPEC["node_kind"])
    for slot in situation.slot_kinds:
        mapped = _SLOT_TAG.get(slot)
        if mapped:
            add(mapped[0], OBS_TAG_WEIGHTS_SPEC["slot_kind"])
    if len(_as_seq(source.get("enemies"))) >= 2:
        add("target_choice", OBS_TAG_WEIGHTS_SPEC["virtue_bucket"])
    if situation.phase == "player_turn":
        add("combat", OBS_TAG_WEIGHTS_SPEC["phase"])
    for tag in tags_hint:
        add(str(tag), 1.0)
    return weights


def situation_key(obs: Mapping[str, Any] | None,
                  tags_hint: Sequence[str] = ()) -> tuple[str, SituationKey, tuple[str, ...]]:
    """``(situation_key, SituationKey, tags(obs))``（PRD §6.2 的签名）。

    ``tags`` 按 ``(-权重, tag)`` 排序（确定性），供 TagSim 与 BM25 query 复用。
    """
    situation = situation_of(obs)
    weights = obs_tag_weights(obs, tags_hint)
    ordered = tuple(tag for tag, _ in sorted(weights.items(), key=lambda kv: (-kv[1], kv[0])))
    return situation.key(), situation, ordered


# =====================================================================
# 候选抽取（FR-05-2：确定性规则，无候选时只存反思不报错）
# =====================================================================

_THEME_KEYWORDS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("defense", ("盾", "防御", "格挡", "护盾", "ward", "defense", "defend")),
    ("low_virtue", ("净化", "德性", "美德", "virtue", "purify")),
    ("corruption", ("魔化", "污染", "corrupt", "corruption")),
    ("school_counter", ("克制", "学派", "箭头", "counter", "school", "arrow")),
    ("target_choice", ("目标", "嘲讽", "taunt", "target", "选敌", "打错")),
    ("combat", ("攻击", "输出", "伤害", "打", "战斗", "骰", "回合", "槽", "attack",
                "damage", "battle", "die", "dice", "slot", "turn")),
    ("map_route", ("路线", "地图", "分支", "route", "map", "path")),
    ("shop", ("商店", "购买", "刷新", "shop", "buy")),
    ("event", ("事件", "选项", "event", "option")),
    ("forge", ("锻造", "重铸", "forge")),
    ("relic", ("遗物", "神龛", "宝箱", "换遗物", "relic", "shrine", "treasure")),
    ("elite", ("精英", "elite")),
    ("boss", ("首领", "魔王", "boss")),
    ("status_effect", ("中毒", "灼烧", "虚弱", "易伤", "poison", "burn", "weak", "status")),
    ("end_turn", ("结束回合", "结束这回合", "end_turn", "end turn")),
    ("ability", ("技能", "能力", "ability")),
)

_AVOID_MARKERS: Final[tuple[str, ...]] = (
    "不该", "不要", "不能", "别", "避免", "错误", "教训", "失败", "却", "反而",
    "我还在", "切忌", "不应该", "亏", "翻车",
)
_DO_MARKERS: Final[tuple[str, ...]] = (
    "应该", "应当", "优先", "最好", "要先", "建议", "记得", "下次要", "需要先",
    "先", "正确",
)

_SCHOOLS: Final[Mapping[str, str]] = {
    "blade": "blade", "刃": "blade", "刀锋": "blade", "剑": "blade",
    "arcana": "arcana", "秘法": "arcana", "奥术": "arcana",
    "verdant": "verdant", "翠绿": "verdant", "自然": "verdant",
    "neutral": "neutral", "中立": "neutral",
}

_HINT_BY_TAG: Final[Mapping[str, dict[str, Any]]] = {
    "defense": {"kind": "play_face", "slotKind": "defense", "school": "neutral"},
    "low_virtue": {"kind": "purify", "slotKind": None, "school": None},
    "corruption": {"kind": "purify", "slotKind": None, "school": None},
    "school_counter": {"kind": "play_face", "slotKind": "attack", "school": None},
    "target_choice": {"kind": "play_face", "slotKind": "attack", "school": None},
    "combat": {"kind": "play_face", "slotKind": "attack", "school": "neutral"},
    "map_route": {"kind": "map_choose", "slotKind": None, "school": None},
    "shop": {"kind": "shop_buy", "slotKind": None, "school": None},
    "event": {"kind": "event_choose", "slotKind": None, "school": None},
    "forge": {"kind": "forge_pick", "slotKind": None, "school": None},
    "relic": {"kind": "relic_swap", "slotKind": None, "school": None},
    "elite": {"kind": "play_face", "slotKind": "attack", "school": None},
    "boss": {"kind": "play_face", "slotKind": "attack", "school": None},
    "status_effect": {"kind": "purify", "slotKind": None, "school": None},
    "end_turn": {"kind": "end_turn", "slotKind": None, "school": None},
    "ability": {"kind": "use_ability", "slotKind": None, "school": None},
}

_TAG_PRIORITY: Final[tuple[str, ...]] = (
    "defense", "low_virtue", "corruption", "target_choice", "school_counter",
    "combat", "boss", "elite", "status_effect", "map_route", "shop", "event",
    "forge", "relic", "ability", "end_turn",
)

_PHASE_BY_TAG: Final[Mapping[str, str]] = {
    "map_route": "map", "shop": "shop", "event": "event", "forge": "forge",
    "relic": "treasure", "end_turn": "rest",
}

_VIRTUE_LOW_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:净化|德性|美德|virtue)[^0-9]{0,8}(\d{1,3})")
_HAND_RE: Final[re.Pattern[str]] = re.compile(r"(\d{1,2})\s*(?:个)?\s*(?:手骰|骰子|dice)")


def extract_candidates(reflection: ReflectionEntry,
                       cfg: Any | None = None) -> list[MemoryCandidate]:
    """确定性规则抽取（FR-05-2）：≤ ``max_candidates_per_reflection`` 条；无命中返回 ``[]``。

    规则：按句切分 → 每句匹配受控 tag 关键词 → 命中的句子生成一条候选（同
    ``(lesson, tags, polarity)`` 去重）→ 截断到配置上限。同一输入连续调用结果全等
    （无随机、无墙钟、无字典序依赖）。
    """
    limit = _extract_limit(cfg)
    if limit <= 0:
        return []
    text = str(reflection.text or "")
    sentences = [s.strip() for s in _SENTENCE_RE.findall(text) if s and s.strip()]
    if not sentences:
        return []

    candidates: list[MemoryCandidate] = []
    seen: set[tuple[str, tuple[str, ...], str]] = set()
    for sentence in sentences:
        tags = _tags_for(sentence)
        if not tags:
            continue
        lesson = _clip(sentence, MAX_LESSON_CHARS)
        polarity = _polarity_for(sentence, reflection)
        key = (lesson, tags, polarity)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(_candidate_for(reflection, sentence, lesson, tags, polarity))
        if len(candidates) >= limit:
            break
    return candidates


def _extract_limit(cfg: Any | None) -> int:
    if cfg is None:
        return 3
    extract = getattr(cfg, "extract", None)
    value = getattr(extract, "max_candidates_per_reflection", 3) if extract else 3
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 3


def _tags_for(text: str) -> tuple[str, ...]:
    lowered = text.lower()
    hits = [tag for tag, keywords in _THEME_KEYWORDS
            if any(k in lowered for k in keywords)]
    if not hits:
        return ()
    ordered = [t for t in _TAG_PRIORITY if t in hits]
    return tuple(ordered[:8])


def _polarity_for(text: str, reflection: ReflectionEntry) -> str:
    lowered = text.lower()
    if any(m in lowered for m in _AVOID_MARKERS):
        return "avoid"
    if any(m in lowered for m in _DO_MARKERS):
        return "do"
    return "do" if str(reflection.outcome) == "win" else "avoid"


def _candidate_for(reflection: ReflectionEntry, sentence: str, lesson: str,
                   tags: tuple[str, ...], polarity: str) -> MemoryCandidate:
    situation = _situation_for(reflection, sentence, tags)
    hint = _hint_for(tags)
    confidence = round6(min(1.0, 0.5 + 0.1 * len(tags) + (0.1 if hint else 0.0)))
    return MemoryCandidate(
        situation=situation,
        tags=tags,
        lesson=lesson,
        polarity=polarity,
        confidence=confidence,
        action_hint=hint,
        source_episode_id=str(reflection.episode_id),
        source_reflection_id=str(reflection.reflection_id),
        created_at=str(reflection.created_at),
        created_tick=int(reflection.tick),
        run_id=str(reflection.run_id),
        generation=int(reflection.generation),
        harness_version=int(reflection.harness_version),
    )


def _hint_for(tags: tuple[str, ...]) -> dict[str, Any] | None:
    for tag in _TAG_PRIORITY:
        if tag in tags:
            hint = _HINT_BY_TAG.get(tag)
            if hint is not None:
                return dict(hint)
    return None


def _situation_for(reflection: ReflectionEntry, sentence: str,
                   tags: tuple[str, ...]) -> SituationKey:
    phase = "player_turn"
    for tag in _TAG_PRIORITY:
        if tag in tags and tag in _PHASE_BY_TAG:
            phase = _PHASE_BY_TAG[tag]
            break
    if "combat" in tags:
        phase = "player_turn"

    node_kind = "boss" if "boss" in tags else ("elite" if "elite" in tags else None)

    virtue_bucket = "mid"
    if "low_virtue" in tags:
        virtue_bucket = "low"
        match = _VIRTUE_LOW_RE.search(sentence)
        if match and int(match.group(1)) <= 25:
            virtue_bucket = "critical"

    enemy_school: str | None = None
    lowered = sentence.lower()
    for token, school in _SCHOOLS.items():
        if token in lowered:
            enemy_school = school
            break

    intent_kind: str | None = None
    if "purge" in lowered:
        intent_kind = "purge"
    elif "attack" in lowered or "攻击" in sentence:
        intent_kind = "attack"
    elif "status_effect" in tags:
        intent_kind = "status"

    counter_arrow: str | None = None
    if "克制" in sentence:
        counter_arrow = "down"
    elif "被克制" in sentence:
        counter_arrow = "up"

    hand_count = 0
    match = _HAND_RE.search(sentence)
    if match:
        hand_count = min(int(match.group(1)), 8)

    corrupt_count = min(sentence.count("魔化") + lowered.count("corrupt"), 8)

    slot_kinds: list[str] = []
    if "攻击槽" in sentence or "attack slot" in lowered:
        slot_kinds.append("attack")
    if "防御槽" in sentence or "defense slot" in lowered:
        slot_kinds.append("defense")
    if "净化槽" in sentence or "purify slot" in lowered:
        slot_kinds.append("purify")

    return SituationKey(
        phase=phase,
        stage_bucket=stage_bucket(int(reflection.stages_cleared) + 1),
        node_kind=node_kind,
        virtue_bucket=virtue_bucket,
        hearts_left=int(reflection.hearts_left),
        enemy_school=enemy_school,
        intent_kind=intent_kind,
        counter_arrow=counter_arrow,
        hand_count=hand_count,
        corrupt_count=corrupt_count,
        slot_kinds=tuple(sorted(set(slot_kinds))),
    )


# =====================================================================
# 去重相似度（FR-05-3 / §8.1）
# =====================================================================

def similarity(a: MemoryCandidate | MemoryEntry, b: MemoryCandidate | MemoryEntry,
               *, jaccard_weight: float = 0.5, char3_weight: float = 0.5) -> float:
    """``0.5*Jaccard(token) + 0.5*cos(char3 tf-idf)``（权重可配，见 §9）。

    纯函数：char3 的 idf 用**两文档**平滑（``ln(1+(N+1)/(df+1))``，N=2），
    不依赖库状态，因此同输入必然同输出。
    """
    text_a = _lesson_of(a)
    text_b = _lesson_of(b)
    jaccard = _jaccard(tokenize(text_a), tokenize(text_b))
    cosine = _char3_tfidf_cosine(text_a, text_b)
    return round6(jaccard_weight * jaccard + char3_weight * cosine)


def _lesson_of(item: MemoryCandidate | MemoryEntry) -> str:
    lesson = getattr(item, "lesson", "")
    return " ".join(str(lesson or "").split())


def _jaccard(tokens_a: Sequence[str], tokens_b: Sequence[str]) -> float:
    set_a = set(tokens_a)
    set_b = set(tokens_b)
    if not set_a and not set_b:
        return 1.0
    union = set_a | set_b
    if not union:
        return 1.0
    return len(set_a & set_b) / len(union)


def _char3_tfidf_cosine(text_a: str, text_b: str) -> float:
    grams_a = char_ngrams(text_a, 3)
    grams_b = char_ngrams(text_b, 3)
    if not grams_a and not grams_b:
        return 1.0
    count_a = Counter(grams_a)
    count_b = Counter(grams_b)
    vocab = sorted(set(count_a) | set(count_b))
    vectors: list[tuple[float, float]] = []
    n_docs = 2.0
    for gram in vocab:
        df = (1 if gram in count_a else 0) + (1 if gram in count_b else 0)
        idf = math.log(1.0 + (n_docs + 1.0) / (df + 1.0))
        tf_a = count_a.get(gram, 0) / max(1, len(grams_a))
        tf_b = count_b.get(gram, 0) / max(1, len(grams_b))
        vectors.append((tf_a * idf, tf_b * idf))
    dot = sum(x * y for x, y in vectors)
    norm_a = math.sqrt(sum(x * x for x, _ in vectors))
    norm_b = math.sqrt(sum(y * y for _, y in vectors))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


# =====================================================================
# 效用分（FR-05-8 / §6.4：证据只用环境原生胜负，INV-7）
# =====================================================================

def utility_score(entry: MemoryEntry, *, tick: int) -> float:
    """Laplace 平滑胜率差 + 引用量 + 时新度；``clip(-1, 1)``。"""
    p_used = (entry.n_win_adopted + 1) / (entry.n_adopted + 2)
    p_base = (entry.n_win_not_adopted + 1) / (entry.n_not_adopted + 2)
    age = max(0, int(tick) - int(entry.created_tick))
    ttl = max(1, int(entry.ttl_ticks))
    raw = (0.5 * (p_used - p_base)
           + 0.3 * math.log1p(int(entry.n_ref)) / math.log1p(200)
           + 0.2 * math.exp(-age / ttl))
    return round6(max(-1.0, min(1.0, raw)))


# =====================================================================
# BM25 与打分（FR-05-5 / §6.3）
# =====================================================================

def doc_tokens(entry: MemoryEntry) -> list[str]:
    """文档 token：lesson 分词 + tags 原样（tag 是高信息量 token）。"""
    return tokenize(entry.lesson) + [str(t) for t in entry.tags]


def bm25_score(query_tokens: Sequence[str], doc_tokens_: Sequence[str],
               df: Mapping[str, int], n_docs: int, avgdl: float,
               k1: float = 1.2, b: float = 0.75) -> float:
    """标准 BM25（k1/b 可配）；空查询/空文档/空库返回 0.0。"""
    if not query_tokens or not doc_tokens_ or n_docs <= 0:
        return 0.0
    length = len(doc_tokens_)
    if length == 0 or avgdl <= 0:
        return 0.0
    freqs = Counter(doc_tokens_)
    total = 0.0
    for token in sorted(set(query_tokens)):
        freq = freqs.get(token, 0)
        if freq == 0:
            continue
        df_t = int(df.get(token, 0))
        idf = math.log(1.0 + (n_docs - df_t + 0.5) / (df_t + 0.5))
        denom = freq + k1 * (1.0 - b + b * length / avgdl)
        total += idf * freq * (k1 + 1.0) / denom
    return total


def _bm25_norm(value: float) -> float:
    if value <= 0.0:
        return 0.0
    return value / (value + 1.0)


def score_entry(entry: MemoryEntry, *, obs_key: str, obs_tags: Mapping[str, float],
                query_tokens: Sequence[str], df: Mapping[str, int], n_docs: int,
                avgdl: float, weights: Mapping[str, float], same_situation_bonus: float,
                recency_lambda: float, tick: int,
                query_embedding: Sequence[float] | None = None,
                doc: Sequence[str] | None = None,
                bm25_k1: float = 1.2, bm25_b: float = 0.75) -> float:
    """§6.3 的 ``S(e)``（已含硬过滤之外的五项加权 + 同情境加成），``round(S, 6)``。

    ``doc`` 为可选的预算好的文档 token（``MemoryStore`` 的索引缓存，NFR-05-2）；
    缺省时现算 ``doc_tokens(entry)``（纯函数调用路径）。
    """
    wrapped = float(weights.get("tag", 1.0))
    total_weight = sum(float(w) for w in obs_tags.values())
    if total_weight > 0.0:
        tag_sim = sum(float(w) for tag, w in obs_tags.items() if tag in set(entry.tags))
        tag_sim /= total_weight
    else:
        tag_sim = 0.0

    doc_tokens_ = doc if doc is not None else doc_tokens(entry)
    bm25 = bm25_score(query_tokens, doc_tokens_, df, n_docs, avgdl,
                      k1=bm25_k1, b=bm25_b)
    bm25_norm = _bm25_norm(bm25)

    vec_sim = 0.0
    w_vec = float(weights.get("vector", 0.0))
    if w_vec > 0.0 and entry.embedding and query_embedding:
        vec_sim = _cosine01(query_embedding, entry.embedding)

    # U(e) 是**存储的** utility（写入与 record_usage 时按 §6.4 重算并落盘），
    # 不是用本次检索的 tick 现算——否则同一条目的分数会随检索时刻漂移，
    # 且与 render_entry / memory.evict 事件里展示的 utility 不一致。
    util = float(entry.utility)
    recency = math.exp(-float(recency_lambda) * max(0, int(tick) - int(entry.created_tick)))
    score = (wrapped * tag_sim
             + float(weights.get("bm25", 0.6)) * bm25_norm
             + w_vec * vec_sim
             + float(weights.get("utility", 0.3)) * util
             + float(weights.get("recency", 0.1)) * recency)
    if entry.situation_key == obs_key:
        score += float(same_situation_bonus)
    return round6(score)


def _cosine01(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return (1.0 + dot / (norm_a * norm_b)) / 2.0


# =====================================================================
# 渲染与预算裁剪（FR-05-6 / §7.3 / NFR-05-4）
# =====================================================================

def render_entry(entry: MemoryEntry, *, max_chars: int = MAX_RENDER_CHARS) -> str:
    """单条一行：``[tags] lesson（hint=…, conf=…, util=…）``；``avoid`` 加「避免：」前缀。

    §7.3 给的是 ``do`` 的样例；``avoid`` 用「避免：」前缀显式标注极性（语义不可丢）。
    总长严格 ≤ ``max_chars``（NFR-05-4：单条渲染 ≤ 400 字符）。
    """
    tags = "|".join(entry.tags) if entry.tags else "-"
    hint = entry.action_hint or {}
    kind = str(hint.get("kind") or "-")
    slot = hint.get("slotKind")
    hint_text = f"{kind}→{slot}" if slot else kind
    head = f"[{tags}] "
    prefix = "避免：" if entry.polarity == "avoid" else ""
    tail = f"（hint={hint_text}, conf={entry.confidence:.2f}, util={entry.utility:.2f}）"
    room = max_chars - len(head) - len(prefix) - len(tail)
    lesson = _clip(entry.lesson, room) if room > 0 else ""
    line = f"{head}{prefix}{lesson}{tail}"
    return line if len(line) <= max_chars else line[:max_chars]


def render_entries(entries: Sequence[MemoryEntry], *,
                   max_chars: int = 1200) -> str:
    """按给定顺序逐行连接；装不下的行**跳过**（不截断文本），总长 ≤ ``max_chars``。"""
    lines: list[str] = []
    used = 0
    for entry in entries:
        line = render_entry(entry)
        addition = len(line) + (1 if lines else 0)
        if used + addition > max_chars:
            continue
        lines.append(line)
        used += addition
    return "\n".join(lines)


def select_with_budget(scored: Sequence[tuple[MemoryEntry, float]], *,
                       top_k: int, token_budget: int,
                       line_of: Any) -> tuple[list[RetrievedMemory], list[tuple[MemoryEntry, str, float]], int]:
    """贪心填预算：跳过装不下的（不截断），再截 ``top_k``（FR-05-6）。

    返回 ``(selected, dropped, used_tokens)``；``dropped`` 元素为
    ``(entry, reason, score)``。调用方保证 ``scored`` 已按
    ``(-round(S,6), entry_id)`` 升序。
    """
    selected: list[RetrievedMemory] = []
    dropped: list[tuple[MemoryEntry, str, float]] = []
    used = 0
    for entry, score in scored:
        if len(selected) >= top_k:
            dropped.append((entry, "top_k_exceeded", score))
            continue
        line = line_of(entry)
        tokens = estimate_tokens(line)
        if used + tokens > token_budget:
            dropped.append((entry, "token_budget_exceeded", score))
            continue
        selected.append(RetrievedMemory(entry=entry, score=score, tokens=tokens,
                                        rank=len(selected), text=line))
        used += tokens
    return selected, dropped, used


# ---- 内部小工具 -------------------------------------------------------------------

def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


def _opt_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_zero(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
