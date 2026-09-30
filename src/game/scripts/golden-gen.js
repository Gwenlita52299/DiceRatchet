#!/usr/bin/env node
// scripts/golden-gen.js — 生成 tests/golden/*.jsonl（固定 seed 的动作脚本 + 每步 obsDigest/rngDigest 快照，GAME-01 §7）
// 用法：node scripts/golden-gen.js   （配表或规则变更后需重新生成并审阅 diff）

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadTables } from '../kernel/loader.js';
import * as K from '../kernel/index.js';
import { runGame } from './bots.js';

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '..');
const OUT = path.join(ROOT, 'tests', 'golden');
const tables = loadTables();
const OFF = { mapBranching: false, relics: false, events: false, shops: false, schoolCounter: false };

const SETS = [
  { name: 'g1_all_off_medium', difficulty: 'medium', seed: 1042, toggles: OFF, strategy: 'greedy_counter', maxSteps: 700 },
  { name: 'g2_all_on_medium', difficulty: 'medium', seed: 7, toggles: {}, strategy: 'greedy_counter', maxSteps: 700 },
  { name: 'g3_easy_full_run', difficulty: 'easy', seed: 10003, toggles: {}, strategy: 'greedy_counter', maxSteps: 4000 },
  { name: 'g4_hard_corrupt_path', difficulty: 'hard', seed: 10001, toggles: {}, strategy: 'school_blade', maxSteps: 700, injectIllegalEndTurn: true }
];

fs.mkdirSync(OUT, { recursive: true });
for (const set of SETS) {
  const r = runGame({ difficulty: set.difficulty, seed: set.seed, toggles: set.toggles, strategy: set.strategy, tables, record: true, maxSteps: set.maxSteps });
  const config = { difficulty: set.difficulty, seed: set.seed, toggles: { ...{ mapBranching: true, relics: true, events: true, shops: true, schoolCounter: true }, ...set.toggles }, runId: `r-golden${set.seed}` };
  // 魔化骰路径：在处理魔化骰（弃/净化）之前插入一次非法 end_turn（应被拒绝且状态不变）
  let actions = r.actions;
  if (set.injectIllegalEndTurn) {
    const out = [];
    let s = K.createGame(config, tables);
    for (const a of actions) {
      if ((a.kind === 'discard_die' || a.kind === 'purify') && s.phase === 'player_turn' && s.player.dice[a.dieId].isCorrupt) out.push({ kind: 'end_turn' });
      out.push(a);
      s = K.reduce(s, a, tables).state;
    }
    actions = out;
  }
  let state = K.createGame(config, tables);
  const lines = [JSON.stringify({ type: 'meta', name: set.name, config, strategy: set.strategy, steps: actions.length, kernelVersion: K.KERNEL_VERSION })];
  lines.push(JSON.stringify({ step: 0, action: null, obsDigest: K.digest(K.observe(state, 'full', tables)), rngDigest: K.digest(state.rng) }));
  let illegal = 0;
  actions.forEach((a, i) => {
    const res = K.reduce(state, a, tables);
    if (res.illegal) illegal += 1;
    state = res.state;
    lines.push(JSON.stringify({ step: i + 1, action: a, illegal: !!res.illegal, obsDigest: K.digest(K.observe(state, 'full', tables)), rngDigest: K.digest(state.rng) }));
  });
  lines.push(JSON.stringify({ type: 'run_end', outcome: state.outcome, stage: state.stage, phase: state.phase }));
  fs.writeFileSync(path.join(OUT, `${set.name}.jsonl`), lines.join('\n') + '\n');
  console.log(`${set.name}: ${actions.length} 步, 非法 ${illegal}, 结局 ${state.outcome || '(截断)'} @stage ${state.stage}`);
}
