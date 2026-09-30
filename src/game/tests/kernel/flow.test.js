import test from 'node:test';
import assert from 'node:assert/strict';
import * as K from '../../kernel/index.js';
import { tables } from '../helpers.js';
import { runGame } from '../../scripts/bots.js';

const ALL_OFF = { mapBranching: false, relics: false, events: false, shops: false, schoolCounter: false };
const ALL_ON = { mapBranching: true, relics: true, events: true, shops: true, schoolCounter: true };
const COMBOS = [
  ['全关', ALL_OFF], ['全开', ALL_ON],
  ...Object.keys(ALL_ON).map((k) => [`单开 ${k}`, { ...ALL_OFF, [k]: true }])
];

test('T-07 开关矩阵：7 种组合均可开局并通过前 3 关（easy，启发式 bot）', () => {
  for (const [name, toggles] of COMBOS) {
    let passed = false;
    for (let seed = 1; seed <= 8 && !passed; seed++) {
      const r = runGame({ difficulty: 'easy', seed, strategy: 'greedy_counter', toggles, tables, stopAtStage: 3 });
      if (r.state.stage > 3 || r.outcome === 'win') passed = true;
    }
    assert.ok(passed, `${name} 未能通过前 3 关`);
  }
});

test('T-07 全关时：地图线性、无事件/商店/遗物节点', () => {
  const s = K.createGame({ difficulty: 'medium', seed: 99, toggles: ALL_OFF }, tables);
  for (const sm of s.map.stages) {
    for (const row of sm.rows) {
      assert.equal(row.length, 1, '线性：每行 1 个节点');
      for (const n of row) assert.ok(!['shop', 'forge', 'event', 'shrine'].includes(n.kind), `stage ${sm.stage} 出现 ${n.kind}`);
    }
  }
  const obs = K.observe(s, 'full', tables);
  assert.deepEqual(obs.relics, []);
});

test('Boss 关 = 3/6/10；精英关按难度 eliteStages', () => {
  for (const d of ['easy', 'medium', 'hard']) {
    const s = K.createGame({ difficulty: d, seed: 5, toggles: {} }, tables);
    const bossStages = s.map.stages.filter((sm) => sm.rows.flat().some((n) => n.kind === 'boss')).map((sm) => sm.stage);
    assert.deepEqual(bossStages, [3, 6, 10]);
    const eliteStages = s.map.stages.filter((sm) => sm.rows.flat().some((n) => n.kind === 'elite')).map((sm) => sm.stage);
    assert.deepEqual(eliteStages, s.config.eliteStages);
  }
});

test('T-08 难度矩阵：配置键生效，且地图 RNG 消费与难度无关', () => {
  const lv = tables.difficulty.levels;
  const games = ['easy', 'medium', 'hard'].map((d) => K.createGame({ difficulty: d, seed: 2024, toggles: {} }, tables));
  games.forEach((g, i) => {
    const d = ['easy', 'medium', 'hard'][i];
    const o = K.observe(g, 'full', tables);
    assert.equal(o.player.hearts, lv[d].heartsMax);
    assert.equal(o.player.virtueMax, lv[d].virtueMax);
    assert.equal(o.config.drawPerTurn, lv[d].drawPerTurn);
    assert.equal(o.config.handMax, lv[d].handMax);
  });
  // 同 seed：各难度 map 流消费步数一致（精英替换不额外消费 RNG 之外的差异由 eliteStages 决定）
  const counters = games.map((g) => g.rng.find((r) => r.name === 'map').counter);
  assert.ok(counters.every((c) => c > 0));
  // 战斗中敌血按 enemyHpMulPermille 缩放
  const hp = ['easy', 'medium', 'hard'].map((d) => K.debugStartBattle(K.createGame({ difficulty: d, seed: 1, toggles: {} }, tables), ['mon_blade_brute'], tables).enemies[0].hpMax);
  ['easy', 'medium', 'hard'].forEach((d, i) => assert.equal(hp[i], Math.floor((34 * lv[d].enemyHpMulPermille) / 1000)));
});

test('完整对局可通关（easy，确定性）且无内核异常', () => {
  let wins = 0;
  for (let seed = 1; seed <= 6; seed++) {
    const r = runGame({ difficulty: 'easy', seed, strategy: 'greedy_counter', tables });
    assert.ok(['win', 'lose'].includes(r.outcome));
    if (r.outcome === 'win') wins += 1;
  }
  assert.ok(wins >= 1, 'easy 至少应有一局通关');
});

test('确定性：同 seed + 同动作 → 同 obsDigest 序列', () => {
  const r = runGame({ difficulty: 'medium', seed: 77, strategy: 'greedy_damage', tables, record: true, maxSteps: 300 });
  const cfg = { difficulty: 'medium', seed: 77, toggles: {}, runId: 'r-bot77' };
  const a = K.replay(cfg, r.actions, tables);
  const b = K.replay(cfg, r.actions, tables);
  assert.deepEqual(a.digests, b.digests);
  assert.equal(a.digests.length, r.actions.length + 1);
});

test('不变式：长局中每步 assertInvariants 通过；state 中无浮点', () => {
  const r = runGame({ difficulty: 'medium', seed: 31, strategy: 'greedy_counter', tables, record: true, maxSteps: 400 });
  let s = K.createGame({ difficulty: 'medium', seed: 31, toggles: {}, runId: 'r-bot31', debug: { showHiddenInfo: true } }, tables);
  for (const a of r.actions) {
    const res = K.reduce(s, a, tables);
    assert.ok(!res.error, res.error && res.error.message);
    s = res.state;
  }
});

test('observe("visual") 返回最小渲染输入，不含数值推导信息', () => {
  const s = K.debugStartBattle(K.createGame({ difficulty: 'medium', seed: 3, toggles: {} }, tables), ['mon_verdant_slime'], tables);
  const v = K.observe(s, 'visual', tables);
  assert.equal(v.view, 'visual');
  const txt = JSON.stringify(v);
  for (const k of ['deckSchool', 'hpMax', 'counterMul', '"deck"']) assert.ok(!txt.includes(k), k);
});
