// kernel/counter.js — 学派克制方阵（GAME-00 §5.1 / GAME-01 §9.1）

export const SCHOOLS = Object.freeze(['blade', 'arcana', 'verdant']);
export const BEATS = Object.freeze({ blade: 'verdant', arcana: 'blade', verdant: 'arcana' });

/** 返回千分倍率。config.toggles.schoolCounter=false 时恒 1000。 */
export function schoolMulPermille(config, atk, def, bonusPermille = 0) {
  if (!config.toggles.schoolCounter) return 1000;
  if (!BEATS[atk] || !BEATS[def] || atk === def) return 1000;
  if (BEATS[atk] === def) return config.counterMulPermille.advantage + bonusPermille;
  if (BEATS[def] === atk) return config.counterMulPermille.disadvantage;
  return 1000;
}

export function arrowOf(mul) {
  return mul > 1000 ? 'up' : mul < 1000 ? 'down' : 'eq';
}

/** 克制 school 的学派（谁能打它） */
export function counterOf(school) {
  for (const s of SCHOOLS) if (BEATS[s] === school) return s;
  return null;
}

/** 按倍率缩放，floor；攻击向 minOne */
export function applyMul(value, mulPermille) {
  return Math.floor((value * mulPermille) / 1000);
}
