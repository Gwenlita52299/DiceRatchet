// kernel/index.js — 规则内核对外 API（GAME-01 §1）
// createGame / reduce / observe / legalActions / isLegal / configHash / digest / applyDifficulty / validateConfig / replay
// 注：全部函数显式接收 tables（配表对象）作为最后一个参数——内核不持有模块级可变状态（K-1）。

import { createRngState, randint, choice } from './rng.js';
import { digest, configHash } from './digest.js';
import { schoolMulPermille, arrowOf, counterOf, SCHOOLS, applyMul } from './counter.js';
import { describeConstraint, SLOT_KIND_NAMES } from './slots.js';
import { selfCorruptionValue } from './risk.js';
import {
  emptyStatuses, statusList, kernelError, newDie, dieDef, removeDieFromDeck, dieFaces, currentFace, dieRiskTier,
  deckSchoolInfo, isMono, assertInvariants, pad
} from './state.js';
import * as C from './combat.js';
import * as M from './map.js';
import * as S from './shop.js';
import * as F from './forge.js';
import * as E from './event.js';
import * as R from './relic.js';

export { digest, configHash };
export const KERNEL_VERSION = 'kernel-1.0.0';
export const OBS_SCHEMA = 'obs-1.0.0';
export const TOGGLE_KEYS = Object.freeze(['mapBranching', 'relics', 'events', 'shops', 'schoolCounter']);
export const ACTION_KINDS = Object.freeze([
  'map_choose', 'play_face', 'discard_die', 'purify', 'use_ability', 'end_turn',
  'shop_buy', 'shop_refresh', 'shop_remove', 'shop_leave', 'event_choose', 'treasure_pick',
  'forge_pick', 'shrine_choose', 'rest_choose', 'relic_swap', 'abandon_run'
]);

// ======================================================================
// 配置
// ======================================================================
export function applyDifficulty(base, difficulty, tables) {
  const lv = tables.difficulty.levels[difficulty];
  if (!lv) throw kernelError('E_KERNEL_BAD_CONFIG', `unknown difficulty ${difficulty}`);
  const common = tables.difficulty.common;
  const toggles = {};
  for (const k of TOGGLE_KEYS) toggles[k] = base.toggles && typeof base.toggles[k] === "boolean" ? !!base.toggles[k] : true;
  return {
    configVersion: 'cfg-1',
    difficulty,
    seed: base.seed >>> 0,
    character: base.character || 'warrior',
    toggles,
    debug: { showHiddenInfo: !!(base.debug && base.debug.showHiddenInfo), logActions: !(base.debug && base.debug.logActions === false) },
    heartsMax: lv.heartsMax, virtueMax: lv.virtueMax, drawPerTurn: lv.drawPerTurn, handMax: lv.handMax,
    enemyHpMulPermille: lv.enemyHpMulPermille, enemyDmgMulPermille: lv.enemyDmgMulPermille, enemyExtraDice: lv.enemyExtraDice,
    counterIntensity: lv.counterIntensity,
    counterMulPermille: { advantage: lv.counterMulPermille.advantage, disadvantage: lv.counterMulPermille.disadvantage },
    monoPenaltyPermille: lv.monoPenaltyPermille, monoThresholdPermille: common.monoThresholdPermille, monoMinDice: common.monoMinDice,
    shopPriceMulPermille: lv.shopPriceMulPermille, goldMulPermille: lv.goldMulPermille,
    relicSlots: lv.relicSlots, purifyPerStage: lv.purifyPerStage, eliteStages: lv.eliteStages.slice(),
    bossPhaseHpBonusPermille: lv.bossPhaseHpBonusPermille, reviveOnce: lv.reviveOnce,
    stageCount: common.stageCount, maxTurnsPerBattle: common.maxTurnsPerBattle
  };
}

export function validateConfig(config, tables) {
  const errors = [];
  if (!tables.difficulty.levels[config.difficulty]) errors.push('difficulty');
  if (!Number.isInteger(config.seed) || config.seed < 0 || config.seed >= 4294967296) errors.push('seed');
  if (!tables.characters[config.character]) errors.push('character');
  for (const k of TOGGLE_KEYS) if (typeof config.toggles[k] !== 'boolean') errors.push(`toggles.${k}`);
  return { ok: errors.length === 0, errors };
}

// ======================================================================
// createGame
// ======================================================================
export function createGame(input, tables) {
  const config = input.configVersion ? input : applyDifficulty(input, input.difficulty || 'medium', tables);
  const v = validateConfig(config, tables);
  if (!v.ok) throw kernelError('E_KERNEL_BAD_CONFIG', v.errors.join(','));
  const ch = tables.characters[config.character];
  if (!ch.startingDeck.length) throw kernelError('E_KERNEL_EMPTY_DECK');
  const state = {
    stateVersion: 'gs-1',
    runId: input.runId || `r-${configHash(config).slice(7, 19)}`,
    configHash: configHash(config),
    config,
    stage: 1, chapter: 1, turn: 0, steps: 0,
    phase: 'map',
    player: {
      hearts: config.heartsMax + ch.heartsBonus, heartsMax: config.heartsMax + ch.heartsBonus,
      virtue: config.virtueMax + ch.virtueBonus, virtueMax: config.virtueMax + ch.virtueBonus,
      ward: 0, starShards: 0, statuses: emptyStatuses(), charge: 0,
      purifyLeft: config.purifyPerStage, abilities: [],
      deck: [], drawPile: [], discardPile: [], hand: [], dice: {},
      slots: ch.startingSlots.map((s) => ({ ...s, reusable: !!s.reusable, filledBy: null, disabledUntilTurn: null })),
      relics: [], removeCount: 0,
      pendingVirtue: 0, pendingStatuses: {}, pendingDraw: 0,
      firstPlayDone: false, bladeAttackedThisTurn: false, arcanaLastTurn: 0, arcanaThisTurn: 0, drainNext: 0, reviveUsed: false
    },
    enemies: [],
    map: null,
    node: null,
    rng: createRngState(config.seed),
    dieSeq: 0, shopSeq: 0, slotSeq: 0,
    seenEvents: [],
    pendingRelic: null,
    stats: { turns: 0, damageDealt: 0, kills: 0, counterHits: 0, skillFaceUses: 0, battlesWon: 0, battlesLost: 0, stagesCleared: 0 },
    illegalCount: 0, consecutiveIllegal: 0,
    outcome: null, outcomeReason: null, truncationReason: null
  };
  for (const defId of ch.startingDeck) state.player.deck.push(newDie(state, defId));
  state.map = M.generateMaps(state, tables);
  return state;
}

// ======================================================================
// 节点流程
// ======================================================================
function enterNode(ctx, node) {
  const { state, tables, events } = ctx;
  state.node = { nodeId: node.nodeId, kind: node.kind, queue: ['complete'] };
  events.push({ type: 'node_enter', nodeId: node.nodeId, kind: node.kind });
  switch (node.kind) {
    case 'battle': case 'elite': case 'boss':
      C.startBattle(ctx, M.groupMembers(tables, node.enemyGroupId), node.kind); break;
    case 'shop': S.enterShop(ctx); break;
    case 'forge': F.enterForge(ctx, false); break;
    case 'event':
      if (!E.enterEvent(ctx)) { state.node.kind = 'treasure'; S.enterTreasure(ctx); }
      break;
    case 'treasure': S.enterTreasure(ctx); break;
    case 'shrine': S.enterShrine(ctx); break;
    case 'rest': S.enterRest(ctx); break;
    default: throw kernelError('E_KERNEL_NODE_KIND', node.kind);
  }
}

function completeNode(ctx) {
  const { state, tables, events } = ctx;
  events.push({ type: 'node_leave', nodeId: state.node ? state.node.nodeId : null });
  state.node = null;
  state.enemies = [];
  const sm = M.currentStageMap(state);
  if (state.map.currentRow >= sm.rows.length - 1) {
    state.stats.stagesCleared += 1;
    if (state.stage >= state.config.stageCount) {
      state.outcome = 'win';
      state.phase = 'settle';
      return;
    }
    state.stage += 1;
    state.chapter = M.stageDef(tables, state.stage).chapter;
    state.map.currentRow = -1;
    state.map.currentNodeId = null;
    events.push({ type: 'stage_enter', stage: state.stage, chapter: state.chapter });
  }
  state.phase = 'map';
}

function runQueue(ctx) {
  const { state } = ctx;
  while (state.node && state.node.queue.length) {
    const step = state.node.queue.shift();
    if (step === 'complete') { completeNode(ctx); return; }
    if (step === 'treasure') { S.enterTreasure(ctx); return; }
    if (step.startsWith('relic:')) {
      if (!R.grantRelic(ctx, step.slice(6), null)) return;
    }
  }
}

function resetBattleState(state) {
  const p = state.player;
  for (const id of p.deck.slice()) if (p.dice[id].temporary) removeDieFromDeck(state, id);
  p.drawPile = []; p.discardPile = []; p.hand = [];
  for (const id of p.deck) p.dice[id].isCorrupt = false;
  p.ward = 0; p.statuses = emptyStatuses(); p.charge = 0;
  for (const s of p.slots) { s.filledBy = null; s.disabledUntilTurn = null; }
  p.virtue = p.virtueMax;
  state.turn = 0;
}

function finishBattle(ctx) {
  const { state, tables, events } = ctx;
  const b = state.node.battle;
  const result = b.result;
  const kind = b.kind;
  const enemies = state.enemies;
  resetBattleState(state);
  state.node.battle = null;
  if (result === 'dead') {
    events.push({ type: 'battle_end', result: 'lose' });
    state.outcome = 'lose';
    state.outcomeReason = 'hearts';
    state.phase = 'settle';
    state.stats.battlesLost += 1;
    return;
  }
  if (result === 'lost' || result === 'truncated') {
    state.stats.battlesLost += 1;
    events.push({ type: 'battle_end', result, heartsLeft: state.player.hearts });
    state.node.queue = ['complete'];
    runQueue(ctx);
    return;
  }
  state.stats.battlesWon += 1;
  // 胜利掉落（loot 流）
  let gold = 0;
  for (const e of enemies) {
    const [lo, hi] = tables.shops.loot[e.tier];
    gold += randint(state, 'loot', lo, hi);
  }
  gold = applyMul(gold, R.goldMul(state, tables));
  state.player.starShards += gold;
  events.push({ type: 'battle_end', result: 'win', drops: { starShards: gold } });
  if (kind === 'boss' && state.stage >= state.config.stageCount) {
    state.outcome = 'win';
    state.phase = 'settle';
    state.node = null;
    return;
  }
  const queue = [];
  if ((kind === 'elite' || kind === 'boss') && state.config.toggles.relics) {
    const pool = R.relicPool(state, tables, kind === 'boss' ? ['boss', 'rare'] : ['common', 'rare']);
    if (pool.length) queue.push(`relic:${choice(state, 'loot', pool)}`);
  }
  queue.push('treasure'); // 战斗胜利：骰子 3 选 1（loot 流，GAME-01 §3.4）
  queue.push('complete');
  state.node.queue = queue;
  state.phase = 'map';
  runQueue(ctx);
}

// ======================================================================
// 合法性
// ======================================================================
function illegal(code, reason) { return { legal: false, code, reason }; }
const OK = Object.freeze({ legal: true, code: null, reason: null });

/** 规范化动作：补全默认目标（便于 UI/自动化客户端） */
export function normalizeAction(state, action, tables) {
  if (!action || typeof action !== 'object') return action;
  const a = { ...action };
  if (a.kind === 'play_face' && state.phase === 'player_turn' && state.player.hand.includes(a.dieId)) {
    const need = C.faceNeedsTarget(currentFace(state, tables, a.dieId));
    if (a.targetId == null) {
      if (need === 'enemy') a.targetId = (C.aliveEnemies(state)[0] || {}).enemyId || null;
      else if (need === 'die') a.targetId = state.player.hand.find((d) => d !== a.dieId) || null;
      else a.targetId = null;
    }
  }
  return a;
}

export function isLegal(state, action, tables) {
  if (!action || typeof action !== 'object' || !ACTION_KINDS.includes(action.kind)) return illegal('E_ACTION_SYNTAX', 'unknown action kind');
  if (state.phase === 'settle') return illegal('E_ACTION_PHASE', '对局已结束');
  const a = normalizeAction(state, action, tables);
  const p = state.player;
  const phaseIs = (...ph) => ph.includes(state.phase);
  switch (a.kind) {
    case 'abandon_run': return OK;
    case 'map_choose':
      if (!phaseIs('map')) return illegal('E_ACTION_PHASE', '不在地图阶段');
      if (!M.reachableNodeIds(state).includes(a.nodeId)) return illegal('E_ACTION_NODE_UNREACHABLE', '节点不可达');
      return OK;
    case 'play_face': {
      if (!phaseIs('player_turn')) return illegal('E_ACTION_PHASE', '不在玩家回合');
      const r = C.checkPlayFace(state, tables, a);
      return r ? illegal(r.code, r.reason) : OK;
    }
    case 'discard_die':
      if (!phaseIs('player_turn')) return illegal('E_ACTION_PHASE', '不在玩家回合');
      if (!p.hand.includes(a.dieId)) return illegal('E_ACTION_DIE_NOT_IN_HAND', '骰子不在手中');
      return OK;
    case 'purify':
      if (!phaseIs('player_turn')) return illegal('E_ACTION_PHASE', '不在玩家回合');
      if (!p.hand.includes(a.dieId)) return illegal('E_ACTION_DIE_NOT_IN_HAND', '骰子不在手中');
      if (!p.dice[a.dieId].isCorrupt) return illegal('E_ACTION_NOT_CORRUPT', '不是魔化骰');
      if (p.purifyLeft <= 0) return illegal('E_ACTION_NO_PURIFY', '净化次数已用完');
      return OK;
    case 'use_ability': {
      if (!phaseIs('player_turn')) return illegal('E_ACTION_PHASE', '不在玩家回合');
      const ab = p.abilities.find((x) => x.id === a.abilityId);
      if (!ab) return illegal('E_ACTION_UNKNOWN_ABILITY', '未知能力');
      if (ab.usesLeft <= 0) return illegal('E_ACTION_ABILITY_EXHAUSTED', '能力次数已用完');
      if (a.abilityId === 'warrior_reroll' && !p.hand.length) return illegal('E_ACTION_ABILITY_NO_TARGET', '手中没有骰子');
      if (a.abilityId === 'warrior_focus') {
        if (!p.hand.includes(a.dieId)) return illegal('E_ACTION_DIE_NOT_IN_HAND', '骰子不在手中');
        if (!Number.isInteger(a.value) || a.value < 1 || a.value > 6) return illegal('E_ACTION_SYNTAX', 'value 1..6');
        if (p.dice[a.dieId].faceIndex === a.value - 1) return illegal('E_ACTION_NOOP', '点数未改变');
      }
      return OK;
    }
    case 'end_turn': {
      if (!phaseIs('player_turn')) return illegal('E_ACTION_PHASE', '不在玩家回合');
      const left = C.corruptRemaining(state);
      if (left.length) return illegal('E_ACTION_CORRUPT_DIE_REMAINS', `corrupt_die_remains: 还有 ${left.length} 颗魔化骰未处理`);
      return OK;
    }
    case 'shop_buy': {
      if (!phaseIs('shop')) return illegal('E_ACTION_PHASE', '不在商店');
      const it = state.node.shop.items.find((x) => x.itemId === a.itemId);
      if (!it || it.sold) return illegal('E_ACTION_UNKNOWN_ITEM', '商品不存在');
      if (p.starShards < it.price) return illegal('E_ACTION_NOT_AFFORDABLE', '星屑不足');
      return OK;
    }
    case 'shop_refresh':
      if (!phaseIs('shop')) return illegal('E_ACTION_PHASE', '不在商店');
      if (state.node.shop.refreshLeft <= 0) return illegal('E_ACTION_NO_REFRESH', '刷新次数已用完');
      if (p.starShards < S.refreshPrice(state, tables)) return illegal('E_ACTION_NOT_AFFORDABLE', '星屑不足');
      return OK;
    case 'shop_remove':
      if (!phaseIs('shop')) return illegal('E_ACTION_PHASE', '不在商店');
      if (!S.removableDice(state, tables).includes(a.dieId)) return illegal('E_ACTION_NOT_REMOVABLE', '该骰子不可删除');
      if (p.starShards < S.removePrice(state, tables)) return illegal('E_ACTION_NOT_AFFORDABLE', '星屑不足');
      return OK;
    case 'shop_leave':
      if (phaseIs('shop', 'forge')) return OK;
      if (phaseIs('event') && state.node.event.resolved) return OK;
      return illegal('E_ACTION_PHASE', '当前不能离开');
    case 'event_choose': {
      if (!phaseIs('event') || state.node.event.resolved) return illegal('E_ACTION_PHASE', '不在事件中');
      const ev = tables.events[state.node.event.eventId];
      const m = /^opt_(\d+)$/.exec(a.optionId || '');
      if (!m || !ev.options[parseInt(m[1], 10)]) return illegal('E_ACTION_UNKNOWN_OPTION', '未知选项');
      if (!E.requirementMet(state, ev.options[parseInt(m[1], 10)].requirement)) return illegal('E_ACTION_REQUIREMENT', '不满足选项要求');
      return OK;
    }
    case 'treasure_pick':
      if (!phaseIs('treasure')) return illegal('E_ACTION_PHASE', '不在宝箱');
      if (a.defId !== 'skip' && !state.node.treasure.offers.includes(a.defId)) return illegal('E_ACTION_UNKNOWN_ITEM', '无此选项');
      return OK;
    case 'forge_pick': {
      if (!phaseIs('forge')) return illegal('E_ACTION_PHASE', '不在锻造');
      const f = state.node.forge;
      if (f.done) return illegal('E_ACTION_FORGE_DONE', '本次已锻造');
      if (!F.forgeableDice(state, tables).includes(a.dieId)) return illegal('E_ACTION_NOT_FORGEABLE', '该骰子不可锻造');
      if (!Number.isInteger(a.faceIndex) || a.faceIndex < 0 || a.faceIndex > 5) return illegal('E_ACTION_SYNTAX', 'faceIndex');
      if (!f.candidates.includes(a.candidateId)) return illegal('E_ACTION_UNKNOWN_ITEM', '无此候选面');
      if (p.starShards < f.price) return illegal('E_ACTION_NOT_AFFORDABLE', '星屑不足');
      return OK;
    }
    case 'shrine_choose': {
      if (!phaseIs('shrine')) return illegal('E_ACTION_PHASE', '不在神殿');
      const o = state.node.shrine.options.find((x) => x.optionId === a.optionId);
      if (!o || !o.available) return illegal('E_ACTION_UNKNOWN_OPTION', '选项不可用');
      return OK;
    }
    case 'rest_choose': {
      if (!phaseIs('rest')) return illegal('E_ACTION_PHASE', '不在休息点');
      const o = state.node.rest.options.find((x) => x.optionId === a.optionId);
      if (!o || !o.available) return illegal('E_ACTION_UNKNOWN_OPTION', '选项不可用');
      return OK;
    }
    case 'relic_swap':
      if (!phaseIs('relic_swap')) return illegal('E_ACTION_PHASE', '无需替换遗物');
      if (a.relicId !== 'skip' && !p.relics.includes(a.relicId)) return illegal('E_ACTION_UNKNOWN_ITEM', '未拥有该遗物');
      return OK;
    default:
      return illegal('E_ACTION_SYNTAX', 'unknown');
  }
}

/** 枚举全部合法动作（候选 → isLegal 过滤，保证与校验同源） */
export function legalActions(state, tables) {
  const cands = [];
  const p = state.player;
  switch (state.phase) {
    case 'map':
      for (const id of M.reachableNodeIds(state)) cands.push({ kind: 'map_choose', nodeId: id });
      break;
    case 'player_turn': {
      const enemies = C.aliveEnemies(state).map((e) => e.enemyId);
      for (const dieId of p.hand) {
        const need = C.faceNeedsTarget(currentFace(state, tables, dieId));
        const targets = need === 'enemy' ? enemies : need === 'die' ? (p.hand.filter((d) => d !== dieId).length ? p.hand.filter((d) => d !== dieId) : [null]) : [null];
        for (const s of p.slots) for (const t of targets) cands.push({ kind: 'play_face', dieId, slotId: s.slotId, targetId: t });
        cands.push({ kind: 'discard_die', dieId });
        cands.push({ kind: 'purify', dieId });
      }
      cands.push({ kind: 'use_ability', abilityId: 'warrior_reroll' });
      for (const dieId of p.hand) for (let v = 1; v <= 6; v++) cands.push({ kind: 'use_ability', abilityId: 'warrior_focus', dieId, value: v });
      cands.push({ kind: 'end_turn' });
      break;
    }
    case 'shop':
      for (const it of state.node.shop.items) cands.push({ kind: 'shop_buy', itemId: it.itemId });
      cands.push({ kind: 'shop_refresh' });
      for (const id of p.deck) cands.push({ kind: 'shop_remove', dieId: id });
      cands.push({ kind: 'shop_leave' });
      break;
    case 'forge':
      if (!state.node.forge.done) {
        for (const id of F.forgeableDice(state, tables)) for (let fi = 0; fi < 6; fi++) for (const c of state.node.forge.candidates) {
          cands.push({ kind: 'forge_pick', dieId: id, faceIndex: fi, candidateId: c });
        }
      }
      cands.push({ kind: 'shop_leave' });
      break;
    case 'event':
      tables.events[state.node.event.eventId].options.forEach((_, i) => cands.push({ kind: 'event_choose', optionId: `opt_${i}` }));
      cands.push({ kind: 'shop_leave' });
      break;
    case 'treasure':
      for (const d of state.node.treasure.offers) cands.push({ kind: 'treasure_pick', defId: d });
      cands.push({ kind: 'treasure_pick', defId: 'skip' });
      break;
    case 'shrine':
      for (const o of state.node.shrine.options) cands.push({ kind: 'shrine_choose', optionId: o.optionId });
      break;
    case 'rest':
      for (const o of state.node.rest.options) cands.push({ kind: 'rest_choose', optionId: o.optionId });
      break;
    case 'relic_swap':
      for (const r of p.relics) cands.push({ kind: 'relic_swap', relicId: r });
      cands.push({ kind: 'relic_swap', relicId: 'skip' });
      break;
    default:
      break;
  }
  const out = cands.filter((a) => isLegal(state, a, tables).legal);
  if (state.phase !== 'settle') out.push({ kind: 'abandon_run' });
  return out;
}

// ======================================================================
// reduce
// ======================================================================
function dispatch(ctx, a) {
  const { state } = ctx;
  switch (a.kind) {
    case 'abandon_run':
      state.outcome = 'lose'; state.outcomeReason = 'abandon'; state.phase = 'settle';
      ctx.events.push({ type: 'run_abandoned' });
      return;
    case 'map_choose': {
      const node = M.findNode(state, a.nodeId);
      node.visited = true;
      state.map.currentRow = node.row;
      state.map.currentNodeId = node.nodeId;
      state.map.path.push(node.nodeId);
      enterNode(ctx, node);
      return;
    }
    case 'play_face': C.playFace(ctx, a); return;
    case 'discard_die': C.discardDie(ctx, a.dieId); return;
    case 'purify': C.purifyDie(ctx, a.dieId); return;
    case 'use_ability': C.useAbility(ctx, a); return;
    case 'end_turn': C.endTurn(ctx); return;
    case 'shop_buy': S.shopBuy(ctx, a.itemId); return;
    case 'shop_refresh': S.shopRefresh(ctx); return;
    case 'shop_remove': S.shopRemove(ctx, a.dieId); return;
    case 'shop_leave': runQueue(ctx); return;
    case 'event_choose': E.eventChoose(ctx, a.optionId); return;
    case 'treasure_pick': S.treasurePick(ctx, a.defId); runQueue(ctx); return;
    case 'forge_pick': F.forgePick(ctx, a); return;
    case 'shrine_choose': if (S.shrineChoose(ctx, a.optionId)) runQueue(ctx); return;
    case 'rest_choose': S.restChoose(ctx, a.optionId); runQueue(ctx); return;
    case 'relic_swap': {
      const ret = R.swapRelic(ctx, a.relicId);
      if (ret) state.phase = ret; else runQueue(ctx);
      return;
    }
    default:
      throw kernelError('E_KERNEL_DISPATCH', a.kind);
  }
}

export function reduce(state, action, tables) {
  const verdict = isLegal(state, action, tables);
  if (!verdict.legal) {
    return { state, events: [{ type: 'illegal', code: verdict.code, reason: verdict.reason }], illegal: true, code: verdict.code, reason: verdict.reason };
  }
  try {
    const draft = structuredClone(state);
    const ctx = { state: draft, tables, events: [] };
    const a = normalizeAction(draft, action, tables);
    draft.steps += 1;
    dispatch(ctx, a);
    if (draft.node && draft.node.battle && draft.node.battle.result) finishBattle(ctx);
    if (draft.config.debug.showHiddenInfo) assertInvariants(draft);
    return { state: draft, events: ctx.events, illegal: false };
  } catch (err) {
    const code = err.code && String(err.code).startsWith('E_KERNEL') ? err.code : 'E_KERNEL_EXCEPTION';
    return { state, events: [{ type: 'error', code, reason: err.message }], illegal: false, error: { code, message: err.message } };
  }
}

/** 测试/平衡脚本用：在当前 state 上直接开启一场指定编队的战斗（不经地图）。不用于正式对局。 */
export function debugStartBattle(state, memberIds, tables, kind = 'battle') {
  const draft = structuredClone(state);
  const ctx = { state: draft, tables, events: [] };
  draft.node = { nodeId: 'n-debug', kind, queue: ['complete'] };
  C.startBattle(ctx, memberIds, kind);
  return draft;
}

export function replay(config, actions, tables) {
  let state = createGame(config, tables);
  const steps = [{ obsDigest: digest(observe(state, 'full', tables)), events: [] }];
  for (const a of actions) {
    const r = reduce(state, a, tables);
    state = r.state;
    steps.push({ obsDigest: digest(observe(state, 'full', tables)), events: r.events });
  }
  return { steps, digests: steps.map((s) => s.obsDigest), outcome: state.outcome, state };
}

// ======================================================================
// observe（白名单显式构造，GAME-01 §5）
// ======================================================================
function faceView(f) {
  return {
    faceIndex: f.faceIndex, label: f.label, kind: f.kind, value: f.value, school: f.school, target: f.target,
    statusKind: f.statusKind || null, selfCorruption: selfCorruptionValue(f), tags: (f.tags || []).slice(), forged: !!f.forged
  };
}

function dieSummary(state, tables, id) {
  const die = state.player.dice[id];
  const def = dieDef(tables, die.defId);
  return {
    dieId: id, defId: die.defId, name: def.name, category: def.category, school: def.school,
    riskTier: dieRiskTier(state, tables, id), forged: !!die.forged,
    faces: dieFaces(state, tables, id).map(faceView)
  };
}

function enemyIntentView(state, tables, e) {
  return e.intent.map((it) => {
    const base = { slotId: it.slotId, pip: it.pip, kind: it.kind, school: e.school, hits: it.hits };
    if (it.kind === 'purge') {
      const r = enemyAttackPreview(state, tables, e, it.value);
      return { ...base, value: r.value, raw: r.raw, schoolMul: r.mulPermille, monoPermille: r.monoPermille, arrow: state.config.toggles.schoolCounter ? r.arrow : 'eq' };
    }
    return { ...base, value: it.value, statusKind: it.statusKind || null, target: it.target || null, arrow: 'eq' };
  });
}

function enemyAttackPreview(state, tables, e, v) { return C.enemyAttackValue(state, tables, e, v); }

function mapView(state, tables) {
  const sm = M.currentStageMap(state);
  const reach = M.reachableNodeIds(state);
  const deck = deckSchoolInfo(state, tables).school;
  const rows = sm.rows.map((row, r) => {
    if (r > Math.max(state.map.currentRow, 0) + 1) return { count: row.length, reachable: false };
    return row.map((n) => {
      const view = { nodeId: n.nodeId, kind: n.kind, visited: n.visited, reachable: state.phase === 'map' && reach.includes(n.nodeId), edges: n.edges.slice(), preview: null, school: null };
      if (n.enemyGroupId) {
        const members = M.groupMembers(tables, n.enemyGroupId).map((id) => tables.monsters[id]);
        const school = members[0].phases ? members[0].phases[0].school : members[0].school;
        const mulIn = schoolMulPermille(state.config, school, deck);
        view.school = school;
        view.preview = { enemyNames: members.map((m) => m.name), schools: members.map((m) => (m.phases ? m.phases[0].school : m.school)), arrow: state.config.toggles.schoolCounter ? arrowOf(mulIn) : 'eq' };
      }
      return view;
    });
  });
  return { stage: sm.stage, chapter: sm.chapter, rows, currentRow: state.map.currentRow, currentNodeId: state.map.currentNodeId, path: state.map.path.slice() };
}

function battleHints(state, tables) {
  const hints = [];
  if (!state.config.toggles.schoolCounter || !state.enemies.length) return hints;
  if (M.stageDef(tables, state.stage).tutorial) return hints; // 教学关不提示学派盲区
  const owned = new Set();
  for (const id of state.player.deck) for (const f of dieFaces(state, tables, id)) owned.add(f.school);
  const names = tables.schools.schools;
  const seen = new Set();
  for (const e of C.aliveEnemies(state)) {
    const c = counterOf(e.school);
    if (c && !owned.has(c) && !seen.has(e.school)) {
      seen.add(e.school);
      hints.push(`你的骰池没有能克制${names[e.school].name}的骰子`);
    }
  }
  if (isMono(state, tables) && state.config.monoPenaltyPermille > 0 && !R.monoPenaltyDisabled(state)) {
    hints.push(`骰池学派过于集中：敌人伤害 +${Math.floor(state.config.monoPenaltyPermille / 10)}%`);
  }
  return hints;
}

function nodeViews(state, tables) {
  const out = { shop: null, event: null, treasure: null, forge: null, shrine: null, rest: null };
  const n = state.node;
  if (!n) return out;
  const p = state.player;
  if (n.shop && state.phase === 'shop') {
    out.shop = {
      items: n.shop.items.map((it) => {
        const base = { itemId: it.itemId, kind: it.kind, defId: it.defId, price: it.price, sold: it.sold, affordable: p.starShards >= it.price };
        if (it.kind === 'die') {
          const d = tables.skills[it.defId];
          return { ...base, name: d.name, school: d.school, riskTier: d.riskTier, faces: d.faces.map(faceView) };
        }
        const r = tables.relics[it.defId];
        return { ...base, name: r.name, desc: r.desc, rarity: r.rarity, school: null };
      }),
      refreshLeft: n.shop.refreshLeft, refreshPrice: S.refreshPrice(state, tables),
      removePrice: S.removePrice(state, tables), removable: S.removableDice(state, tables),
      counterSchool: counterOf(S.stageTheme(state, tables))
    };
  }
  if (n.event && (state.phase === 'event' || state.phase === 'relic_swap')) {
    const ev = tables.events[n.event.eventId];
    out.event = {
      eventId: ev.id, title: ev.title, text: ev.text, school: ev.school, resolved: n.event.resolved, resultText: n.event.resultText,
      chosen: n.event.chosen,
      options: ev.options.map((o, i) => ({ optionId: `opt_${i}`, label: o.label, requirement: E.requirementText(o.requirement), affordable: E.requirementMet(state, o.requirement), risk: o.risk }))
    };
  }
  if (n.treasure && state.phase === 'treasure') {
    out.treasure = {
      offers: n.treasure.offers.map((id) => {
        const d = tables.skills[id];
        return { defId: id, name: d.name, school: d.school, riskTier: d.riskTier, faces: d.faces.map(faceView), counter: d.school === n.treasure.counterSchool };
      }),
      counterSchool: n.treasure.counterSchool,
      skipGold: applyMul(tables.shops.treasure.skipGold, R.goldMul(state, tables))
    };
  }
  if (n.forge && state.phase === 'forge') {
    out.forge = {
      price: n.forge.price, done: n.forge.done, affordable: p.starShards >= n.forge.price,
      candidates: n.forge.candidates.map((id) => ({ candidateId: id, ...faceView({ ...tables.forgeFaces[id].face, faceIndex: 0 }) })),
      forgeable: F.forgeableDice(state, tables)
    };
  }
  if (n.shrine && state.phase === 'shrine') out.shrine = { options: n.shrine.options.map((o) => ({ optionId: o.optionId, label: o.label, available: o.available })) };
  if (n.rest && state.phase === 'rest') out.rest = { options: n.rest.options.map((o) => ({ ...o })) };
  return out;
}

export function observe(state, view = 'full', tables) {
  const p = state.player;
  const c = state.config;
  const ds = deckSchoolInfo(state, tables);
  const inBattle = state.phase === 'player_turn' || state.phase === 'enemy_turn';
  const nodes = nodeViews(state, tables);
  const obs = {
    schemaVersion: OBS_SCHEMA,
    runId: state.runId,
    phase: state.phase,
    stage: state.stage,
    chapter: state.chapter,
    chapterName: M.chapterDef(tables, state.chapter).name,
    turn: state.turn,
    difficulty: c.difficulty,
    player: {
      hearts: p.hearts, heartsMax: p.heartsMax, virtue: p.virtue, virtueMax: p.virtueMax, ward: p.ward,
      starShards: p.starShards, statuses: statusList(p.statuses), charge: p.charge,
      deckSchool: ds.school, deckSchoolCounts: { ...ds.counts }, deckConcentrationPermille: ds.concentrationPermille,
      purifyLeft: p.purifyLeft,
      abilities: [
        ...p.abilities.map((a) => ({ id: a.id, usesLeft: inBattle ? a.usesLeft : 0 })),
        { id: 'warrior_purify', usesLeft: inBattle ? p.purifyLeft : 0 }
      ],
      pendingVirtue: p.pendingVirtue, pendingDraw: p.pendingDraw, pendingStatuses: statusList({ ...emptyStatuses(), ...p.pendingStatuses })
    },
    hand: inBattle ? p.hand.map((id) => {
      const die = p.dice[id];
      const f = currentFace(state, tables, id);
      const def = dieDef(tables, die.defId);
      return {
        dieId: id, defId: die.defId, name: def.name, category: def.category, school: f.school, dieSchool: def.school,
        riskTier: dieRiskTier(state, tables, id), faceIndex: die.faceIndex, pip: die.faceIndex + 1,
        label: f.label, kind: f.kind, value: f.value, statusKind: f.statusKind || null, target: f.target,
        isCorrupt: die.isCorrupt, corruptionValue: selfCorruptionValue(f), needsTarget: C.faceNeedsTarget(f),
        faces: dieFaces(state, tables, id).map(faceView)
      };
    }) : [],
    piles: { draw: p.drawPile.length, discard: p.discardPile.length, deck: p.deck.length },
    deck: p.deck.map((id) => dieSummary(state, tables, id)),
    slots: p.slots.map((s) => ({
      slotId: s.slotId, slotKind: s.slotKind, slotKindName: SLOT_KIND_NAMES[s.slotKind], constraint: s.constraint,
      constraintText: describeConstraint(s.constraint), bonus: s.bonus, reusable: s.reusable,
      filledBy: s.filledBy && p.dice[s.filledBy] ? { dieId: s.filledBy, label: currentFace(state, tables, s.filledBy).label } : null,
      effectKind: { attack: 'purge', defense: 'ward', heal: 'mend', utility: 'any', cursed: 'selfCorruption' }[s.slotKind],
      disabled: inBattle && C.slotDisabled(state, s)
    })),
    enemies: C.aliveEnemies(state).map((e) => ({
      enemyId: e.enemyId, defId: e.defId, name: e.name, tier: e.tier, school: e.school, hp: e.hp, hpMax: e.hpMax, ward: e.ward,
      statuses: statusList(e.statuses), intent: enemyIntentView(state, tables, e), phase: e.phase,
      phaseCount: tables.monsters[e.defId].phases ? tables.monsters[e.defId].phases.length : 1,
      taunt: e.taunt, passives: e.passives.map((x) => x.kind),
      arrowVsYou: c.toggles.schoolCounter ? arrowOf(schoolMulPermille(c, e.school, ds.school)) : 'eq'
    })),
    battleKind: state.node && state.node.battle ? state.node.battle.kind : null,
    battleHints: inBattle ? battleHints(state, tables) : [],
    map: state.map ? mapView(state, tables) : null,
    stages: tables.stages.stages.map((s) => ({ stage: s.stage, chapter: s.chapter, boss: s.boss, elite: c.eliteStages.includes(s.stage) })),
    shop: nodes.shop, event: nodes.event, treasure: nodes.treasure, forge: nodes.forge, shrine: nodes.shrine, rest: nodes.rest,
    pendingRelic: state.pendingRelic ? { relicId: state.pendingRelic.relicId, name: tables.relics[state.pendingRelic.relicId].name, desc: tables.relics[state.pendingRelic.relicId].desc } : null,
    toggles: { ...c.toggles },
    config: {
      heartsMax: c.heartsMax, virtueMax: c.virtueMax, drawPerTurn: c.drawPerTurn, handMax: c.handMax,
      enemyHpMulPermille: c.enemyHpMulPermille, enemyDmgMulPermille: c.enemyDmgMulPermille, enemyExtraDice: c.enemyExtraDice,
      counterIntensity: c.counterIntensity, counterMulPermille: { ...c.counterMulPermille }, monoPenaltyPermille: c.monoPenaltyPermille,
      relicSlots: c.relicSlots, purifyPerStage: c.purifyPerStage, eliteStages: c.eliteStages.slice(), reviveOnce: c.reviveOnce
    },
    relics: c.toggles.relics ? p.relics.map((id) => ({ relicId: id, name: tables.relics[id].name, desc: tables.relics[id].desc, rarity: tables.relics[id].rarity })) : [],
    stats: { turns: state.stats.turns, damageDealt: state.stats.damageDealt, kills: state.stats.kills, counterHits: state.stats.counterHits, skillFaceUses: state.stats.skillFaceUses, battlesWon: state.stats.battlesWon, battlesLost: state.stats.battlesLost, stagesCleared: state.stats.stagesCleared },
    legalActionKinds: [...new Set(legalActions(state, tables).map((a) => a.kind))].sort(),
    outcome: state.outcome,
    outcomeReason: state.outcomeReason,
    truncationReason: state.truncationReason,
    seed: null
  };
  if (view === 'visual') {
    return {
      schemaVersion: OBS_SCHEMA, view: 'visual', phase: obs.phase,
      dice: obs.hand.map((h, i) => ({ slot: i, text: `${h.pip}·${h.label}`, corrupt: h.isCorrupt })),
      enemies: obs.enemies.map((e, i) => ({ slot: i, name: e.name, hpRatioPermille: Math.floor((e.hp * 1000) / Math.max(1, e.hpMax)) })),
      slots: obs.slots.map((s) => ({ slotId: s.slotId, occupied: !!s.filledBy })),
      particles: []
    };
  }
  return obs;
}

// ======================================================================
// UI 提示（伤害预览/非法原因）——规则仍在内核计算，前端只展示
// ======================================================================
export function playHints(state, tables) {
  if (state.phase !== 'player_turn') return null;
  const p = state.player;
  const out = {};
  const enemies = C.aliveEnemies(state);
  for (const dieId of p.hand) {
    const face = currentFace(state, tables, dieId);
    const need = C.faceNeedsTarget(face);
    out[dieId] = {};
    for (const s of p.slots) {
      const probe = normalizeAction(state, { kind: 'play_face', dieId, slotId: s.slotId }, tables);
      const r = C.checkPlayFace(state, tables, probe);
      const hint = { legal: !r, code: r ? r.code : null, reason: r ? (r.code === 'E_ACTION_CONSTRAINT_VIOLATION' ? describeConstraint(s.constraint) : r.reason) : null, needsTarget: need, previews: null };
      if (!r && face.kind === 'purge') {
        let school = face.school;
        let valueBonus = 0;
        if (R.swiftHandActive(state)) { const d = deckSchoolInfo(state, tables).school; if (d !== 'neutral') school = d; valueBonus = 1; }
        hint.previews = enemies.map((e) => {
          const v = C.playerAttackValue(state, tables, face, { school, slotBonus: s.bonus, valueBonus }, e);
          return { enemyId: e.enemyId, raw: v.raw, mulPermille: v.mulPermille, relicMulPermille: v.relicMulPermille, value: v.value, arrow: v.arrow, execute: v.execute };
        });
      } else if (!r && (face.kind === 'ward' || face.kind === 'mend')) {
        hint.previews = [{ enemyId: null, raw: C.supportValue(state, tables, face, { slotBonus: s.bonus }), mulPermille: 1000, value: C.supportValue(state, tables, face, { slotBonus: s.bonus }), arrow: 'eq' }];
      }
      out[dieId][s.slotId] = hint;
    }
    out[dieId].__discardCost = p.dice[dieId].isCorrupt ? Math.floor((selfCorruptionValue(face) + 1) / 2) : 0;
  }
  return out;
}

export function configSchema(tables) {
  return {
    difficulties: Object.entries(tables.difficulty.levels).map(([id, lv]) => ({ id, ...lv })),
    difficultyKeys: tables.difficulty.keys,
    toggles: [
      { id: 'mapBranching', default: true, label: '地图分支', off: '每关单一线性节点，自动推进', on: '逐层分支选择，展示路径与预览' },
      { id: 'relics', default: true, label: '遗物系统', off: '无遗物系统，相关 UI 隐藏', on: '遗物栏、被动加成，可在商店/精英/事件获得' },
      { id: 'events', default: true, label: '随机事件', off: '事件节点改为宝箱', on: '随机叙事事件（正面/负面/赌博）' },
      { id: 'shops', default: true, label: '商店与锻造', off: '商店/锻造节点改为宝箱', on: '商店买骰/遗物/删骰；锻造改造骰面' },
      { id: 'schoolCounter', default: true, label: '学派克制', off: '克制倍率恒为 ×1.00', on: '锋刃 → 自然 → 秘法 → 锋刃 克制方阵生效' }
    ],
    characters: Object.entries(tables.characters).map(([id, ch]) => ({ id, name: ch.name, desc: ch.desc })),
    enums: {
      schools: SCHOOLS.concat(['neutral']), faceKinds: ['purge', 'ward', 'mend', 'status', 'selfCorruption', 'draw', 'reroll', 'modify', 'gold', 'nothing', 'duplicate_face'],
      slotKinds: ['attack', 'defense', 'heal', 'utility', 'cursed'], riskTiers: ['safe', 'balanced', 'risky', 'cursed'],
      statusKinds: ['poison', 'burn', 'weak', 'strength', 'regen'], actionKinds: ACTION_KINDS.slice(), phases: ['map', 'player_turn', 'enemy_turn', 'shop', 'event', 'treasure', 'shrine', 'forge', 'rest', 'relic_swap', 'settle']
    },
    schools: tables.schools.schools,
    defaults: { difficulty: 'medium', character: 'warrior', toggles: { mapBranching: true, relics: true, events: true, shops: true, schoolCounter: true } }
  };
}

