import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { buildTables, DEFAULT_CONTENT_DIR, computeRiskTier } from '../../kernel/loader.js';
import { tables } from '../helpers.js';

const NAMES = ['schools', 'difficulty', 'dice', 'skills', 'characters', 'monsters', 'stages', 'events', 'relics', 'shops'];
const raw = () => Object.fromEntries(NAMES.map((n) => [n, JSON.parse(fs.readFileSync(path.join(DEFAULT_CONTENT_DIR, `${n}.json`), 'utf8'))]));

test('配表数量满足 PRD 下限', () => {
  const skills = Object.values(tables.skills);
  for (const s of ['blade', 'arcana', 'verdant']) assert.ok(skills.filter((d) => d.school === s).length >= 4, s);
  const mons = Object.values(tables.monsters);
  assert.ok(mons.filter((m) => m.tier === 'normal').length >= 12);
  assert.ok(mons.filter((m) => m.tier === 'elite').length >= 4);
  assert.equal(mons.filter((m) => m.tier === 'boss').length, 3);
  for (const s of ['blade', 'arcana', 'verdant']) assert.ok(mons.filter((m) => m.tier === 'normal' && m.school === s).length >= 3);
  assert.ok(Object.keys(tables.events).length >= 10);
  assert.ok(Object.values(tables.events).filter((e) => e.school).length >= 3);
  assert.ok(Object.keys(tables.relics).length >= 14);
  assert.equal(tables.monsters.boss_chaos_core.phases.length, 3);
  assert.deepEqual(tables.monsters.boss_chaos_core.phases.map((p) => p.school).sort(), ['arcana', 'blade', 'verdant']);
});

test('风险等级由 loader 计算并与声明一致', () => {
  assert.equal(tables.skills.skill_blade_flurry.riskTier, 'safe');
  assert.equal(tables.skills.skill_blade_execute.riskTier, 'balanced');
  assert.equal(tables.skills.skill_blade_berserk.riskTier, 'risky');
  assert.equal(tables.dice.die_curse_heavy.riskTier, 'cursed');
  assert.equal(computeRiskTier({ category: 'normal', faces: tables.dice.die_strike.faces }), 'safe');
});

test('错误配表被拒：面数 / 风险声明 / 学派规则 / 悬空引用 / ID 格式', () => {
  const cases = [
    ['E_CONTENT_FACES', (r) => { r.skills.skills.skill_blade_flurry.faces.pop(); }],
    ['E_CONTENT_RISK_MISMATCH', (r) => { r.skills.skills.skill_blade_flurry.riskTier = 'risky'; }],
    ['E_CONTENT_SCHOOL_RULE', (r) => { r.skills.skills.skill_blade_flurry.faces[5].school = 'blade'; }],
    ['E_CONTENT_DANGLING_REF', (r) => { r.monsters.groups.g_01.members = ['mon_nope']; }],
    ['E_CONTENT_ID_FORMAT', (r) => { r.relics.relics.BadId = r.relics.relics.relic_iron_bark; }],
    ['E_CONTENT_EVENT', (r) => { r.events.events.ev_shrine_shards.options.length = 2; }],
    ['E_CONTENT_STAGES', (r) => { r.stages.stages.pop(); }]
  ];
  for (const [code, mutate] of cases) {
    const r = raw();
    mutate(r);
    assert.throws(() => buildTables(r), (e) => e.code === code, code);
  }
});
