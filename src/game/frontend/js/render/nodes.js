// render/nodes.js — 商店 / 事件 / 宝箱 / 锻造 / 神殿 / 休息 / 遗物替换
import { h, sch, dieCard, facesGrid, RELIC_ICONS, SCHOOL_NAMES, KIND_ICONS } from '../common.js';
import { store, send, legalOf, render } from '../main.js';

function page(title, ...children) {
  return h('div', { class: 'screen col' }, h('h2', {}, title), ...children);
}

export function renderShop() {
  const s = store.obs.shop;
  const gold = store.obs.player.starShards;
  const dice = s.items.filter((i) => i.kind === 'die');
  const relics = s.items.filter((i) => i.kind === 'relic');
  const removable = new Set(s.removable);
  return page('🛒 商店',
    h('div', { class: 'muted' }, '本章敌人学派的克星：', sch(s.counterSchool), '（标注「克制」的骰子对本章敌人更有效，但也会改变你的主学派）'),
    h('h3', {}, '技能骰'),
    h('div', { class: 'cards' }, dice.map((it) => dieCard(it, {
      class: it.sold ? 'sold' : '',
      footer: h('div', { class: 'row' },
        it.school === s.counterSchool ? h('span', { class: 'tag-counter' }, '克制本章敌人') : null,
        h('span', { class: 'grow' }),
        h('button', { class: 'primary', disabled: it.sold || !legalOf('shop_buy', (a) => a.itemId === it.itemId).length, onclick: () => send({ kind: 'shop_buy', itemId: it.itemId }) },
          it.sold ? '已售出' : `✪ ${it.price} 购买`))
    }))),
    relics.length ? h('h3', {}, '遗物') : null,
    relics.length ? h('div', { class: 'cards' }, relics.map((it) => h('div', { class: `card ${it.sold ? 'sold' : ''}` },
      h('div', { class: 'hd' }, h('b', {}, RELIC_ICONS[it.rarity], ' ', it.name), h('span', { class: 'muted' }, it.rarity)),
      h('div', {}, it.desc),
      h('div', { class: 'row' }, h('span', { class: 'grow' }), h('button', { class: 'primary', disabled: it.sold || !legalOf('shop_buy', (a) => a.itemId === it.itemId).length, onclick: () => send({ kind: 'shop_buy', itemId: it.itemId }) }, it.sold ? '已售出' : `✪ ${it.price} 购买`))))) : null,
    h('h3', {}, `删除骰子（✪ ${s.removePrice}）`, h('span', { class: 'muted', style: { fontSize: '13px', fontWeight: 400 } }, '  骰池至少保留 5 颗；厄咒骰只能通过事件移除')),
    h('div', { class: 'row wrap' }, store.obs.deck.map((d) => h('button', {
      class: 'small', disabled: !removable.has(d.dieId) || gold < s.removePrice, title: d.faces.map((f) => f.label).join(' / '),
      onclick: () => send({ kind: 'shop_remove', dieId: d.dieId })
    }, `✕ ${d.name}`, h('span', { class: `sch-${d.school}` }, ` ${SCHOOL_NAMES[d.school]}`)))),
    h('div', { class: 'row' },
      h('button', { disabled: !legalOf('shop_refresh').length, onclick: () => send({ kind: 'shop_refresh' }) }, `⟳ 刷新（✪ ${s.refreshPrice}，剩 ${s.refreshLeft} 次）`),
      h('button', { class: 'primary', onclick: () => send({ kind: 'shop_leave' }) }, '离开商店 →')));
}

export function renderEvent() {
  const ev = store.obs.event;
  return page(`❓ ${ev.title}`,
    ev.school ? h('div', {}, ev.school === 'all' ? '三系试炼' : sch(ev.school)) : null,
    h('div', { class: 'panel event-text' }, ev.text),
    ev.resolved
      ? h('div', { class: 'col' }, h('div', { class: 'panel event-text', style: { borderColor: 'var(--gold)' } }, ev.resultText),
        h('div', {}, h('button', { class: 'primary', onclick: () => send({ kind: 'shop_leave' }) }, '继续 →')))
      : h('div', { class: 'col', style: { maxWidth: '820px' } }, ev.options.map((o) => h('button', {
        class: `option ${o.risk}`, disabled: !o.affordable, onclick: () => send({ kind: 'event_choose', optionId: o.optionId })
      }, o.label, o.requirement ? h('span', { class: 'muted' }, `  （${o.requirement}）`) : null))));
}

export function renderTreasure() {
  const t = store.obs.treasure;
  return page('🎁 宝箱：三选一',
    h('div', { class: 'muted' }, '两颗贴合你的构筑，一颗克制本章敌人（', sch(t.counterSchool), '）。选择克制，还是坚持构筑？'),
    h('div', { class: 'cards' }, t.offers.map((d) => dieCard(d, {
      onclick: () => send({ kind: 'treasure_pick', defId: d.defId }),
      footer: h('div', { class: 'row' }, d.counter ? h('span', { class: 'tag-counter' }, '克制本章敌人') : h('span', { class: 'muted' }, '构筑向'), h('span', { class: 'grow' }), h('button', { class: 'primary' }, '选择'))
    }))),
    h('div', {}, h('button', { onclick: () => send({ kind: 'treasure_pick', defId: 'skip' }) }, `跳过（✪ +${t.skipGold}）`)));
}

export function renderForge() {
  const f = store.obs.forge;
  const ui = store.ui.forge;
  if (f.done) {
    return page('⚒ 锻造', h('div', { class: 'panel' }, '锻造完成。'), h('div', {}, h('button', { class: 'primary', onclick: () => send({ kind: 'shop_leave' }) }, '离开 →')));
  }
  const deck = store.obs.deck.filter((d) => f.forgeable.includes(d.dieId));
  const die = deck.find((d) => d.dieId === ui.dieId);
  const canPick = die && ui.faceIndex !== undefined && ui.faceIndex !== null && ui.candidateId;
  return page('⚒ 锻造：选骰 → 选面 → 5 选 1 替换',
    h('div', { class: 'muted' }, f.price ? `费用 ✪ ${f.price}` : '本次锻造免费（已预付）', !f.affordable ? h('span', { style: { color: 'var(--danger)' } }, ' · 星屑不足') : null),
    h('h3', {}, '1. 选择骰子'),
    h('div', { class: 'row wrap' }, deck.map((d) => h('button', { class: d.dieId === ui.dieId ? 'primary small' : 'small', onclick: () => { ui.dieId = d.dieId; ui.faceIndex = null; render(); } }, d.name, d.forged ? ' ⚒' : ''))),
    die ? h('h3', {}, '2. 选择要替换的骰面') : null,
    die ? facesGrid(die.faces, { selected: ui.faceIndex, onPick: (i) => { ui.faceIndex = i; render(); } }) : null,
    h('h3', {}, '3. 选择候选面'),
    h('div', { class: 'row wrap' }, f.candidates.map((c) => h('button', {
      class: ui.candidateId === c.candidateId ? 'primary' : '', onclick: () => { ui.candidateId = c.candidateId; render(); }
    }, `${KIND_ICONS[c.kind] || ''} ${c.label}`, c.selfCorruption ? `（自损 ${c.selfCorruption}）` : '', ' ', h('span', { class: `sch-${c.school}` }, SCHOOL_NAMES[c.school])))),
    h('div', { class: 'row' },
      h('button', {
        class: 'primary', disabled: !canPick || !f.affordable,
        onclick: () => { send({ kind: 'forge_pick', dieId: ui.dieId, faceIndex: ui.faceIndex, candidateId: ui.candidateId }); store.ui.forge = {}; }
      }, '锻造'),
      h('button', { onclick: () => { store.ui.forge = {}; send({ kind: 'shop_leave' }); } }, '离开')));
}

export function renderChoice(kind) {
  const n = store.obs[kind];
  const title = kind === 'shrine' ? '⛩ 神殿：择一祈愿' : '🔥 休息点';
  const kindAct = kind === 'shrine' ? 'shrine_choose' : 'rest_choose';
  return page(title, h('div', { class: 'col', style: { maxWidth: '600px' } }, n.options.map((o) => h('button', {
    class: 'option positive', disabled: !o.available, onclick: () => send({ kind: kindAct, optionId: o.optionId })
  }, o.label, !o.available ? h('span', { class: 'muted' }, '（不可用）') : null))));
}

export function renderRelicSwap() {
  const o = store.obs;
  const pr = o.pendingRelic;
  return page('遗物栏已满',
    h('div', { class: 'panel' }, '新遗物：', h('b', {}, pr.name), ' — ', pr.desc),
    h('div', { class: 'muted' }, '选择要丢弃的遗物，或放弃新遗物：'),
    h('div', { class: 'cards' }, o.relics.map((r) => h('div', { class: 'card' },
      h('div', { class: 'hd' }, h('b', {}, RELIC_ICONS[r.rarity], ' ', r.name)), h('div', {}, r.desc),
      h('button', { class: 'danger', onclick: () => send({ kind: 'relic_swap', relicId: r.relicId }) }, '丢弃它，换新遗物')))),
    h('div', {}, h('button', { onclick: () => send({ kind: 'relic_swap', relicId: 'skip' }) }, '放弃新遗物')));
}
