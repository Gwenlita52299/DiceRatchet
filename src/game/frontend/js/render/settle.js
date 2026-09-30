// render/settle.js — 结算页
import { h } from '../common.js';
import { store, go, newGame } from '../main.js';

export function renderSettle() {
  const o = store.obs;
  const win = o.outcome === 'win';
  const s = o.stats;
  let last = null;
  try { last = JSON.parse(localStorage.getItem('rsi.lastConfig') || 'null'); } catch { last = null; }
  return h('div', { class: 'settle screen' },
    h('h1', {}, win ? '🏆 净化完成' : '💀 旅途终结'),
    h('div', { class: 'muted' }, win ? '异常原点已被净化，世界重归平静。' : (o.outcomeReason === 'abandon' ? '你放弃了本局。' : `你倒在了第 ${o.stage} 关。`)),
    h('div', { class: 'stat-grid' },
      [['到达关卡', `${o.stage}/10`], ['总回合', s.turns], ['造成伤害', s.damageDealt], ['击杀', s.kills],
        ['克制触发', s.counterHits], ['技能骰使用', s.skillFaceUses], ['战斗胜/负', `${s.battlesWon}/${s.battlesLost}`], ['剩余星屑', o.player.starShards]]
        .map(([k, v]) => h('div', { class: 'stat' }, h('b', {}, String(v)), h('span', { class: 'muted' }, k)))),
    h('div', { class: 'row', style: { justifyContent: 'center' } },
      last ? h('button', { class: 'primary', onclick: () => newGame({ ...last, seed: undefined }) }, '同配置再来一局') : null,
      h('button', { onclick: () => go('setup') }, '重新配置'),
      h('button', { onclick: () => { localStorage.removeItem('rsi.session'); go('menu'); } }, '主菜单')));
}
