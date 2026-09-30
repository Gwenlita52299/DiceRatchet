// render/menu.js — 主菜单 / 开局配置（由 /api/config/schema 动态渲染）/ 图鉴
import { h, sch, toast, facesGrid, RISK_NAMES, SCHOOL_NAMES } from '../common.js';
import { api } from '../net.js';
import { store, newGame, resume, go } from '../main.js';
import { toggleSound, soundOn } from '../audio.js';

export function renderMenu() {
  return h('div', { class: 'menu' },
    h('h1', {}, '破咒骰'),
    h('div', { class: 'sub' }, '掷骰 · 入槽 · 以学派克制压制强敌 —— 10 关骰子 roguelike'),
    h('button', { class: 'primary', onclick: () => go('setup') }, '开始新局'),
    h('button', { disabled: !store.sessionId, onclick: () => resume() }, '继续'),
    h('button', { onclick: () => go('codex') }, '图鉴'),
    h('button', { onclick: (e) => { const on = toggleSound(); e.target.textContent = `音效：${on ? '开' : '关'}`; } }, `音效：${soundOn() ? '开' : '关'}`),
    h('div', { class: 'muted', style: { marginTop: '30px', fontSize: '13px', textAlign: 'center', lineHeight: '1.8' } },
      '学派克制：', sch('blade'), ' → ', sch('verdant'), ' → ', sch('arcana'), ' → ', sch('blade'), h('br'),
      '战斗快捷键：', h('kbd', {}, '1–8'), ' 选骰 · ', h('kbd', {}, 'Q W E R T'), ' 选槽 · ', h('kbd', {}, '空格'), ' 结束回合 · ', h('kbd', {}, 'Esc'), ' 取消'));
}

const DIFF_ROWS = [
  ['heartsMax', '心数'], ['virtueMax', '净化上限'], ['drawPerTurn', '每回合抽骰'], ['handMax', '手骰上限'],
  ['enemyHpMulPermille', '敌血', (v) => `×${v / 1000}`], ['enemyDmgMulPermille', '敌伤', (v) => `×${v / 1000}`],
  ['enemyExtraDice', '敌额外意图骰', (v) => `+${v}`], ['counterMulPermille', '克制强度', (v) => `×${v.advantage / 1000} / ×${v.disadvantage / 1000}`],
  ['monoPenaltyPermille', '单系惩罚', (v) => `+${v / 10}%`], ['eliteStages', '精英关', (v) => v.join(',')],
  ['purifyPerStage', '每场净化次数'], ['relicSlots', '遗物栏'], ['reviveOnce', '首次失心复苏', (v) => (v ? '有' : '无')]
];

export function renderSetup() {
  const schema = store.schema;
  if (!schema) return h('div', { class: 'loading' }, '配置加载失败，请确认后端已启动');
  let last = {};
  try { last = JSON.parse(localStorage.getItem('rsi.lastConfig') || '{}'); } catch { last = {}; }
  const cfg = {
    difficulty: last.difficulty || schema.defaults.difficulty,
    character: schema.defaults.character,
    toggles: { ...schema.defaults.toggles, ...(last.toggles || {}) },
    seed: ''
  };
  const root = h('div', { class: 'screen col' });
  const diffBox = h('div', { class: 'diff-cards' });
  const drawDiff = () => {
    diffBox.replaceChildren(...schema.difficulties.map((d) => h('div', {
      class: `diff-card ${cfg.difficulty === d.id ? 'sel' : ''}`, onclick: () => { cfg.difficulty = d.id; drawDiff(); }
    }, h('h3', {}, d.label, ' ', h('span', { class: 'muted', style: { fontSize: '13px' } }, d.id)),
    h('table', {}, DIFF_ROWS.map(([k, label, fmt]) => h('tr', { title: schema.difficultyKeys[k] || '' }, h('td', {}, label), h('td', {}, fmt ? fmt(d[k]) : String(d[k]))))))));
  };
  drawDiff();
  const toggles = h('div', { class: 'toggle-list' }, schema.toggles.map((t) => {
    const cb = h('input', { type: 'checkbox' });
    cb.checked = cfg.toggles[t.id];
    cb.addEventListener('change', () => { cfg.toggles[t.id] = cb.checked; });
    return h('label', { class: 'toggle' }, cb, h('div', {}, h('b', {}, t.label), h('div', { class: 'muted', style: { fontSize: '12px' } }, `开：${t.on}`), h('div', { class: 'muted', style: { fontSize: '12px' } }, `关：${t.off}`)));
  }));
  const seedIn = h('input', { type: 'number', placeholder: '留空则随机', style: { width: '200px' } });
  const charSel = h('select', {}, schema.characters.map((c) => h('option', { value: c.id }, `${c.name} — ${c.desc}`)));
  root.append(
    h('div', { class: 'row' }, h('button', { onclick: () => go('menu') }, '← 返回'), h('h2', { style: { margin: 0 } }, '开局配置')),
    h('h3', {}, '难度'), diffBox,
    h('h3', {}, '系统开关', h('span', { class: 'muted', style: { fontSize: '13px', fontWeight: 400 } }, '  （全部关闭 = 纯战斗线性闯关）')), toggles,
    h('div', { class: 'row wrap' },
      h('div', { class: 'col', style: { gap: '4px' } }, h('b', {}, '种子'), h('div', { class: 'row' }, seedIn,
        h('button', { onclick: async () => { const r = await api('POST', '/seed', { difficulty: cfg.difficulty }); if (r.ok) seedIn.value = r.data.seed; else toast(r.error.message); } }, '🎲 随机'))),
      h('div', { class: 'col', style: { gap: '4px' } }, h('b', {}, '角色'), charSel)),
    h('div', {}, h('button', {
      class: 'primary', style: { padding: '12px 36px', fontSize: '16px' },
      onclick: () => newGame({ difficulty: cfg.difficulty, character: charSel.value, toggles: cfg.toggles, seed: seedIn.value === '' ? undefined : Number(seedIn.value) })
    }, '开始冒险 →')));
  return root;
}

export function renderCodex() {
  const root = h('div', { class: 'screen col' }, h('div', { class: 'row' }, h('button', { onclick: () => go('menu') }, '← 返回'), h('h2', { style: { margin: 0 } }, '图鉴')));
  const body = h('div', { class: 'col' }, h('div', { class: 'loading' }, '加载中…'));
  root.append(body);
  Promise.all(['skills', 'monsters', 'relics'].map((t) => api('GET', `/content/${t}`))).then(([sk, mo, re]) => {
    if (!sk.ok) { body.replaceChildren(h('div', {}, '加载失败')); return; }
    const skills = Object.values(sk.data);
    body.replaceChildren(
      h('h3', {}, `技能骰（${skills.length}）`),
      h('div', { class: 'cards' }, skills.map((d) => h('div', { class: `card risk-${d.riskTier}` },
        h('div', { class: 'hd' }, h('b', {}, d.name), h('span', {}, sch(d.school), ' ', h('span', { style: { color: 'var(--rc)', fontSize: '12px' } }, RISK_NAMES[d.riskTier]))),
        facesGrid(d.faces.map((f) => ({ ...f, selfCorruption: f.kind === 'selfCorruption' ? f.value : (f.selfCorruption || 0) })))))),
      h('h3', {}, '怪物'),
      h('table', { class: 'codex' }, h('tr', {}, h('th', {}, '名称'), h('th', {}, '阶'), h('th', {}, '学派'), h('th', {}, '魔化值'), h('th', {}, '说明')),
        Object.values(mo.data).map((m) => h('tr', {}, h('td', {}, m.name), h('td', {}, m.tier), h('td', {}, m.phases ? m.phases.map((p) => SCHOOL_NAMES[p.school]).join(' → ') : SCHOOL_NAMES[m.school]), h('td', {}, m.maxCorruption), h('td', { class: 'muted' }, m.taunt)))),
      h('h3', {}, '遗物'),
      h('table', { class: 'codex' }, Object.values(re.data).map((r) => h('tr', {}, h('td', {}, r.name), h('td', {}, r.rarity), h('td', { class: 'muted' }, r.desc)))));
  });
  return root;
}
