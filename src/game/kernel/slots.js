// kernel/slots.js — 槽约束 DSL 与槽/骰面兼容表（GAME-00 §4.3）

function syntaxError(text) {
  const e = new Error(`bad constraint: ${text}`);
  e.code = 'E_KERNEL_CONSTRAINT_SYNTAX';
  return e;
}

/** any | odd | even | (ge|gt|le|lt|eq):int | in:int,int,... */
export function parseConstraint(text) {
  if (typeof text !== 'string') throw syntaxError(text);
  if (text === 'any' || text === 'odd' || text === 'even') return { op: text };
  const m = /^(ge|gt|le|lt|eq):(-?\d+)$/.exec(text);
  if (m) return { op: m[1], n: parseInt(m[2], 10) };
  const m2 = /^in:(\d+(?:,\d+)*)$/.exec(text);
  if (m2) return { op: 'in', set: m2[1].split(',').map((x) => parseInt(x, 10)) };
  throw syntaxError(text);
}

export function checkConstraint(text, pip) {
  const c = parseConstraint(text);
  switch (c.op) {
    case 'any': return true;
    case 'odd': return pip % 2 === 1;
    case 'even': return pip % 2 === 0;
    case 'ge': return pip >= c.n;
    case 'gt': return pip > c.n;
    case 'le': return pip <= c.n;
    case 'lt': return pip < c.n;
    case 'eq': return pip === c.n;
    case 'in': return c.set.includes(pip);
    default: throw syntaxError(text);
  }
}

export function describeConstraint(text) {
  const c = parseConstraint(text);
  switch (c.op) {
    case 'any': return '任意点数';
    case 'odd': return '需要奇数';
    case 'even': return '需要偶数';
    case 'ge': return `需要 ≥${c.n}`;
    case 'gt': return `需要 >${c.n}`;
    case 'le': return `需要 ≤${c.n}`;
    case 'lt': return `需要 <${c.n}`;
    case 'eq': return `需要 =${c.n}`;
    case 'in': return `需要 ${c.set.join('/')}`;
    default: return text;
  }
}

/** slotKind → 可接受的 face.kind；null 表示任意 */
export const SLOT_ACCEPTS = Object.freeze({
  attack: ['purge'],
  defense: ['ward'],
  heal: ['mend'],
  utility: null,
  cursed: ['selfCorruption']
});

export const SLOT_KIND_NAMES = Object.freeze({ attack: '攻击', defense: '防御', heal: '恢复', utility: '工具', cursed: '厄咒' });

export function slotAccepts(slotKind, faceKind) {
  const acc = SLOT_ACCEPTS[slotKind];
  if (!(slotKind in SLOT_ACCEPTS)) return false;
  return acc === null || acc.includes(faceKind);
}
