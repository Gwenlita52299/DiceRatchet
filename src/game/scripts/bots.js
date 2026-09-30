// scripts/bots.js — 启发式 bot（GAME-02 §8）：random / greedy_damage / greedy_counter / school_blade / school_arcana / school_verdant
// 只通过内核公开 API（legalActions / playHints / observe）决策，可复用为 MOD-08 的 heuristic 基线。

import * as K from '../kernel/index.js';

export const STRATEGIES = ['random', 'greedy_damage', 'greedy_counter', 'school_blade', 'school_arcana', 'school_verdant'];

// 简易确定性 PRNG（bot 自身决策用，与内核 RNG 无关）
function botRng(seed) {
  let x = (seed * 2654435761) >>> 0 || 1;
  return () => { x ^= x << 13; x >>>= 0; x ^= x >>> 17; x ^= x << 5; x >>>= 0; return x / 4294967296; };
}

const BEATS = { blade: 'verdant', arcana: 'blade', verdant: 'arcana' };
const counterOf = (s) => Object.keys(BEATS).find((k) => BEATS[k] === s) || null;

function prefSchool(strategy) {
  return strategy.startsWith('school_') ? strategy.slice(7) : null;
}

function incomingDamage(obs) {
  let d = 0;
  for (const e of obs.enemies) for (const it of e.intent) if (it.kind === 'purge') d += it.value * (it.hits || 1);
  return d;
}

function scorePlay(obs, hints, a, strategy) {
  const die = obs.hand.find((d) => d.dieId === a.dieId);
  const hint = hints[a.dieId][a.slotId];
  const pref = prefSchool(strategy);
  const p = obs.player;
  let s = 0;
  const sc = die.corruptionValue || 0;
  switch (die.kind) {
    case 'purge': {
      const pv = hint.previews && (hint.previews.find((x) => x.enemyId === a.targetId) || hint.previews[0]);
      const enemy = obs.enemies.find((e) => e.enemyId === a.targetId);
      s = pv ? Math.min(pv.value, enemy.hp + enemy.ward) * 3 : 3;
      if (enemy && pv && pv.value >= enemy.hp + enemy.ward) s += 8; // 击杀奖励
      if (strategy === 'greedy_counter' && pv && pv.arrow === 'up') s += 6;
      // 优先打低血/高威胁目标
      if (enemy) s += Math.max(0, 10 - enemy.hp) * 0.3;
      break;
    }
    case 'ward': {
      const need = Math.max(0, incomingDamage(obs) - p.ward);
      const v = hint.previews ? hint.previews[0].value : die.value;
      s = Math.min(v, need) * 2.5;
      break;
    }
    case 'mend': {
      const v = hint.previews ? hint.previews[0].value : die.value;
      s = Math.min(v, p.virtueMax - p.virtue) * 2.2;
      break;
    }
    case 'status': {
      const w = { poison: 2.5, burn: 2, weak: 2, strength: 4, regen: 2.5 }[die.statusKind] || 1;
      s = die.value * w;
      if (die.statusKind === 'weak' && !obs.enemies.some((e) => e.intent.some((i) => i.kind === 'purge'))) s *= 0.3;
      break;
    }
    case 'draw': s = 7 * die.value; break;
    case 'reroll': s = obs.hand.find((d) => d.dieId === a.targetId && d.isCorrupt) ? 9 : 1.5; break;
    case 'modify': s = obs.hand.find((d) => d.dieId === a.targetId && d.pip < 6) ? 2 : 0; break;
    case 'duplicate_face': s = 5; break;
    case 'gold': s = 1.5; break;
    case 'selfCorruption': s = -sc * 3; break;
    default: s = 0;
  }
  if (sc > 0 && die.kind !== 'selfCorruption') s -= sc * 2.5;
  if (pref && die.school === pref) s *= 1.4;
  // 工具槽可重复使用，留给其他面：非工具面尽量走专用槽
  const slot = obs.slots.find((x) => x.slotId === a.slotId);
  if (slot.slotKind === 'utility' && ['purge', 'ward', 'mend'].includes(die.kind)) s -= 1.5;
  s += slot.bonus * 0.5;
  return s;
}

function battleAction(state, tables, strategy, rnd) {
  const legal = K.legalActions(state, tables);
  if (strategy === 'random') {
    const pool = legal.filter((a) => a.kind !== 'abandon_run');
    const nonEnd = pool.filter((a) => a.kind !== 'end_turn');
    if (!nonEnd.length || rnd() < 0.15) return pool.find((a) => a.kind === 'end_turn') || pool[0];
    return nonEnd[Math.floor(rnd() * nonEnd.length)];
  }
  const obs = K.observe(state, 'full', tables);
  const hints = K.playHints(state, tables);
  const plays = legal.filter((a) => a.kind === 'play_face');
  let best = null;
  let bestS = 0.5;
  for (const a of plays) {
    const s = scorePlay(obs, hints, a, strategy);
    if (s > bestS) { best = a; bestS = s; }
  }
  if (best) return best;
  // 处理魔化骰
  const corrupt = obs.hand.filter((d) => d.isCorrupt);
  if (corrupt.length) {
    const d = corrupt.slice().sort((x, y) => y.corruptionValue - x.corruptionValue)[0];
    if (legal.find((a) => a.kind === 'purify' && a.dieId === d.dieId)) return { kind: 'purify', dieId: d.dieId };
    const reroll = legal.find((a) => a.kind === 'use_ability' && a.abilityId === 'warrior_reroll');
    if (reroll && corrupt.length >= 2) return reroll;
    return { kind: 'discard_die', dieId: d.dieId };
  }
  return { kind: 'end_turn' };
}

function nodeScore(obs, node, strategy) {
  const p = obs.player;
  const lowHp = p.hearts <= 1;
  const k = node.kind;
  let s = { battle: 5, elite: lowHp ? -5 : 3, boss: 0, treasure: 7, shop: p.starShards >= 50 ? 8 : 2, forge: p.starShards >= 40 ? 5 : 1, event: 4, shrine: 6, rest: p.hearts < p.heartsMax ? 9 : 3 }[k] || 0;
  if (strategy === 'greedy_counter' && node.preview && node.preview.arrow === 'down') s += 2;
  if (node.preview && node.preview.arrow === 'up') s -= 1;
  return s;
}

function pickDieDef(offers, strategy, obs) {
  const pref = prefSchool(strategy);
  const score = (d) => {
    let s = 0;
    for (const f of d.faces) {
      if (f.kind === 'purge') s += f.value;
      else if (f.kind === 'ward' || f.kind === 'mend') s += f.value * 0.7;
      else if (f.kind === 'status') s += f.value * 0.9;
      else if (f.kind === 'draw') s += 3;
      s -= (f.selfCorruption || 0) * 1.2;
    }
    if (pref && d.school === pref) s += 12;
    if (strategy === 'greedy_counter' && d.counter) s += 8;
    if (!pref && obs && d.school === obs.player.deckSchool) s += 2;
    return s;
  };
  return offers.slice().sort((a, b) => score(b) - score(a))[0];
}

export function chooseAction(state, tables, strategy, rnd) {
  const legal = K.legalActions(state, tables);
  const obs = () => K.observe(state, 'full', tables);
  switch (state.phase) {
    case 'player_turn': return battleAction(state, tables, strategy, rnd);
    case 'map': {
      if (strategy === 'random') return legal.filter((a) => a.kind === 'map_choose')[Math.floor(rnd() * legal.filter((a) => a.kind === 'map_choose').length)];
      const o = obs();
      const nodes = o.map.rows.flat().filter((n) => n && n.reachable);
      nodes.sort((a, b) => nodeScore(o, b, strategy) - nodeScore(o, a, strategy));
      return { kind: 'map_choose', nodeId: nodes[0].nodeId };
    }
    case 'treasure': {
      const o = obs();
      if (strategy === 'random') return { kind: 'treasure_pick', defId: o.treasure.offers[Math.floor(rnd() * o.treasure.offers.length)].defId };
      if (o.deck.length >= 20) return { kind: 'treasure_pick', defId: 'skip' }; // 控制骰池膨胀
      return { kind: 'treasure_pick', defId: pickDieDef(o.treasure.offers, strategy, o).defId };
    }
    case 'shop': {
      const o = obs();
      const buyable = o.shop.items.filter((it) => !it.sold && it.affordable);
      const relic = buyable.find((it) => it.kind === 'relic');
      if (relic && strategy !== 'random') return { kind: 'shop_buy', itemId: relic.itemId };
      const dice = buyable.filter((it) => it.kind === 'die');
      if (dice.length && o.deck.length < 14) {
        const d = strategy === 'random' ? dice[0] : pickDieDef(dice.map((x) => ({ ...x, counter: x.school === o.shop.counterSchool })), strategy, o);
        return { kind: 'shop_buy', itemId: d.itemId };
      }
      if (strategy !== 'random' && o.player.starShards >= o.shop.removePrice + 20) {
        const weak = ['die_utility'];
        for (const w of weak) {
          const d = o.deck.find((x) => x.defId === w && o.shop.removable.includes(x.dieId));
          if (d) return { kind: 'shop_remove', dieId: d.dieId };
        }
      }
      return { kind: 'shop_leave' };
    }
    case 'forge': {
      const o = obs();
      if (!o.forge.done && o.forge.affordable) {
        const cand = o.forge.candidates.slice().sort((a, b) => (b.kind === 'purge' ? b.value : 1) - (a.kind === 'purge' ? a.value : 1))[0];
        const target = o.deck.find((d) => d.defId === 'die_strike' && o.forge.forgeable.includes(d.dieId));
        if (target && cand) return { kind: 'forge_pick', dieId: target.dieId, faceIndex: 0, candidateId: cand.candidateId };
      }
      return { kind: 'shop_leave' };
    }
    case 'event': {
      const o = obs();
      if (o.event.resolved) return { kind: 'shop_leave' };
      const opts = o.event.options.filter((x) => x.affordable);
      const pos = opts.find((x) => x.risk === 'positive');
      if (strategy === 'random') return { kind: 'event_choose', optionId: opts[Math.floor(rnd() * opts.length)].optionId };
      return { kind: 'event_choose', optionId: (pos || opts[opts.length - 1]).optionId };
    }
    case 'shrine': case 'rest': {
      const acts = legal.filter((a) => a.kind === 'shrine_choose' || a.kind === 'rest_choose');
      const pref = ['heart', 'relic', 'vigor', 'gold'];
      acts.sort((a, b) => pref.indexOf(a.optionId) - pref.indexOf(b.optionId));
      return acts[0];
    }
    case 'relic_swap': return { kind: 'relic_swap', relicId: 'skip' };
    default: return legal[0];
  }
}

export function runGame({ difficulty = 'medium', seed = 1, strategy = 'greedy_damage', toggles = {}, maxSteps = 20000, tables, record = false, stopAtStage = null, stageCount = null }) {
  const rnd = botRng(seed + 7);
  let state = K.createGame({ difficulty, seed, toggles, stageCount, runId: `r-bot${seed}` }, tables);
  const actions = [];
  let steps = 0;
  while (state.phase !== 'settle' && steps < maxSteps) {
    if (stopAtStage && state.stage > stopAtStage) break;
    const a = chooseAction(state, tables, strategy, rnd);
    const r = K.reduce(state, a, tables);
    if (r.error) throw new Error(`kernel error ${r.error.code}: ${r.error.message} @ ${JSON.stringify(a)}`);
    if (r.illegal) throw new Error(`bot illegal ${r.code} ${r.reason} @ ${JSON.stringify(a)} phase=${state.phase}`);
    if (record) actions.push(a);
    state = r.state;
    steps += 1;
  }
  return { state, outcome: state.outcome, stage: state.stage, steps, actions, stats: state.stats };
}

/** 单场战斗对拍（验收 A6）：给定骰池与编队，跑一场战斗 */
export function runDuel({ tables, seed, difficulty = 'medium', deck, members, strategy = 'greedy_damage' }) {
  const rnd = botRng(seed + 99);
  let state = K.createGame({ difficulty, seed, toggles: {}, runId: `r-duel${seed}` }, tables);
  state = structuredClone(state);
  state.player.deck = [];
  state.player.dice = {};
  state.dieSeq = 0;
  for (const defId of deck) {
    state.dieSeq += 1;
    const id = `d-${String(state.dieSeq).padStart(4, '0')}`;
    state.player.dice[id] = { defId, faceIndex: 0, isCorrupt: false, faces: null, forged: false };
    state.player.deck.push(id);
  }
  state.player.hearts = 1;
  state = K.debugStartBattle(state, members, tables, 'elite');
  let steps = 0;
  while (state.phase === 'player_turn' && steps < 3000) {
    const a = chooseAction(state, tables, strategy, rnd);
    state = K.reduce(state, a, tables).state;
    steps += 1;
  }
  return state.phase !== 'settle' && state.outcome !== 'lose' ? 'win' : 'lose';
}
