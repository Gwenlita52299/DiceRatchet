// kernel/forge.js — 锻造：5 选 1 替换骰面（GAME-00 §8.5 / GAME-02 §1.3），消费 forge 流

import { weighted, randint } from './rng.js';
import { applyMul } from './counter.js';
import { dieDef, dieFaces, refreshCorrupt } from './state.js';

export function enterForge(ctx, prepaid) {
  const { state, tables } = ctx;
  const items = Object.values(tables.forgeFaces).map((f) => ({ key: f.id, weight: f.weight }));
  const candidates = [];
  const n = tables.shops.forge.candidates;
  while (candidates.length < n && items.length) {
    const k = weighted(state, 'forge', items);
    candidates.push(k);
    items.splice(items.findIndex((x) => x.key === k), 1);
  }
  const cfg = tables.shops.forge;
  const price = applyMul(randint(state, 'forge', cfg.priceMin, cfg.priceMax), state.config.shopPriceMulPermille);
  state.node.forge = { candidates, price: prepaid ? 0 : price, done: false };
  state.phase = 'forge';
}

export function forgeableDice(state, tables) {
  return state.player.deck.filter((id) => dieDef(tables, state.player.dice[id].defId).category !== 'cursed');
}

export function applyForgedFace(state, tables, dieId, faceIndex, forgeFaceId) {
  const die = state.player.dice[dieId];
  const faces = dieFaces(state, tables, dieId).map((f) => ({ ...f }));
  faces[faceIndex] = { ...tables.forgeFaces[forgeFaceId].face, faceIndex, forged: true };
  die.faces = faces;
  die.forged = true;
  refreshCorrupt(state, tables, dieId);
}

export function forgePick(ctx, action) {
  const { state, tables, events } = ctx;
  const f = state.node.forge;
  state.player.starShards -= f.price;
  applyForgedFace(state, tables, action.dieId, action.faceIndex, action.candidateId);
  f.done = true;
  events.push({ type: 'die_forged', dieId: action.dieId, faceIndex: action.faceIndex, faceId: action.candidateId });
}
