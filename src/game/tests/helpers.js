// tests/helpers.js — 测试公共工具
import { loadTables } from '../kernel/loader.js';
import * as K from '../kernel/index.js';

export const tables = loadTables();

export function newGame(opts = {}) {
  return K.createGame({ difficulty: 'medium', seed: 12345, toggles: {}, runId: 'r-test', ...opts }, tables);
}

/** 开一场战斗并返回可自由改写的 state（测试专用） */
export function battleState(members = ['mon_verdant_slime'], opts = {}) {
  return structuredClone(K.debugStartBattle(newGame(opts), members, tables));
}

/** 把某颗手骰改成指定骰子定义的指定面 */
export function setHandDie(state, idx, defId, faceIndex, faces = null) {
  const id = state.player.hand[idx];
  const d = state.player.dice[id];
  d.defId = defId;
  d.faceIndex = faceIndex;
  d.faces = faces;
  const f = (faces || tables.allDice[defId].faces)[faceIndex];
  d.isCorrupt = f.kind === 'selfCorruption' || (f.selfCorruption | 0) > 0;
  return id;
}
