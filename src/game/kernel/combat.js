// kernel/combat.js — 战斗：骰面结算、伤害顺序、状态、敌人意图与回合推进（GAME-00 §4.2/§5.4，GAME-01 §4）

import { randint, shuffle } from './rng.js';
import { schoolMulPermille, applyMul, arrowOf } from './counter.js';
import { checkConstraint } from './slots.js';
import { faceHasSelfCorruption, selfCorruptionValue } from './risk.js';
import {
  emptyStatuses, pad, dieDef, currentFace, refreshCorrupt, deckSchoolInfo, isMono, addDieToDeck
} from './state.js';
import * as R from './relic.js';

const MAX_CHARGE = 5;

// ======================================================================
// 敌人
// ======================================================================
function enemySlotsFor(def, phase) {
  if (def.phases) return def.phases[phase - 1].slots;
  return def.slots;
}

function enemySchoolFor(def, phase) {
  if (def.phases) return def.phases[phase - 1].school;
  return def.school;
}

export function spawnEnemies(ctx, memberIds) {
  const { state, tables } = ctx;
  const c = state.config;
  return memberIds.map((defId, i) => {
    const def = tables.monsters[defId];
    let hp = applyMul(def.maxCorruption, c.enemyHpMulPermille);
    if (def.tier === 'boss') hp = applyMul(hp, 1000 + c.bossPhaseHpBonusPermille);
    hp = Math.max(1, hp);
    return {
      enemyId: `e-${pad(i, 2)}`, defId, name: def.name, tier: def.tier,
      hp, hpMax: hp, ward: 0, school: enemySchoolFor(def, 1), phase: 1,
      statuses: emptyStatuses(), slots: enemySlotsFor(def, 1).map((s) => ({ ...s })),
      intent: [], passives: def.passives.slice(), dead: false, taunt: def.taunt
    };
  });
}

export function aliveEnemies(state) {
  return state.enemies.filter((e) => !e.dead).sort((a, b) => (a.enemyId < b.enemyId ? -1 : 1));
}

function hasPassive(enemy, kind) {
  return enemy.passives.find((p) => p.kind === kind) || null;
}

function slotOrder(enemy, ai) {
  const idx = enemy.slots.map((s, i) => ({ s, i }));
  const pri = (s) => {
    if (ai === 'greedy_damage') return s.effect.kind === 'purge' ? 0 : 1;
    if (ai === 'greedy_block') return s.effect.kind === 'ward' ? 0 : 1;
    return 0;
  };
  idx.sort((a, b) => pri(a.s) - pri(b.s) || (ai === 'greedy_damage' && pri(a.s) === 0 ? b.s.effect.value - a.s.effect.value : 0) || a.i - b.i);
  return idx.map((x) => x.s);
}

/** 由意图骰分配敌槽（确定性贪心） */
export function planIntent(enemy, ai, pips) {
  const order = slotOrder(enemy, ai);
  const sorted = pips.map((p, i) => ({ p, i })).sort((a, b) => b.p - a.p || a.i - b.i);
  const used = new Array(sorted.length).fill(false);
  const intent = [];
  const take = (slot) => {
    for (let k = 0; k < sorted.length; k++) {
      if (!used[k] && checkConstraint(slot.constraint, sorted[k].p)) { used[k] = true; return sorted[k].p; }
    }
    return 0;
  };
  for (const slot of order) {
    const pip = take(slot);
    if (pip) intent.push({ slotId: slot.slotId, pip, ...slot.effect });
  }
  // 额外意图骰（难度 enemyExtraDice）只提高满足槽约束的概率，不重复触发槽位（决策 D-13）
  intent.sort((a, b) => (a.slotId < b.slotId ? -1 : a.slotId > b.slotId ? 1 : 0));
  return intent.map((it, k) => ({ ...it, seq: k, hits: it.hits || 1, committed: true }));
}

export function commitIntentFor(ctx, enemy) {
  const { state, tables } = ctx;
  const def = tables.monsters[enemy.defId];
  const n = def.diceCount + state.config.enemyExtraDice;
  const pips = [];
  for (let i = 0; i < n; i++) pips.push(randint(state, 'dice.enemy', 1, def.diceSides));
  enemy.intent = planIntent(enemy, def.ai, pips);
}

export function commitIntents(ctx) {
  for (const e of aliveEnemies(ctx.state)) commitIntentFor(ctx, e);
  ctx.events.push({ type: 'intent_committed', enemyIds: aliveEnemies(ctx.state).map((e) => e.enemyId) });
}

/** 敌人攻击对玩家的最终值（raw → 难度倍率 → 学派 → 单系惩罚，逐步 floor，最小 1） */
export function enemyAttackValue(state, tables, enemy, value) {
  const raw = Math.max(0, value + enemy.statuses.strength - enemy.statuses.weak);
  const deck = deckSchoolInfo(state, tables).school;
  const mul = schoolMulPermille(state.config, enemy.school, deck);
  let v = applyMul(raw, state.config.enemyDmgMulPermille);
  v = applyMul(v, mul);
  let mono = 0;
  if (isMono(state, tables) && !R.monoPenaltyDisabled(state) && state.config.monoPenaltyPermille > 0) {
    mono = state.config.monoPenaltyPermille;
    v = applyMul(v, 1000 + mono);
  }
  return { raw, mulPermille: mul, monoPermille: mono, value: Math.max(1, v), arrow: arrowOf(mul) };
}

// ======================================================================
// 玩家伤害 / 效果
// ======================================================================
/** 玩家攻击面的最终伤害（GAME-01 §9.2：先学派后遗物，各自 floor，攻击向最小 1） */
export function playerAttackValue(state, tables, face, opts, enemy) {
  const p = state.player;
  const school = opts.school;
  let raw = face.value + (opts.slotBonus || 0) + (opts.valueBonus || 0) + p.statuses.strength - p.statuses.weak;
  if (school === 'blade') raw += p.charge;
  raw += R.attackFlatBonus(state, tables, school);
  let execute = false;
  if ((face.tags || []).includes('execute') && enemy.hp * 2 <= enemy.hpMax) { raw *= 2; execute = true; }
  raw = Math.max(0, raw);
  const mul = schoolMulPermille(state.config, school, enemy.school, R.counterBonusPermille(state, tables));
  const step1 = applyMul(raw, mul);
  const relicMul = R.relicMulPermille(state, tables, enemy);
  const final = Math.max(1, applyMul(step1, relicMul));
  return { raw, mulPermille: mul, relicMulPermille: relicMul, value: final, arrow: arrowOf(mul), execute };
}

export function supportValue(state, tables, face, opts) {
  return Math.max(0, face.value + (opts.slotBonus || 0) + (opts.valueBonus || 0) + R.deckThinBonus(state, tables));
}

function battle(state) { return state.node && state.node.battle; }
function battleOver(state) { return !battle(state) || !!battle(state).result; }

/** 失心流程（GAME-01 §4.3） */
function checkPlayerDeath(ctx) {
  const { state, events } = ctx;
  const p = state.player;
  if (p.virtue > 0 || battleOver(state)) return;
  if (state.config.reviveOnce && !p.reviveUsed) {
    p.reviveUsed = true;
    p.virtue = Math.floor(p.virtueMax / 2);
    events.push({ type: 'revive', virtue: p.virtue });
    return;
  }
  p.hearts -= 1 + R.extraHeartLoss(state);
  if (p.hearts < 0) p.hearts = 0;
  events.push({ type: 'player_lost_heart', heartsLeft: p.hearts });
  battle(state).result = p.hearts > 0 ? 'lost' : 'dead';
}

export function applySelfCorruption(ctx, value, source) {
  const p = ctx.state.player;
  p.virtue -= value;
  ctx.events.push({ type: 'self_corruption', target: 'player', value, source });
  checkPlayerDeath(ctx);
}

function damagePlayer(ctx, dmg, info) {
  const p = ctx.state.player;
  const absorbed = Math.min(p.ward, dmg);
  p.ward -= absorbed;
  if (absorbed > 0) ctx.events.push({ type: 'ward_absorb', target: 'player', value: absorbed });
  p.virtue -= dmg - absorbed;
  ctx.events.push({ type: 'damage', target: 'player', ...info, value: dmg, dealt: dmg - absorbed });
  checkPlayerDeath(ctx);
}

function checkBossPhase(ctx, enemy) {
  const def = ctx.tables.monsters[enemy.defId];
  if (!def.phases || enemy.dead) return;
  let changed = false;
  while (enemy.phase < def.phases.length && enemy.hp * 1000 <= def.phases[enemy.phase - 1].endsAtPermille * enemy.hpMax) {
    enemy.phase += 1;
    changed = true;
  }
  if (changed) {
    enemy.school = enemySchoolFor(def, enemy.phase);
    enemy.slots = enemySlotsFor(def, enemy.phase).map((s) => ({ ...s }));
    ctx.events.push({ type: 'boss_phase', enemyId: enemy.enemyId, phase: enemy.phase, school: enemy.school });
    commitIntentFor(ctx, enemy); // 阶段切换：意图刷新并公开（T-11）
  }
}

function onEnemyKilled(ctx, enemy) {
  const { state, tables, events } = ctx;
  enemy.dead = true;
  enemy.intent = [];
  state.stats.kills += 1;
  events.push({ type: 'enemy_died', enemyId: enemy.enemyId, name: enemy.name });
  const echo = R.bladeEchoValue(state, tables);
  if (echo && state.phase === 'player_turn') {
    const target = state.player.hand.find((id) => dieDef(tables, state.player.dice[id].defId).school === 'blade');
    if (target) {
      const die = state.player.dice[target];
      die.faceIndex = Math.min(5, die.faceIndex + echo);
      refreshCorrupt(state, tables, target);
      events.push({ type: 'die_modified', dieId: target, pip: die.faceIndex + 1, source: 'relic_blade_echo' });
    }
  }
  if (!aliveEnemies(state).length) battle(state).result = 'won';
}

export function damageEnemy(ctx, enemy, dmg, info, fromPlayerAttack) {
  const absorbed = Math.min(enemy.ward, dmg);
  enemy.ward -= absorbed;
  if (absorbed > 0) ctx.events.push({ type: 'ward_absorb', target: enemy.enemyId, value: absorbed });
  enemy.hp -= dmg - absorbed;
  ctx.state.stats.damageDealt += dmg - absorbed;
  ctx.events.push({ type: 'damage', target: enemy.enemyId, ...info, value: dmg, dealt: dmg - absorbed });
  if (enemy.hp <= 0) {
    enemy.hp = 0;
    onEnemyKilled(ctx, enemy);
    return;
  }
  checkBossPhase(ctx, enemy);
  const thorns = hasPassive(enemy, 'thorns');
  if (fromPlayerAttack && thorns) applySelfCorruption(ctx, thorns.value, `thorns:${enemy.enemyId}`);
}

function addStatus(ctx, targetId, statusKind, stacks) {
  const { state, events } = ctx;
  if (targetId === 'player') {
    state.player.statuses[statusKind] += stacks;
  } else {
    const e = state.enemies.find((x) => x.enemyId === targetId);
    if (!e || e.dead) return;
    if (statusKind === 'poison' && hasPassive(e, 'poison_immune')) {
      events.push({ type: 'status_immune', target: targetId, statusKind });
      return;
    }
    e.statuses[statusKind] += stacks;
  }
  events.push({ type: 'status_applied', target: targetId, statusKind, stacks });
}

function healPlayer(ctx, v, source) {
  const p = ctx.state.player;
  const before = p.virtue;
  p.virtue = Math.max(p.virtue, Math.min(p.virtueMax, p.virtue + v));
  ctx.events.push({ type: 'mend', target: 'player', value: p.virtue - before, source });
}

// ---------- 骰子抽/掷 ----------
export function rollDie(ctx, dieId) {
  const { state, tables } = ctx;
  const pip = randint(state, 'dice.player', 1, 6);
  state.player.dice[dieId].faceIndex = pip - 1;
  refreshCorrupt(state, tables, dieId);
  return pip;
}

export function drawDice(ctx, n) {
  const { state } = ctx;
  const p = state.player;
  const drawn = [];
  const pips = [];
  for (let i = 0; i < n; i++) {
    if (!p.drawPile.length) {
      if (!p.discardPile.length) break;
      p.drawPile = shuffle(state, 'dice.player', p.discardPile);
      p.discardPile = [];
      ctx.events.push({ type: 'reshuffle', count: p.drawPile.length });
    }
    const id = p.drawPile.shift();
    if (p.hand.length >= state.config.handMax) {
      p.discardPile.push(id);
      ctx.events.push({ type: 'hand_overflow', dieId: id });
      continue;
    }
    p.hand.push(id);
    drawn.push(id);
    pips.push(rollDie(ctx, id));
  }
  if (drawn.length) ctx.events.push({ type: 'roll', dieIds: drawn, pips });
}

// ======================================================================
// 战斗开始 / 回合
// ======================================================================
export function startBattle(ctx, memberIds, kind) {
  const { state, tables, events } = ctx;
  const p = state.player;
  state.enemies = spawnEnemies(ctx, memberIds);
  state.node.battle = { kind, result: null, groupMembers: memberIds.slice() };
  state.turn = 1;
  p.virtue = Math.max(1, p.virtueMax - R.battleStartVirtueLoss(state, tables) + p.pendingVirtue);
  p.pendingVirtue = 0;
  p.ward = 0;
  p.statuses = emptyStatuses();
  for (const k of Object.keys(p.pendingStatuses)) p.statuses[k] += p.pendingStatuses[k];
  p.pendingStatuses = {};
  p.charge = 0;
  p.reviveUsed = false;
  p.purifyLeft = state.config.purifyPerStage + R.purifyBonus(state, tables);
  const ch = tables.characters[state.config.character];
  p.abilities = ch.abilities.filter((a) => a.usesPerBattle !== null).map((a) => ({ id: a.id, usesLeft: a.usesPerBattle }));
  p.hand = [];
  p.discardPile = [];
  p.drawPile = shuffle(state, 'dice.player', p.deck);
  for (const id of p.deck) p.dice[id].isCorrupt = false;
  for (const s of p.slots) { s.filledBy = null; s.disabledUntilTurn = null; }
  p.arcanaLastTurn = 0;
  p.arcanaThisTurn = 0;
  p.drainNext = 0;
  state.phase = 'player_turn';
  events.push({ type: 'battle_start', kind, enemies: state.enemies.map((e) => ({ enemyId: e.enemyId, name: e.name, school: e.school })) });
  const ward = R.battleStartWard(state, tables);
  if (ward) { p.ward += ward; events.push({ type: 'ward_gain', target: 'player', value: ward, source: 'relic_iron_bark' }); }
  startPlayerTurn(ctx, true);
}

function startPlayerTurn(ctx, first) {
  const { state, tables } = ctx;
  const p = state.player;
  for (const s of p.slots) s.filledBy = null;
  p.firstPlayDone = false;
  p.bladeAttackedThisTurn = false;
  if (!first) { p.arcanaLastTurn = p.arcanaThisTurn; }
  p.arcanaThisTurn = 0;
  let n = state.config.drawPerTurn + R.drawDelta(state, tables) - p.drainNext;
  if (first) { n += p.pendingDraw; p.pendingDraw = 0; }
  p.drainNext = 0;
  drawDice(ctx, Math.max(1, n));
  commitIntents(ctx);
  ctx.events.push({ type: 'turn_start', turn: state.turn });
}

/** 玩家 upkeep（E8）：光盾清零；中毒/灼烧非致死至少留 1；再生；虚弱减半（决策 D-12） */
function playerUpkeep(ctx) {
  const p = ctx.state.player;
  p.ward = 0;
  const st = p.statuses;
  if (st.poison > 0) {
    const d = Math.min(st.poison, Math.max(0, p.virtue - 1));
    p.virtue -= d;
    ctx.events.push({ type: 'status_tick', target: 'player', statusKind: 'poison', value: d });
    st.poison -= 1;
  }
  if (st.burn > 0) {
    const d = Math.min(st.burn, Math.max(0, p.virtue - 1));
    p.virtue -= d;
    ctx.events.push({ type: 'status_tick', target: 'player', statusKind: 'burn', value: d });
    st.burn = Math.floor(st.burn / 2);
  }
  if (st.regen > 0) {
    healPlayer(ctx, st.regen, 'regen');
    st.regen -= 1;
  }
  if (st.weak > 0) st.weak = Math.floor(st.weak / 2);
}

/** 敌人回合开始：光盾清零、中毒/灼烧结算（可致死）、被动 */
function enemyTurnStart(ctx, e) {
  e.ward = 0;
  const st = e.statuses;
  if (st.poison > 0) {
    const d = st.poison;
    st.poison -= 1;
    ctx.events.push({ type: 'status_tick', target: e.enemyId, statusKind: 'poison', value: d });
    damageEnemy(ctx, e, d, { source: 'poison', raw: d, mulPermille: 1000 }, false);
  }
  if (!e.dead && st.burn > 0) {
    const d = st.burn;
    st.burn = Math.floor(st.burn / 2);
    ctx.events.push({ type: 'status_tick', target: e.enemyId, statusKind: 'burn', value: d });
    damageEnemy(ctx, e, d, { source: 'burn', raw: d, mulPermille: 1000 }, false);
  }
  if (e.dead) return;
  if (st.weak > 0) st.weak = Math.floor(st.weak / 2);
  const regen = hasPassive(e, 'regen');
  if (regen) {
    const before = e.hp;
    e.hp = Math.min(e.hpMax, e.hp + regen.value);
    ctx.events.push({ type: 'mend', target: e.enemyId, value: e.hp - before, source: 'passive' });
  }
  const ramp = hasPassive(e, 'ramp_strength');
  if (ramp) addStatus(ctx, e.enemyId, 'strength', ramp.value);
}

function resolveEnemyIntent(ctx, e, it) {
  const { state, tables, events } = ctx;
  const p = state.player;
  switch (it.kind) {
    case 'purge':
      for (let h = 0; h < it.hits && !battleOver(state); h++) {
        const r = enemyAttackValue(state, tables, e, it.value);
        damagePlayer(ctx, r.value, { source: e.enemyId, raw: r.raw, mulPermille: r.mulPermille, school: e.school, monoPermille: r.monoPermille });
      }
      break;
    case 'ward':
      e.ward += it.value;
      events.push({ type: 'ward_gain', target: e.enemyId, value: it.value });
      break;
    case 'status':
      addStatus(ctx, it.target === 'self' ? e.enemyId : 'player', it.statusKind, it.value);
      break;
    case 'heal': {
      const before = e.hp;
      e.hp = Math.min(e.hpMax, e.hp + it.value);
      events.push({ type: 'mend', target: e.enemyId, value: e.hp - before });
      break;
    }
    case 'add_curse':
      for (let i = 0; i < it.value; i++) {
        const id = addDieToDeck(state, it.dieId, true); // 敌人塞入的厄咒骰仅本场有效（决策 D-14）
        events.push({ type: 'curse_added', dieId: id, defId: it.dieId });
      }
      break;
    case 'disable_slot': {
      const open = p.slots.filter((s) => !(s.disabledUntilTurn !== null && state.turn < s.disabledUntilTurn));
      if (open.length) {
        const s = open[it.pip % open.length];
        s.disabledUntilTurn = state.turn + 1 + it.value;
        events.push({ type: 'slot_disabled', slotId: s.slotId, untilTurn: s.disabledUntilTurn });
      }
      break;
    }
    case 'drain_hand':
      p.drainNext += it.value;
      events.push({ type: 'drain', value: it.value });
      break;
    default:
      break;
  }
}

/** end_turn：E1..E13（GAME-01 §4.2） */
export function endTurn(ctx) {
  const { state, events } = ctx;
  const p = state.player;
  // E2
  p.discardPile.push(...p.hand);
  for (const id of p.hand) p.dice[id].isCorrupt = false;
  p.hand = [];
  if (!p.bladeAttackedThisTurn) p.charge = Math.floor(p.charge / 2);
  events.push({ type: 'turn_end', turn: state.turn });
  // E3/E4
  state.phase = 'enemy_turn';
  for (const e of aliveEnemies(state)) {
    if (battleOver(state)) break;
    enemyTurnStart(ctx, e);
    if (e.dead || battleOver(state)) continue;
    for (const it of e.intent) {
      if (battleOver(state)) break;
      resolveEnemyIntent(ctx, e, it);
    }
    e.intent = [];
  }
  if (battleOver(state)) { state.phase = 'player_turn'; return; }
  // E7
  state.turn += 1;
  state.stats.turns += 1;
  state.phase = 'player_turn';
  if (state.turn > state.config.maxTurnsPerBattle) {
    state.truncationReason = 'E_KERNEL_TURN_LIMIT';
    battle(state).result = 'truncated';
    return;
  }
  // E8/E9
  playerUpkeep(ctx);
  checkPlayerDeath(ctx);
  if (battleOver(state)) return;
  // E10–E12
  startPlayerTurn(ctx, false);
}

// ======================================================================
// 玩家动作
// ======================================================================
export function faceNeedsTarget(face) {
  if (face.kind === 'purge' || (face.kind === 'status' && face.target === 'enemy')) return 'enemy';
  if (face.kind === 'reroll' || face.kind === 'modify' || face.kind === 'duplicate_face') return 'die';
  return null;
}

function applyPrimary(ctx, face, opts) {
  const { state, tables, events } = ctx;
  const p = state.player;
  switch (face.kind) {
    case 'purge': {
      const enemy = state.enemies.find((e) => e.enemyId === opts.targetId && !e.dead) || aliveEnemies(state)[0];
      if (!enemy) return;
      const r = playerAttackValue(state, tables, face, opts, enemy);
      if (r.mulPermille > 1000) state.stats.counterHits += 1;
      damageEnemy(ctx, enemy, r.value, {
        source: 'play_face', raw: r.raw, mulPermille: r.mulPermille, relicMulPermille: r.relicMulPermille,
        school: opts.school, label: face.label, dieId: opts.dieId, slotId: opts.slotId, execute: r.execute
      }, true);
      if (opts.school === 'blade') {
        p.bladeAttackedThisTurn = true;
        p.charge = Math.min(MAX_CHARGE, p.charge + 1);
      }
      break;
    }
    case 'ward': {
      const v = supportValue(state, tables, face, opts);
      p.ward += v;
      events.push({ type: 'ward_gain', target: 'player', value: v });
      break;
    }
    case 'mend':
      healPlayer(ctx, supportValue(state, tables, face, opts), 'play_face');
      break;
    case 'status': {
      const target = face.target === 'enemy'
        ? ((state.enemies.find((e) => e.enemyId === opts.targetId && !e.dead) || aliveEnemies(state)[0] || {}).enemyId)
        : 'player';
      if (target) addStatus(ctx, target, face.statusKind, face.value);
      break;
    }
    case 'draw':
      drawDice(ctx, face.value);
      break;
    case 'reroll':
      if (opts.targetId && p.hand.includes(opts.targetId)) {
        const pip = rollDie(ctx, opts.targetId);
        events.push({ type: 'roll', dieIds: [opts.targetId], pips: [pip] });
      }
      break;
    case 'modify':
      if (opts.targetId && p.hand.includes(opts.targetId)) {
        const die = p.dice[opts.targetId];
        die.faceIndex = Math.min(5, die.faceIndex + face.value);
        refreshCorrupt(state, tables, opts.targetId);
        events.push({ type: 'die_modified', dieId: opts.targetId, pip: die.faceIndex + 1 });
      }
      break;
    case 'duplicate_face':
      if (opts.targetId && p.hand.includes(opts.targetId)) {
        const tf = currentFace(state, tables, opts.targetId);
        events.push({ type: 'duplicate', dieId: opts.targetId, label: tf.label });
        if (tf.kind !== 'duplicate_face' && tf.kind !== 'selfCorruption') {
          applyPrimary(ctx, tf, { school: tf.school, slotBonus: 0, valueBonus: 0, targetId: null, dieId: opts.targetId });
        }
      }
      break;
    case 'gold': {
      const g = applyMul(face.value, R.goldMul(state, tables));
      p.starShards += g;
      events.push({ type: 'gold', value: g });
      break;
    }
    default:
      break;
  }
}

/** play_face：负面先结算（M6），再正向分量 */
export function playFace(ctx, action) {
  const { state, tables, events } = ctx;
  const p = state.player;
  const { dieId, slotId, targetId } = action;
  const face = currentFace(state, tables, dieId);
  const slot = p.slots.find((s) => s.slotId === slotId);
  const def = dieDef(tables, p.dice[dieId].defId);
  p.hand = p.hand.filter((d) => d !== dieId);
  p.discardPile.push(dieId);
  p.dice[dieId].isCorrupt = false;
  if (!slot.reusable) slot.filledBy = dieId;
  let school = face.school;
  let valueBonus = 0;
  if (R.swiftHandActive(state)) {
    const ds = deckSchoolInfo(state, tables).school;
    if (ds !== 'neutral') school = ds;
    valueBonus += 1;
  }
  p.firstPlayDone = true;
  events.push({ type: 'play_face', dieId, slotId, label: face.label, kind: face.kind, school });
  if (def.category === 'skill') state.stats.skillFaceUses += 1;
  if (school === 'arcana') p.arcanaThisTurn += 1;

  const sc = selfCorruptionValue(face);
  if (sc > 0) {
    applySelfCorruption(ctx, sc, dieId);
    if (battleOver(state)) return;
  }
  applyPrimary(ctx, face, { school, slotBonus: slot.bonus, valueBonus, targetId, dieId, slotId });
  if (!battleOver(state) && school === 'verdant') {
    const m = R.verdantMend(state, tables);
    if (m) healPlayer(ctx, m, 'relic_verdant_root');
  }
}

export function discardDie(ctx, dieId) {
  const { state, tables } = ctx;
  const p = state.player;
  const die = p.dice[dieId];
  const face = currentFace(state, tables, dieId);
  const wasCorrupt = die.isCorrupt;
  p.hand = p.hand.filter((d) => d !== dieId);
  p.discardPile.push(dieId);
  die.isCorrupt = false;
  ctx.events.push({ type: 'discard', dieId, corrupt: wasCorrupt });
  if (wasCorrupt) {
    const v = selfCorruptionValue(face);
    applySelfCorruption(ctx, Math.floor((v + 1) / 2), `discard:${dieId}`);
  }
}

export function purifyDie(ctx, dieId) {
  const p = ctx.state.player;
  p.purifyLeft -= 1;
  p.hand = p.hand.filter((d) => d !== dieId);
  p.discardPile.push(dieId);
  p.dice[dieId].isCorrupt = false;
  ctx.events.push({ type: 'purify', dieId, purifyLeft: p.purifyLeft });
}

export function useAbility(ctx, action) {
  const { state, tables, events } = ctx;
  const p = state.player;
  const ab = p.abilities.find((a) => a.id === action.abilityId);
  ab.usesLeft -= 1;
  if (action.abilityId === 'warrior_reroll') {
    const pips = p.hand.map((id) => rollDie(ctx, id));
    events.push({ type: 'ability', abilityId: action.abilityId });
    events.push({ type: 'roll', dieIds: p.hand.slice(), pips });
  } else if (action.abilityId === 'warrior_focus') {
    const die = p.dice[action.dieId];
    die.faceIndex = action.value - 1;
    refreshCorrupt(state, tables, action.dieId);
    events.push({ type: 'ability', abilityId: action.abilityId });
    events.push({ type: 'die_modified', dieId: action.dieId, pip: action.value });
  }
}

// ======================================================================
// 合法性（战斗内）
// ======================================================================
export function slotDisabled(state, slot) {
  return slot.disabledUntilTurn !== null && state.turn < slot.disabledUntilTurn;
}

/** 返回 null 表示合法，否则 {code, reason} */
export function checkPlayFace(state, tables, a) {
  const p = state.player;
  if (!p.hand.includes(a.dieId)) return { code: 'E_ACTION_DIE_NOT_IN_HAND', reason: '骰子不在手中' };
  const die = p.dice[a.dieId];
  if (a.faceIndex != null && a.faceIndex !== die.faceIndex) return { code: 'E_ACTION_FACE_MISMATCH', reason: '骰面不匹配' };
  const slot = p.slots.find((s) => s.slotId === a.slotId);
  if (!slot) return { code: 'E_ACTION_UNKNOWN_SLOT', reason: '未知技能槽' };
  if (slotDisabled(state, slot)) return { code: 'E_ACTION_SLOT_DISABLED', reason: '技能槽被禁用' };
  if (slot.filledBy && !slot.reusable) return { code: 'E_ACTION_SLOT_OCCUPIED', reason: '技能槽已被占用' };
  const face = currentFace(state, tables, a.dieId);
  const accepts = { attack: ['purge'], defense: ['ward'], heal: ['mend'], utility: null, cursed: ['selfCorruption'] }[slot.slotKind];
  if (accepts !== null && (!accepts || !accepts.includes(face.kind))) {
    return { code: 'E_ACTION_KIND_MISMATCH', reason: `该槽不接受「${face.label}」` };
  }
  if (!checkConstraint(slot.constraint, die.faceIndex + 1)) return { code: 'E_ACTION_CONSTRAINT_VIOLATION', reason: slot.constraint };
  const need = faceNeedsTarget(face);
  if (need === 'enemy') {
    const t = state.enemies.find((e) => e.enemyId === a.targetId);
    if (!t || t.dead) return { code: 'E_ACTION_BAD_TARGET', reason: '目标无效' };
  } else if (need === 'die') {
    const others = p.hand.filter((d) => d !== a.dieId);
    if (others.length) {
      if (!others.includes(a.targetId)) return { code: 'E_ACTION_BAD_TARGET', reason: '需要选择另一颗手骰' };
    } else if (a.targetId != null) return { code: 'E_ACTION_BAD_TARGET', reason: '无可选目标' };
  } else if (a.targetId != null) {
    return { code: 'E_ACTION_BAD_TARGET', reason: '该骰面不需要目标' };
  }
  return null;
}

export function corruptRemaining(state) {
  return state.player.hand.filter((id) => state.player.dice[id].isCorrupt);
}

export { faceHasSelfCorruption };
