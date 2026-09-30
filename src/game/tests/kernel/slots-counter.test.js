import test from 'node:test';
import assert from 'node:assert/strict';
import { checkConstraint, parseConstraint } from '../../kernel/slots.js';
import { schoolMulPermille, applyMul } from '../../kernel/counter.js';

test('T-02 约束 DSL', () => {
  const cases = [
    ['any', 1, true], ['odd', 3, true], ['odd', 4, false], ['even', 6, true], ['even', 1, false],
    ['ge:3', 3, true], ['ge:3', 2, false], ['gt:3', 3, false], ['le:2', 2, true], ['lt:2', 2, false],
    ['eq:5', 5, true], ['in:2,4,6', 4, true], ['in:2,4,6', 5, false]
  ];
  for (const [c, pip, exp] of cases) assert.equal(checkConstraint(c, pip), exp, `${c} ${pip}`);
  for (const bad of ['ge3', 'foo', 'in:', 'ge:x', '']) assert.throws(() => parseConstraint(bad), (e) => e.code === 'E_KERNEL_CONSTRAINT_SYNTAX');
});

const cfg = (on = true) => ({ toggles: { schoolCounter: on }, counterMulPermille: { advantage: 1300, disadvantage: 770 } });

test('T-04 克制方阵与 GAME-01 §9.1 完全一致', () => {
  const S = ['blade', 'arcana', 'verdant', 'neutral'];
  const expect = {
    blade: [1000, 770, 1300, 1000], arcana: [1300, 1000, 770, 1000], verdant: [770, 1300, 1000, 1000], neutral: [1000, 1000, 1000, 1000]
  };
  for (const a of S) S.forEach((d, i) => assert.equal(schoolMulPermille(cfg(), a, d), expect[a][i], `${a}→${d}`));
  for (const a of S) for (const d of S) assert.equal(schoolMulPermille(cfg(false), a, d), 1000);
});

test('GAME-01 §9.2 伤害向量（先学派后遗物，各自 floor，攻击最小 1）', () => {
  const fin = (base, mul, relic) => Math.max(1, applyMul(applyMul(base, mul), relic));
  assert.equal(fin(5, 1300, 1000), 6); // C1
  assert.equal(fin(5, 770, 1000), 3); // C2
  assert.equal(fin(4, 1000, 1000), 4); // C3
  assert.equal(fin(1, 770, 1000), 1); // C4
  assert.equal(fin(6, 1300, 1000), 7); // C5
  assert.equal(fin(6, 1300, 1250), 8); // C6
  assert.equal(fin(3, 1300, 1000), 3); // C7
});
