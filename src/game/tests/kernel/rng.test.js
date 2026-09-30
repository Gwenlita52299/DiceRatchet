import test from 'node:test';
import assert from 'node:assert/strict';
import { createRngState, randint, shuffle, nextU32, deriveSeed, STREAM_NAMES, splitmix64 } from '../../kernel/rng.js';

const mk = (seed) => ({ rng: createRngState(seed) });

test('SplitMix64 与参考实现（BigInt）一致', () => {
  const M = (1n << 64n) - 1n;
  let s = 0x0123456789abcdefn;
  let st = { hi: 0x01234567, lo: 0x89abcdef };
  for (let i = 0; i < 50; i++) {
    s = (s + 0x9e3779b97f4a7c15n) & M;
    let z = s;
    z = ((z ^ (z >> 30n)) * 0xbf58476d1ce4e5b9n) & M;
    z = ((z ^ (z >> 27n)) * 0x94d049bb133111ebn) & M;
    z = z ^ (z >> 31n);
    const r = splitmix64(st);
    st = r.state;
    assert.equal(r.z.hi, Number(z >> 32n));
    assert.equal(r.z.lo, Number(z & 0xffffffffn));
  }
});

test('同 seed 同流 → 同序列；不同流互相独立', () => {
  const a = mk(42), b = mk(42);
  const xs = Array.from({ length: 20 }, () => nextU32(a, 'map'));
  nextU32(b, 'dice.player'); // 消费别的流不影响 map 流
  const ys = Array.from({ length: 20 }, () => nextU32(b, 'map'));
  assert.deepEqual(xs, ys);
  assert.notDeepEqual(deriveSeed(42, 'map'), deriveSeed(42, 'shop'));
});

test('rng 数组按流名升序且计数单调', () => {
  const s = mk(1);
  assert.deepEqual(s.rng.map((r) => r.name), [...STREAM_NAMES].sort());
  randint(s, 'loot', 1, 6);
  assert.ok(s.rng.find((r) => r.name === 'loot').counter >= 1);
});

test('randint 闭区间、覆盖全部取值', () => {
  const s = mk(7);
  const seen = new Set();
  for (let i = 0; i < 600; i++) {
    const v = randint(s, 'dice.player', 1, 6);
    assert.ok(v >= 1 && v <= 6 && Number.isInteger(v));
    seen.add(v);
  }
  assert.equal(seen.size, 6);
});

test('shuffle 是排列且不改原数组', () => {
  const s = mk(9);
  const src = [1, 2, 3, 4, 5, 6, 7, 8];
  const out = shuffle(s, 'shop', src);
  assert.deepEqual([...out].sort(), src);
  assert.deepEqual(src, [1, 2, 3, 4, 5, 6, 7, 8]);
});

test('未知流名抛 E_KERNEL_RNG_STREAM_MISSING', () => {
  assert.throws(() => nextU32(mk(1), 'nope'), (e) => e.code === 'E_KERNEL_RNG_STREAM_MISSING');
});
