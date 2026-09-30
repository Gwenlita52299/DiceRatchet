// kernel/map.js — 章节/关卡/地图生成（GAME-00 §4.4 / GAME-02 §4），只消费 map 流

import { choice } from './rng.js';
import { pad } from './state.js';

const NON_BATTLE = ['shop', 'forge', 'event', 'treasure', 'shrine', 'rest'];

export function stageDef(tables, stage) {
  return tables.stages.stages.find((s) => s.stage === stage);
}

export function chapterDef(tables, chapter) {
  return tables.stages.chapters.find((c) => c.chapter === chapter);
}

function substitute(tables, toggles, kind) {
  const fb = tables.stages.toggleFallback[kind];
  if (fb && !toggles[fb.toggle]) return substitute(tables, toggles, fb.fallback);
  return kind;
}

function edgesFor(i, n, m) {
  // 行 r 的第 i 个节点（共 n 个）连接到行 r+1（共 m 个）的区间，保证每个节点可达且有出边
  const lo = Math.floor((i * m) / n);
  const hi = Math.max(lo, Math.floor(((i + 1) * m - 1) / n));
  const out = [];
  for (let j = lo; j <= hi; j++) out.push(j);
  return out;
}

function generateStage(state, tables, sd) {
  const c = state.config;
  const linear = !c.toggles.mapBranching;
  const elite = c.eliteStages.includes(sd.stage);
  const rows = [];
  const usedKinds = new Set();
  const pool = [...new Set(sd.kinds.map((k) => substitute(tables, c.toggles, k)))].sort();
  for (let r = 0; r < sd.rows; r++) {
    const last = r === sd.rows - 1;
    let count = linear ? 1 : sd.nodeBudget[r];
    if (last && sd.boss) count = 1;
    const row = [];
    for (let i = 0; i < count; i++) {
      let kind;
      if (r === 0) kind = 'battle';
      else if (last) kind = sd.boss ? 'boss' : (elite ? 'elite' : 'battle');
      else {
        const avail = pool.filter((k) => !(NON_BATTLE.includes(k) && usedKinds.has(k)));
        kind = choice(state, 'map', avail.length ? avail : ['battle']);
        if (NON_BATTLE.includes(kind)) usedKinds.add(kind);
      }
      row.push({ nodeId: `n-${pad(sd.stage, 2)}-${r}-${i}`, kind, row: r, index: i, edges: [], enemyGroupId: null, visited: false });
    }
    rows.push(row);
  }
  // 精英：Boss 关的倒数第二行首个战斗节点升格为精英（无战斗节点则首节点）
  if (elite && sd.boss && sd.rows >= 3) {
    const row = rows[sd.rows - 2];
    const target = row.find((n) => n.kind === 'battle') || row[0];
    if (NON_BATTLE.includes(target.kind)) usedKinds.delete(target.kind);
    target.kind = 'elite';
  }
  // 编队
  const groups = Object.values(tables.groups);
  const normalPool = groups.filter((g) => !g.elite && g.stages[0] <= sd.stage && sd.stage <= g.stages[1]).map((g) => g.id).sort();
  const elitePool = groups.filter((g) => g.elite).map((g) => g.id).sort();
  for (const row of rows) {
    for (const n of row) {
      if (n.kind === 'battle') n.enemyGroupId = choice(state, 'map', normalPool);
      else if (n.kind === 'elite') n.enemyGroupId = choice(state, 'map', elitePool);
      else if (n.kind === 'boss') n.enemyGroupId = `boss:${sd.bossId}`;
    }
  }
  // 边
  for (let r = 0; r < rows.length - 1; r++) {
    const n = rows[r].length, m = rows[r + 1].length;
    rows[r].forEach((node, i) => { node.edges = edgesFor(i, n, m).map((j) => rows[r + 1][j].nodeId); });
  }
  return { stage: sd.stage, chapter: sd.chapter, rows };
}

export function generateMaps(state, tables) {
  const stages = [];
  for (let s = 1; s <= state.config.stageCount; s++) stages.push(generateStage(state, tables, stageDef(tables, s)));
  return { stages, currentRow: -1, currentNodeId: null, path: [] };
}

export function currentStageMap(state) {
  return state.map.stages[state.stage - 1];
}

export function findNode(state, nodeId) {
  const sm = currentStageMap(state);
  for (const row of sm.rows) for (const n of row) if (n.nodeId === nodeId) return n;
  return null;
}

export function reachableNodeIds(state) {
  const sm = currentStageMap(state);
  if (state.map.currentRow < 0) return sm.rows[0].map((n) => n.nodeId);
  const cur = findNode(state, state.map.currentNodeId);
  return cur ? cur.edges.slice() : [];
}

export function groupMembers(tables, groupId) {
  if (groupId.startsWith('boss:')) return [groupId.slice(5)];
  return tables.groups[groupId].members.slice();
}
