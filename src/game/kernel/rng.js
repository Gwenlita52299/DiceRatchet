// kernel/rng.js — SplitMix64（双 u32 表示）+ 命名流（GAME-01 §3）
// 纪律：不得使用 Math.random / BigInt；所有位运算后显式 >>> 0 掩码；64 位乘法用 16 位分片。

export const STREAM_NAMES = Object.freeze([
  'dice.enemy', 'dice.player', 'event', 'forge', 'levelup', 'loot', 'map', 'shop'
]); // 按 name 升序（不变式 4）

const TWO32 = 4294967296;

function u32(x) { return x >>> 0; }

// 32×32 → 64 位无符号乘法，返回 {hi, lo}
function mulU32(a, b) {
  const a0 = a & 0xffff, a1 = a >>> 16;
  const b0 = b & 0xffff, b1 = b >>> 16;
  const lo0 = a0 * b0;
  const mid1 = a1 * b0;
  const mid2 = a0 * b1;
  const hi0 = a1 * b1;
  const t = lo0 + (mid1 % 65536) * 65536 + (mid2 % 65536) * 65536;
  const lo = t % TWO32;
  const carry = Math.floor(t / TWO32);
  const hi = hi0 + Math.floor(mid1 / 65536) + Math.floor(mid2 / 65536) + carry;
  return { hi: u32(hi % TWO32), lo: u32(lo) };
}

function add64(a, b) {
  const lo = a.lo + b.lo;
  const carry = lo >= TWO32 ? 1 : 0;
  return { hi: u32(a.hi + b.hi + carry), lo: u32(lo) };
}

function mul64(a, b) {
  const p = mulU32(a.lo, b.lo);
  const cross = u32(Math.imul(a.hi, b.lo) + Math.imul(a.lo, b.hi));
  return { hi: u32(p.hi + cross), lo: p.lo };
}

function xorShr64(a, n) { // a ^ (a >>> n), 0 < n < 32
  const shHi = a.hi >>> n;
  const shLo = u32((a.lo >>> n) | (a.hi << (32 - n)));
  return { hi: u32(a.hi ^ shHi), lo: u32(a.lo ^ shLo) };
}

const GOLDEN = { hi: 0x9e3779b9, lo: 0x7f4a7c15 };
const M1 = { hi: 0xbf58476d, lo: 0x1ce4e5b9 };
const M2 = { hi: 0x94d049bb, lo: 0x133111eb };

// 推进一步：返回 {state, z}
export function splitmix64(state) {
  const s = add64(state, GOLDEN);
  let z = xorShr64(s, 30);
  z = mul64(z, M1);
  z = xorShr64(z, 27);
  z = mul64(z, M2);
  z = xorShr64(z, 31);
  return { state: s, z };
}

// xmur3 字符串哈希 → 生成器
export function xmur3(str) {
  let h = u32(1779033703 ^ str.length);
  for (let i = 0; i < str.length; i++) {
    h = u32(Math.imul(h ^ str.charCodeAt(i), 3432918353));
    h = u32((h << 13) | (h >>> 19));
  }
  return function next() {
    h = u32(Math.imul(h ^ (h >>> 16), 2246822507));
    h = u32(Math.imul(h ^ (h >>> 13), 3266489909));
    h = u32(h ^ (h >>> 16));
    return h;
  };
}

export function deriveSeed(rootSeed, streamName) {
  const g = xmur3(streamName);
  const h1 = g(), h2 = g(), h3 = g(), h4 = g();
  const init = { hi: u32(h1 ^ rootSeed), lo: u32(h2 ^ h3 ^ h4) };
  const { z } = splitmix64(init);
  return { s0: z.hi, s1: z.lo };
}

export function createRngState(rootSeed) {
  return STREAM_NAMES.map((name) => {
    const { s0, s1 } = deriveSeed(u32(rootSeed), name);
    return { name, counter: 0, s0, s1 };
  });
}

function streamOf(state, name) {
  const s = state.rng.find((r) => r.name === name);
  if (!s) {
    const err = new Error(`unknown rng stream ${name}`);
    err.code = 'E_KERNEL_RNG_STREAM_MISSING';
    throw err;
  }
  return s;
}

// 以下函数对 draft state 原地推进（reduce 内部对克隆后的 draft 操作，外部入参不受影响）
export function nextU32(state, name) {
  const s = streamOf(state, name);
  const r = splitmix64({ hi: s.s0, lo: s.s1 });
  s.s0 = r.state.hi;
  s.s1 = r.state.lo;
  s.counter += 1;
  return r.z.hi;
}

export function randint(state, name, lo, hi) {
  const range = hi - lo + 1;
  if (range <= 0) throw Object.assign(new Error('randint empty range'), { code: 'E_KERNEL_RNG_RANGE' });
  const limit = Math.floor(TWO32 / range) * range;
  let x = nextU32(state, name);
  while (x >= limit) x = nextU32(state, name);
  return lo + (x % range);
}

export function choice(state, name, seq) {
  if (!seq.length) throw Object.assign(new Error('choice on empty'), { code: 'E_KERNEL_RNG_RANGE' });
  return seq[randint(state, name, 0, seq.length - 1)];
}

export function shuffle(state, name, seq) {
  const a = seq.slice();
  for (let i = a.length - 1; i >= 1; i--) {
    const j = randint(state, name, 0, i);
    const t = a[i]; a[i] = a[j]; a[j] = t;
  }
  return a;
}

// 加权选择：items = [{key, weight}]（按 key 升序后抽，避免依赖插入序）
export function weighted(state, name, items) {
  const sorted = items.slice().sort((x, y) => (x.key < y.key ? -1 : x.key > y.key ? 1 : 0));
  const total = sorted.reduce((s, it) => s + it.weight, 0);
  let r = randint(state, name, 1, total);
  for (const it of sorted) {
    r -= it.weight;
    if (r <= 0) return it.key;
  }
  return sorted[sorted.length - 1].key;
}

// 无放回抽 k 个
export function sample(state, name, seq, k) {
  const pool = seq.slice();
  const out = [];
  while (out.length < k && pool.length) {
    const i = randint(state, name, 0, pool.length - 1);
    out.push(pool[i]);
    pool.splice(i, 1);
  }
  return out;
}
