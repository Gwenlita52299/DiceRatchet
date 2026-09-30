// backend/validate.js — L1 语法校验（L2 语义校验由内核 isLegal 负责）

import { ACTION_KINDS, TOGGLE_KEYS } from '../kernel/index.js';
import { ApiError } from './errors.js';

const REQUIRED = {
  map_choose: ['nodeId'], play_face: ['dieId', 'slotId'], discard_die: ['dieId'], purify: ['dieId'],
  use_ability: ['abilityId'], end_turn: [], shop_buy: ['itemId'], shop_refresh: [], shop_remove: ['dieId'],
  shop_leave: [], event_choose: ['optionId'], treasure_pick: ['defId'], forge_pick: ['dieId', 'faceIndex', 'candidateId'],
  shrine_choose: ['optionId'], rest_choose: ['optionId'], relic_swap: ['relicId'], abandon_run: []
};

export function validateActionSyntax(action) {
  if (!action || typeof action !== 'object' || Array.isArray(action)) throw new ApiError('E_ACTION_SYNTAX', 'action 必须是对象');
  if (!ACTION_KINDS.includes(action.kind)) throw new ApiError('E_ACTION_SYNTAX', `未知 action.kind: ${action.kind}`);
  for (const f of REQUIRED[action.kind]) {
    if (action[f] === undefined || action[f] === null) throw new ApiError('E_ACTION_SYNTAX', `缺少字段 ${f}`);
  }
  for (const [k, v] of Object.entries(action)) {
    if (v !== null && !['string', 'number', 'boolean'].includes(typeof v)) throw new ApiError('E_ACTION_SYNTAX', `字段 ${k} 类型错误`);
  }
  return action;
}

export function validateGameRequest(body, tables) {
  const b = body || {};
  const difficulty = b.difficulty || 'medium';
  if (!tables.difficulty.levels[difficulty]) throw new ApiError('E_API_BAD_REQUEST', `未知难度 ${difficulty}`);
  const character = b.character || 'warrior';
  if (!tables.characters[character]) throw new ApiError('E_API_BAD_REQUEST', `未知角色 ${character}`);
  let seed = b.seed;
  if (seed !== undefined && seed !== null && seed !== '') {
    seed = Number(seed);
    if (!Number.isInteger(seed) || seed < 0 || seed >= 4294967296) throw new ApiError('E_API_BAD_REQUEST', 'seed 必须是 0 ≤ seed < 2^32 的整数');
  } else seed = null;
  const toggles = {};
  for (const k of TOGGLE_KEYS) {
    const v = b.toggles ? b.toggles[k] : undefined;
    if (v !== undefined && typeof v !== 'boolean') throw new ApiError('E_API_BAD_REQUEST', `toggles.${k} 必须是布尔值`);
    toggles[k] = v === undefined ? true : v;
  }
  const debug = { showHiddenInfo: !!(b.debug && b.debug.showHiddenInfo), logActions: !(b.debug && b.debug.logActions === false) };
  let stageCount = null;
  if (b.stageCount != null) {
    const allowed = tables.stages.stages.filter((x) => x.boss).map((x) => x.stage);
    if (!allowed.includes(b.stageCount)) throw new ApiError('E_API_BAD_REQUEST', `stageCount 只能取 ${allowed.join('/')}`);
    stageCount = b.stageCount;
  }
  return { difficulty, character, seed, toggles, debug, stageCount };
}
