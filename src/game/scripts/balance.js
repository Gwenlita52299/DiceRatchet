#!/usr/bin/env node
// scripts/balance.js — 平衡脚本（GAME-00 §7.2 / GAME-02 §8，验收 A6/A10）
// 用法：node scripts/balance.js [--n 200] [--difficulty easy|medium|hard|all] [--strategy greedy_damage|all] [--duel]

import { loadTables } from '../kernel/loader.js';
import { runGame, runDuel, STRATEGIES } from './bots.js';

function arg(name, def) {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && process.argv[i + 1] && !process.argv[i + 1].startsWith('--') ? process.argv[i + 1] : def;
}
const flag = (name) => process.argv.includes(`--${name}`);

const tables = loadTables();
const n = Number(arg('n', 200));
const diffs = arg('difficulty', 'all') === 'all' ? ['easy', 'medium', 'hard'] : [arg('difficulty')];
const strategies = arg('strategy', 'best') === 'all' ? STRATEGIES : arg('strategy', 'best') === 'best' ? ['greedy_counter'] : [arg('strategy')];
const TARGET = { easy: [45, 65], medium: [25, 40], hard: [10, 20] };

const t0 = Date.now();
if (!flag('duel-only')) {
  console.log(`# 通关率（每组 ${n} 局）`);
  console.log('| 难度 | 策略 | 通关率 | 目标 | 平均到达关 | 平均回合/局 | 技能骰使用/胜局 |');
  console.log('|---|---|---|---|---|---|---|');
  for (const d of diffs) {
    for (const st of strategies) {
      let wins = 0, stageSum = 0, turnSum = 0, skillWin = 0;
      for (let i = 0; i < n; i++) {
        const r = runGame({ difficulty: d, seed: 10000 + i, strategy: st, tables });
        if (r.outcome === 'win') { wins += 1; skillWin += r.stats.skillFaceUses; }
        stageSum += r.stage;
        turnSum += r.stats.turns;
      }
      const rate = (wins * 100) / n;
      const [lo, hi] = TARGET[d];
      const mark = rate >= lo && rate <= hi ? '✅' : '⚠️';
      console.log(`| ${d} | ${st} | ${rate.toFixed(1)}% ${mark} | ${lo}–${hi}% | ${(stageSum / n).toFixed(2)} | ${(turnSum / n).toFixed(1)} | ${wins ? (skillWin / wins).toFixed(1) : '-'} |`);
    }
  }
}

if (flag('duel') || flag('duel-only')) {
  // A6：同一敌人在"克制学派构筑"与"被克学派构筑"下的胜率差
  const base = ['die_strike', 'die_strike', 'die_guard', 'die_guard', 'die_mend', 'die_utility'];
  const builds = {
    blade: ['skill_blade_flurry', 'skill_blade_charge', 'skill_blade_reap', 'skill_blade_flurry'],
    arcana: ['skill_arcana_echo', 'skill_arcana_hex', 'skill_arcana_echo', 'skill_arcana_shift'],
    verdant: ['skill_verdant_venom', 'skill_verdant_bloom', 'skill_verdant_bark', 'skill_verdant_venom']
  };
  const foes = { elite_verdant_warden: 'verdant', elite_arcana_weaver: 'arcana', elite_blade_champion: 'blade' };
  const m = Math.max(50, Math.floor(n / 2));
  console.log(`\n# 学派 × 敌人学派 胜率矩阵（精英单挑，每格 ${m} 场，medium）`);
  console.log('| 敌人 \\ 构筑 | blade | arcana | verdant | 克制−被克 |');
  console.log('|---|---|---|---|---|');
  for (const [foe, school] of Object.entries(foes)) {
    const rates = {};
    for (const [b, dice] of Object.entries(builds)) {
      let w = 0;
      for (let i = 0; i < m; i++) if (runDuel({ tables, seed: 500 + i, deck: [...base, ...dice], members: [foe], strategy: 'greedy_damage' }) === 'win') w += 1;
      rates[b] = (w * 100) / m;
    }
    const counter = { verdant: 'blade', arcana: 'verdant', blade: 'arcana' }[school];
    const weak = { verdant: 'arcana', arcana: 'blade', blade: 'verdant' }[school];
    const diff = rates[counter] - rates[weak];
    console.log(`| ${foe} (${school}) | ${rates.blade.toFixed(0)}% | ${rates.arcana.toFixed(0)}% | ${rates.verdant.toFixed(0)}% | ${diff >= 0 ? '+' : ''}${diff.toFixed(0)}pp ${diff >= 20 ? '✅' : '⚠️'} |`);
  }
}
console.log(`\n耗时 ${((Date.now() - t0) / 1000).toFixed(1)}s`);
