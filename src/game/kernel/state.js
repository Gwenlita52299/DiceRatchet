// kernel/state.js — GameState 辅助函数与不变式断言（GAME-01 §2）

import { STREAM_NAMES } from './rng.js';
import { computeRiskTier, faceHasSelfCorruption } from './risk.js';
import { SCHOOLS } from './counter.js';

export const STATUS_KINDS = Object.freeze(['poison', 'burn', 'weak', 'strength', 'regen']);

export function emptyStatuses() {
  return { poison: 0, burn: 0, weak: 0, strength: 0, regen: 0 };
}

export function statusList(st) {
  return STATUS_KINDS.filter((k) => st[k] > 0).map((k) => ({ kind: k, stacks: st[k] }));
}

export function pad(n, w) {
  return String(n).padStart(w, '0');
}

export function kernelError(code, message) {
  const e = new Error(message || code);
  e.code = code;
  return e;
}

// ---------- 骰子 ----------
export function dieDef(tables, defId) {
  const d = tables.allDice[defId];
  if (!d) throw kernelError('E_KERNEL_UNKNOWN_DIE', defId);
  return d;
}

export function dieFaces(state, tables, dieId) {
  const die = state.player.dice[dieId];
  return die.faces || dieDef(tables, die.defId).faces;
}

export function currentFace(state, tables, dieId) {
  const die = state.player.dice[dieId];
  return dieFaces(state, tables, dieId)[die.faceIndex];
}

export function dieRiskTier(state, tables, dieId) {
  const die = state.player.dice[dieId];
  const def = dieDef(tables, die.defId);
  return computeRiskTier({ category: def.category, faces: dieFaces(state, tables, dieId) });
}

export function newDie(state, defId) {
  state.dieSeq += 1;
  const id = `d-${pad(state.dieSeq, 4)}`;
  state.player.dice[id] = { defId, faceIndex: 0, isCorrupt: false, faces: null, forged: false };
  return id;
}

/** 加入骰池（永久）。inBattle 时同时放入弃骰堆。 */
export function addDieToDeck(state, defId, temporary = false) {
  const id = newDie(state, defId);
  if (temporary) state.player.dice[id].temporary = true;
  state.player.deck.push(id);
  if (state.node && state.node.battle) state.player.discardPile.push(id);
  return id;
}

export function removeDieFromDeck(state, dieId) {
  const p = state.player;
  p.deck = p.deck.filter((d) => d !== dieId);
  p.drawPile = p.drawPile.filter((d) => d !== dieId);
  p.discardPile = p.discardPile.filter((d) => d !== dieId);
  p.hand = p.hand.filter((d) => d !== dieId);
  delete p.dice[dieId];
}

export function refreshCorrupt(state, tables, dieId) {
  const die = state.player.dice[dieId];
  die.isCorrupt = faceHasSelfCorruption(currentFace(state, tables, dieId));
}

// ---------- 学派 ----------
/** deckSchool：骰池中学派技能骰计数最大者；平局取注册顺序靠前者；无学派骰 → neutral（决策 D-01） */
export function deckSchoolInfo(state, tables) {
  const counts = { blade: 0, arcana: 0, verdant: 0 };
  for (const id of state.player.deck) {
    const def = dieDef(tables, state.player.dice[id].defId);
    if (def.category === 'skill' && def.school in counts) counts[def.school] += 1;
  }
  let best = 'neutral';
  let bestN = 0;
  for (const s of SCHOOLS) if (counts[s] > bestN) { best = s; bestN = counts[s]; }
  const total = counts.blade + counts.arcana + counts.verdant;
  const concentrationPermille = total ? Math.floor((bestN * 1000) / total) : 0;
  return { school: best, counts, total, concentrationPermille };
}

export function isMono(state, tables) {
  const info = deckSchoolInfo(state, tables);
  const c = state.config;
  return info.total >= c.monoMinDice && info.concentrationPermille >= c.monoThresholdPermille;
}

// ---------- 不变式 ----------
function scanFloat(v, path) {
  if (typeof v === 'number' && !Number.isInteger(v)) throw kernelError('E_KERNEL_INVARIANT', `float at ${path}`);
  if (v && typeof v === 'object') for (const k of Object.keys(v)) scanFloat(v[k], `${path}.${k}`);
}

export function assertInvariants(state) {
  const p = state.player;
  if (p.hand.length > state.config.handMax) throw kernelError('E_KERNEL_INVARIANT', 'hand > handMax');
  const all = [...p.drawPile, ...p.discardPile, ...p.hand];
  if (new Set(all).size !== all.length) throw kernelError('E_KERNEL_INVARIANT', 'duplicate die in piles');
  for (const id of all) if (!p.deck.includes(id)) throw kernelError('E_KERNEL_INVARIANT', `die ${id} not in deck`);
  if (state.node && state.node.battle && all.length !== p.deck.length) throw kernelError('E_KERNEL_INVARIANT', 'die lost');
  for (const id of p.deck) {
    const d = p.dice[id];
    if (!d || d.faceIndex < 0 || d.faceIndex > 5) throw kernelError('E_KERNEL_INVARIANT', `bad die ${id}`);
  }
  const names = state.rng.map((r) => r.name);
  if (names.join('|') !== STREAM_NAMES.join('|')) throw kernelError('E_KERNEL_INVARIANT', 'rng streams');
  scanFloat(state, 'state');
}
