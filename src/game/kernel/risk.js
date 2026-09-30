// kernel/risk.js — 骰面风险判定（纯函数，loader 与内核共用）

export function faceHasSelfCorruption(face) {
  return face.kind === 'selfCorruption' || (face.selfCorruption | 0) > 0;
}

export function selfCorruptionValue(face) {
  if (face.kind === 'selfCorruption') return face.value;
  return face.selfCorruption | 0;
}

/**
 * 风险等级自动判定（决策 D-09，见 docs/DECISIONS.md）：
 * 不利面 = selfCorruption 面或 nothing 面；0 → safe，1–2 → balanced，≥3 → risky；cursed 类恒为 cursed。
 */
export function computeRiskTier(def) {
  if (def.category === 'cursed') return 'cursed';
  const bad = def.faces.filter((f) => faceHasSelfCorruption(f) || f.kind === 'nothing').length;
  if (bad === 0) return 'safe';
  if (bad <= 2) return 'balanced';
  return 'risky';
}
