import test from 'node:test';
import assert from 'node:assert/strict';
import * as K from '../../kernel/index.js';
import * as C from '../../kernel/combat.js';
import { tables, battleState, setHandDie } from '../helpers.js';

test('T-01 骰面索引：pip = faceIndex + 1，6 面全覆盖', () => {
  const s = battleState();
  const seen = new Set();
  let st = s;
  for (let i = 0; i < 40 && seen.size < 6; i++) {
    for (const h of K.observe(st, 'full', tables).hand) {
      assert.equal(h.pip, h.faceIndex + 1);
      assert.equal(h.label, tables.allDice[h.defId].faces[h.faceIndex].label);
      seen.add(h.faceIndex);
    }
    st = structuredClone(st);
    for (const id of st.player.hand) st.player.dice[id].isCorrupt = false;
    st = K.reduce(st, { kind: 'use_ability', abilityId: 'warrior_reroll' }, tables).state;
    st.player.abilities.find((a) => a.id === 'warrior_reroll').usesLeft = 1;
  }
  assert.equal(seen.size, 6);
});

test('T-03 结算顺序：先光盾后实伤', () => {
  const run = (ward, hp, dmg) => {
    const s = battleState(['mon_neutral_shell']);
    const e = s.enemies[0];
    e.ward = ward; e.hp = hp;
    const ctx = { state: s, tables, events: [] };
    C.damageEnemy(ctx, e, dmg, { source: 'test' }, false);
    return { ward: e.ward, hp: e.hp, dead: e.dead };
  };
  assert.deepEqual(run(5, 10, 3), { ward: 2, hp: 10, dead: false });
  assert.deepEqual(run(2, 10, 7), { ward: 0, hp: 5, dead: false });
  assert.equal(run(0, 4, 4).dead, true);
});

test('玩家攻击：学派克制 + 遗物倍率（C1 / C6 形态）', () => {
  const s = battleState(['mon_verdant_slime']);
  const face = { value: 5, tags: [] };
  const enemy = { school: 'verdant', hp: 10, hpMax: 10, tier: 'normal' };
  assert.equal(C.playerAttackValue(s, tables, face, { school: 'blade', slotBonus: 0 }, enemy).value, 6);
  assert.equal(C.playerAttackValue(s, tables, face, { school: 'arcana', slotBonus: 0 }, enemy).value, 3);
  s.player.relics = ['relic_boss_trophy'];
  const r = C.playerAttackValue(s, tables, { value: 6, tags: [] }, { school: 'blade' }, { ...enemy, tier: 'elite' });
  assert.equal(r.value, 8); // floor(floor(6*1.3)*1.2) = floor(7*1.2) = 8
});

test('M1/M2 魔化骰：残留时不能结束回合；弃掉承受 50% 上取整', () => {
  const s = battleState();
  s.player.virtue = 20;
  const id = setHandDie(s, 0, 'skill_blade_berserk', 5); // 自损 5
  const r1 = K.reduce(s, { kind: 'end_turn' }, tables);
  assert.equal(r1.illegal, true);
  assert.equal(r1.code, 'E_ACTION_CORRUPT_DIE_REMAINS');
  assert.equal(r1.state, s, '非法动作不改 state');
  const r2 = K.reduce(s, { kind: 'discard_die', dieId: id }, tables);
  assert.equal(r2.state.player.virtue, 17);
  assert.equal(K.isLegal(r2.state, { kind: 'end_turn' }, tables).legal, true);
});

test('M3/M4 净化：无代价；次数为 0 时非法', () => {
  const s = battleState();
  s.player.virtue = 20;
  s.player.purifyLeft = 1;
  const id = setHandDie(s, 0, 'die_curse_heavy', 4);
  const r = K.reduce(s, { kind: 'purify', dieId: id }, tables);
  assert.equal(r.state.player.virtue, 20);
  assert.equal(r.state.player.purifyLeft, 0);
  const s2 = structuredClone(r.state);
  const id2 = setHandDie(s2, 0, 'die_curse_heavy', 4);
  assert.equal(K.reduce(s2, { kind: 'purify', dieId: id2 }, tables).code, 'E_ACTION_NO_PURIFY');
});

test('M5 自魔化不吃光盾/力量/学派', () => {
  const s = battleState();
  s.player.virtue = 20; s.player.ward = 10; s.player.statuses.strength = 3;
  const id = setHandDie(s, 0, 'die_curse_heavy', 4); // 自损 4
  const r = K.reduce(s, { kind: 'play_face', dieId: id, slotId: 'slot_5' }, tables);
  assert.equal(r.illegal, false);
  assert.equal(r.state.player.virtue, 16);
  assert.equal(r.state.player.ward, 10);
});

test('M6 复合面：先自魔化后魔化', () => {
  const s = battleState(['mon_neutral_shell']);
  s.player.virtue = 20;
  const faces = tables.allDice.die_strike.faces.map((f) => ({ ...f }));
  faces[5] = { ...tables.forgeFaces.ff_risky_purge9.face, faceIndex: 5 };
  const id = setHandDie(s, 0, 'die_strike', 5, faces);
  const r = K.reduce(s, { kind: 'play_face', dieId: id, slotId: 'slot_1' }, tables);
  const types = r.events.map((e) => e.type);
  assert.ok(types.indexOf('self_corruption') < types.indexOf('damage'));
  assert.equal(r.state.player.virtue, 17);
});

test('槽位约束与类型不匹配被拒绝，且提供原因', () => {
  const s = battleState();
  const id = setHandDie(s, 0, 'die_guard', 0); // 光盾，pip 1
  assert.equal(K.isLegal(s, { kind: 'play_face', dieId: id, slotId: 'slot_1' }, tables).code, 'E_ACTION_KIND_MISMATCH');
  const id2 = setHandDie(s, 1, 'die_strike', 1); // 刺击 2，pip 2
  assert.equal(K.isLegal(s, { kind: 'play_face', dieId: id2, slotId: 'slot_2' }, tables).code, 'E_ACTION_CONSTRAINT_VIOLATION');
  const hints = K.playHints(s, tables);
  assert.equal(hints[id2].slot_2.legal, false);
  assert.match(hints[id2].slot_2.reason, /≥3/);
  assert.equal(hints[id2].slot_1.legal, true);
});

test('reduce 不修改入参（K-1）', () => {
  const s = battleState();
  const frozen = JSON.stringify(s);
  const a = K.legalActions(s, tables).find((x) => x.kind === 'play_face') || { kind: 'end_turn' };
  K.reduce(s, a, tables);
  assert.equal(JSON.stringify(s), frozen);
});

test('敌人意图在玩家回合开始提交，敌方回合按已提交意图结算（不重掷）', () => {
  let s = battleState(['mon_blade_grunt']);
  for (const id of s.player.hand) s.player.dice[id].isCorrupt = false;
  const intent = JSON.stringify(s.enemies[0].intent);
  s.player.ward = 0; s.player.virtue = 50; s.player.virtueMax = 50;
  const before = s.player.virtue;
  const expected = s.enemies[0].intent.filter((i) => i.kind === 'purge').reduce((acc, i) => acc + C.enemyAttackValue(s, tables, s.enemies[0], i.value).value, 0);
  const r = K.reduce(s, { kind: 'end_turn' }, tables);
  const taken = r.events.filter((e) => e.type === 'damage' && e.target === 'player').reduce((acc, e) => acc + e.value, 0);
  assert.equal(taken, expected);
  assert.ok(intent.length > 2);
  assert.ok(r.state.player.virtue <= before);
});

test('T-11 Boss 阶段：阈值切换学派与槽位并刷新意图', () => {
  const s = battleState(['boss_chaos_core']);
  const e = s.enemies[0];
  assert.equal(e.school, 'arcana');
  const ctx = { state: s, tables, events: [] };
  C.damageEnemy(ctx, e, e.hp - Math.floor((e.hpMax * 60) / 100), { source: 'test' }, false);
  assert.equal(e.phase, 2);
  assert.equal(e.school, 'blade');
  assert.ok(ctx.events.some((x) => x.type === 'boss_phase' && x.phase === 2));
  C.damageEnemy(ctx, e, e.hp - Math.floor((e.hpMax * 30) / 100), { source: 'test' }, false);
  assert.equal(e.phase, 3);
  assert.equal(e.school, 'verdant');
  assert.ok(e.intent.length > 0);
});

test('失心流程：hearts>0 时本场判负并回地图；reviveOnce 仅一次', () => {
  const s = battleState(['mon_verdant_slime'], { difficulty: 'easy' });
  s.player.virtue = 2;
  const id = setHandDie(s, 0, 'die_curse_heavy', 0); // 自损 3
  const r = K.reduce(s, { kind: 'play_face', dieId: id, slotId: 'slot_5' }, tables);
  assert.ok(r.events.some((e) => e.type === 'revive'));
  assert.equal(r.state.phase, 'player_turn');
  const s2 = structuredClone(r.state);
  s2.player.virtue = 1;
  const id2 = setHandDie(s2, 0, 'die_curse_heavy', 0);
  const r2 = K.reduce(s2, { kind: 'play_face', dieId: id2, slotId: 'slot_5' }, tables);
  assert.ok(r2.events.some((e) => e.type === 'player_lost_heart'));
  assert.equal(r2.state.player.hearts, s.player.hearts - 1);
  assert.notEqual(r2.state.phase, 'player_turn');
});
