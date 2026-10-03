#!/usr/bin/env node
// scripts/golden-gen.js — 生成 tests/golden/*.jsonl（固定 seed 的动作脚本 + 每步 obsDigest/rngDigest 快照，GAME-01 §7）
// 用法：
//   node scripts/golden-gen.js                          # 重生成 5 组 golden
//   node scripts/golden-gen.js --random-walk N [--out DIR] [--seed-base S]
//                                                       # 差分对拍：N 局随机动作（MOD-01 成功度量②）
// 配表或规则变更后需重新生成并审阅 diff。

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadTables } from '../kernel/loader.js';
import * as K from '../kernel/index.js';
import { runGame } from './bots.js';

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '..');
const tables = loadTables();
const OFF = { mapBranching: false, relics: false, events: false, shops: false, schoolCounter: false };

const SETS = [
  { name: 'g1_all_off_medium', difficulty: 'medium', seed: 1042, toggles: OFF, strategy: 'greedy_counter', maxSteps: 700 },
  { name: 'g2_all_on_medium', difficulty: 'medium', seed: 7, toggles: {}, strategy: 'greedy_counter', maxSteps: 700 },
  { name: 'g3_easy_full_run', difficulty: 'easy', seed: 10003, toggles: {}, strategy: 'greedy_counter', maxSteps: 4000 },
  { name: 'g4_hard_corrupt_path', difficulty: 'hard', seed: 10001, toggles: {}, strategy: 'school_blade', maxSteps: 700, injectIllegalEndTurn: true },
  // Spec v1.3 §8.6 默认局（hard × 6 关，D-34），MOD-01 移植对拍的主基准；seed 3100 不属于任何 split（避免 INV-5 审计误报）
  { name: 'g5_hard_six_stage', difficulty: 'hard', seed: 3100, toggles: {}, strategy: 'greedy_counter', maxSteps: 1000, stageCount: 6 }
];

function writeEpisode(OUT, name, config, strategy, actions) {
  let state = K.createGame(config, tables);
  const lines = [JSON.stringify({ type: 'meta', name, config, strategy, steps: actions.length, kernelVersion: K.KERNEL_VERSION })];
  lines.push(JSON.stringify({ step: 0, action: null, obsDigest: K.digest(K.observe(state, 'full', tables)), rngDigest: K.digest(state.rng), stateDigest: K.digest(state) }));
  let illegal = 0;
  actions.forEach((a, i) => {
    const res = K.reduce(state, a, tables);
    if (res.illegal) illegal += 1;
    state = res.state;
    lines.push(JSON.stringify({ step: i + 1, action: a, illegal: !!res.illegal, obsDigest: K.digest(K.observe(state, 'full', tables)), rngDigest: K.digest(state.rng), stateDigest: K.digest(state) }));
  });
  lines.push(JSON.stringify({ type: 'run_end', outcome: state.outcome, stage: state.stage, phase: state.phase }));
  fs.writeFileSync(path.join(OUT, `${name}.jsonl`), lines.join('\n') + '\n');
  return { illegal, outcome: state.outcome, steps: actions.length };
}

// ----------------------------------------------------------------------
// 默认：重生成 5 组 golden
// ----------------------------------------------------------------------
function genGolden() {
  const OUT = path.join(ROOT, 'tests', 'golden');
  fs.mkdirSync(OUT, { recursive: true });
  for (const set of SETS) {
    const r = runGame({ difficulty: set.difficulty, seed: set.seed, toggles: set.toggles, strategy: set.strategy, tables, record: true, maxSteps: set.maxSteps, stageCount: set.stageCount == null ? null : set.stageCount });
    const config = { difficulty: set.difficulty, seed: set.seed, toggles: { ...{ mapBranching: true, relics: true, events: true, shops: true, schoolCounter: true }, ...set.toggles }, runId: `r-golden${set.seed}`, ...(set.stageCount == null ? {} : { stageCount: set.stageCount }) };
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
    const info = writeEpisode(OUT, set.name, config, set.strategy, actions);
    console.log(`${set.name}: ${info.steps} 步, 非法 ${info.illegal}, 结局 ${info.outcome || '(截断)'}`);
  }
}

// ----------------------------------------------------------------------
// 差分对拍：N 局随机动作（MOD-01 成功度量②）。JS 侧生成动作与快照，Python 侧只读重放。
// ----------------------------------------------------------------------
function illegalProbe(state) {
  // 构造一个保证非法的动作，校验两侧拒绝路径一致（状态不变 + illegal=true）
  if (state.phase === 'map') return { kind: 'map_choose', nodeId: 'n-nope' };
  if (state.phase === 'player_turn') return { kind: 'play_face', dieId: 'd-nope', slotId: 'slot_1' };
  if (state.phase === 'shop') return { kind: 'shop_buy', itemId: 's-nope' };
  if (state.phase === 'treasure') return { kind: 'treasure_pick', defId: 'die_nope' };
  if (state.phase === 'event') return { kind: 'event_choose', optionId: 'opt_99' };
  if (state.phase === 'forge') return { kind: 'forge_pick', dieId: 'd-nope', faceIndex: 0, candidateId: 'ff_nope' };
  if (state.phase === 'shrine' || state.phase === 'rest') return { kind: 'treasure_pick', defId: 'die_nope' };
  if (state.phase === 'relic_swap') return { kind: 'relic_swap', relicId: 'relic_nope' };
  return { kind: 'end_turn', dieId: 'd-nope' };
}

function randomWalk(count, outDir, seedBase) {
  const OUT = path.resolve(outDir);
  fs.mkdirSync(OUT, { recursive: true });
  const diffs = ['easy', 'medium', 'hard'];
  let totalSteps = 0, totalIllegal = 0, wins = 0;
  for (let i = 0; i < count; i++) {
    const difficulty = diffs[i % diffs.length];
    const seed = seedBase + i;
    const cfg = { difficulty, seed, runId: `r-rw${seed}` };
    const r = runGame({ difficulty, seed, strategy: 'random', toggles: {}, tables,
      record: true, maxSteps: 300, runId: `r-rw${seed}` });
    // 每 7 步插入一次非法探测动作（前置于真实动作之前），覆盖拒绝路径
    const actions = [];
    let s = K.createGame(cfg, tables);
    for (const a of r.actions) {
      if (actions.length % 7 === 3) {
        const probe = illegalProbe(s);
        actions.push(probe);
        s = K.reduce(s, probe, tables).state;
      }
      actions.push(a);
      s = K.reduce(s, a, tables).state;
    }
    const info = writeEpisode(OUT, `rw-${String(i).padStart(3, '0')}`, cfg, 'random', actions);
    totalSteps += info.steps; totalIllegal += info.illegal;
    if (info.outcome === 'win') wins += 1;
  }
  console.log(`random-walk: ${count} 局, ${totalSteps} 步, 非法 ${totalIllegal}, 通关 ${wins}`);
}

// ----------------------------------------------------------------------
// 调试导出：重放 golden 到指定 step，打印全状态 JSON（对拍定位用，PRD §16 #2）
// 用法：node scripts/golden-gen.js --dump-state g1_all_off_medium --step 58
// ----------------------------------------------------------------------
function dumpState(name, upto) {
  const file = path.join(ROOT, 'tests', 'golden', `${name}.jsonl`);
  const lines = fs.readFileSync(file, 'utf8').split('\n').filter(Boolean).map(JSON.parse);
  let state = K.createGame(lines[0].config, tables);
  for (let i = 1; i < lines.length; i++) {
    if (lines[i].type === 'run_end' || lines[i].step > upto) break;
    state = K.reduce(state, lines[i].action, tables).state;
  }
  console.log(JSON.stringify(state, null, 2));
}

// ----------------------------------------------------------------------
// CLI
// ----------------------------------------------------------------------
const argv = process.argv.slice(2);
if (argv[0] === '--random-walk') {
  const count = parseInt(argv[1], 10) || 200;
  const outDir = (argv.includes('--out') ? argv[argv.indexOf('--out') + 1]
    : path.join(ROOT, 'tests', 'random-walk'));
  const seedBase = (argv.includes('--seed-base') ? parseInt(argv[argv.indexOf('--seed-base') + 1], 10) : 50000);
  randomWalk(count, outDir, seedBase);
} else if (argv[0] === '--dump-state') {
  dumpState(argv[1], parseInt(argv[argv.indexOf('--step') + 1], 10));
} else {
  genGolden();
}
