// render/hud.js — 顶部状态栏（局内通用）
import { h, sch, openModal, dieCard, RELIC_ICONS, SCHOOL_NAMES } from '../common.js';
import { store, send, go } from '../main.js';

export function showDeck() {
  const o = store.obs;
  openModal(h('div', { class: 'col' },
    h('h2', {}, `骰池（${o.deck.length} 颗）`),
    h('div', { class: 'muted' }, '主学派：', sch(o.player.deckSchool), `  集中度 ${Math.floor(o.player.deckConcentrationPermille / 10)}%  ·  `,
      Object.entries(o.player.deckSchoolCounts).map(([k, v]) => `${SCHOOL_NAMES[k]} ${v}`).join(' / ')),
    h('div', { class: 'cards' }, o.deck.map((d) => dieCard(d)))));
}

export function renderHud() {
  const o = store.obs;
  const p = o.player;
  const hearts = '♥'.repeat(Math.max(0, p.hearts)) + '♡'.repeat(Math.max(0, p.heartsMax - p.hearts));
  const inBattle = o.phase === 'player_turn';
  return h('div', { class: 'hud' },
    h('span', { class: 'title' }, `第 ${o.stage}/10 关`),
    h('span', { class: 'muted' }, `Ch${o.chapter} · ${o.chapterName}`),
    h('span', { class: 'muted' }, { easy: '简单', medium: '中等', hard: '困难' }[o.difficulty]),
    h('span', { class: 'sep' }),
    h('span', { class: 'hearts', title: '心（局内生命）' }, hearts),
    inBattle ? h('span', { class: 'row', style: { gap: '6px' }, 'data-target': 'player' },
      '净化', h('div', { class: 'bar' }, h('i', { style: { width: `${Math.max(0, Math.min(100, (p.virtue / p.virtueMax) * 100))}%` } }), h('span', {}, `${p.virtue}/${p.virtueMax}`)),
      p.ward ? h('span', { title: '光盾' }, `🛡 ${p.ward}`) : null) : h('span', { class: 'muted' }, `净化上限 ${p.virtueMax}`),
    h('span', { class: 'gold' }, `✪ ${p.starShards}`),
    h('span', { class: 'sep' }),
    h('span', {}, '主学派 ', sch(p.deckSchool)),
    h('span', { class: 'row', style: { gap: '4px' } }, o.toggles.relics
      ? [h('span', { class: 'muted', style: { fontSize: '12px' } }, `遗物 ${o.relics.length}/${o.config.relicSlots}`),
        ...o.relics.map((r) => h('span', { class: 'relic-chip', title: `${r.name}：${r.desc}` }, RELIC_ICONS[r.rarity], r.name))]
      : null),
    h('span', { class: 'grow' }),
    h('button', { class: 'small', onclick: showDeck }, `🎲 骰池 ${o.deck.length}`),
    h('button', { class: 'small', onclick: () => go('menu') }, '菜单'),
    h('button', { class: 'small danger', onclick: () => { if (confirm('确定放弃本局？')) send({ kind: 'abandon_run' }); } }, '放弃'));
}
