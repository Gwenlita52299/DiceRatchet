// common.js — DOM 辅助、文案与占位美术（只做展示，不含任何规则判定）

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === 'html') el.innerHTML = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export const SCHOOL_NAMES = { blade: '锋刃', arcana: '秘法', verdant: '自然', neutral: '中立' };
export const SCHOOL_ICONS = { blade: '🗡', arcana: '✦', verdant: '❦', neutral: '◇' };
export const RISK_NAMES = { safe: '安全', balanced: '均衡', risky: '冒险', cursed: '厄咒' };
export const STATUS_NAMES = { poison: '中毒', burn: '灼烧', weak: '虚弱', strength: '力量', regen: '再生' };
export const STATUS_ICONS = { poison: '☠', burn: '🔥', weak: '↓', strength: '💪', regen: '✚' };
export const KIND_ICONS = {
  purge: '⚔', ward: '🛡', mend: '✚', status: '✧', selfCorruption: '☣', draw: '🂠', reroll: '⟳', modify: '±', gold: '✪', nothing: '·', duplicate_face: '⎘',
  heal: '✚', add_curse: '☣', disable_slot: '⛓', drain_hand: '🂠'
};
export const NODE_INFO = {
  battle: ['⚔', '战斗'], elite: ['☠', '精英'], boss: ['👑', 'Boss'], shop: ['🛒', '商店'], forge: ['⚒', '锻造'],
  event: ['?', '事件'], treasure: ['🎁', '宝箱'], shrine: ['⛩', '神殿'], rest: ['🔥', '休息']
};
export const PIPS = ['⚀', '⚁', '⚂', '⚃', '⚄', '⚅'];
export const MONSTER_ICONS = { blade: '👹', arcana: '👁', verdant: '🌿', neutral: '🗿' };
export const RELIC_ICONS = { common: '🔹', rare: '🔶', boss: '👑' };
export const ARROW_TEXT = { up: '↑克你', down: '↓被你克', eq: '=' };

export function sch(school) {
  return h('span', { class: `sch sch-${school}` }, `${SCHOOL_ICONS[school] || ''} ${SCHOOL_NAMES[school] || school}`);
}

export function arrow(a, text) {
  const t = text || { up: '↑', down: '↓', eq: '=' }[a];
  return h('span', { class: `arrow-${a}` }, t);
}

export function mulText(p) {
  const v = p / 1000;
  return `×${v.toFixed(2).replace(/0$/, '')}`;
}

export function faceShort(f) {
  let t = f.label;
  if (f.selfCorruption && f.kind !== 'selfCorruption') t += ` (自损${f.selfCorruption})`;
  return t;
}

export function facesGrid(faces, opts = {}) {
  return h('div', { class: 'faces' }, faces.map((f, i) => h('div', {
    class: `face ${f.selfCorruption || f.kind === 'nothing' ? 'bad' : ''} ${opts.onPick ? 'selectable' : ''} ${opts.selected === i ? 'sel' : ''}`,
    title: `${f.label} · ${SCHOOL_NAMES[f.school]}`,
    onclick: opts.onPick ? () => opts.onPick(i) : null
  }, h('div', { class: 'p' }, PIPS[i]), h('div', { class: `sch-${f.school}` }, KIND_ICONS[f.kind] || ''), faceShort(f))));
}

export function dieCard(d, extra = {}) {
  return h('div', { class: `card risk-${d.riskTier} ${extra.class || ''}`, onclick: extra.onclick || null },
    h('div', { class: 'hd' }, h('b', {}, d.name), h('span', { class: 'row', style: { gap: '6px' } }, sch(d.school), h('span', { class: 'muted', style: { fontSize: '12px', color: 'var(--rc)' } }, RISK_NAMES[d.riskTier]))),
    facesGrid(d.faces, extra.facesOpts || {}),
    extra.footer || null);
}

export function statusChips(list) {
  return h('div', { class: 'statuses' }, list.map((s) => h('span', { class: 'status', title: STATUS_NAMES[s.kind] }, `${STATUS_ICONS[s.kind]} ${STATUS_NAMES[s.kind]} ${s.stacks}`)));
}

let toastTimer = null;
export function toast(msg, kind = 'error') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = `toast ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), 2600);
}

export function openModal(content) {
  const m = document.getElementById('modal');
  const body = m.querySelector('.modal-body');
  body.innerHTML = '';
  body.appendChild(content);
  m.classList.remove('hidden');
}

export function closeModal() {
  document.getElementById('modal').classList.add('hidden');
}
