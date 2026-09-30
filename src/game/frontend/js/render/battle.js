// render/battle.js — 战斗页（GAME-00 §10.2）：拖放 + 点击 + 键盘三路径；伤害预览与合法性提示均来自内核 hints
import { h, sch, arrow, statusChips, toast, facesGrid, mulText, openModal, PIPS, KIND_ICONS, STATUS_NAMES, MONSTER_ICONS, SCHOOL_NAMES, RISK_NAMES, ARROW_TEXT } from '../common.js';
import { store, send, legalOf, render } from '../main.js';
import { animState, stop as stopAnim } from '../anim.js';
import { play } from '../audio.js';

const SLOT_KEYS = ['Q', 'W', 'E', 'R', 'T', 'Y', 'U'];
const BATTLE_KIND = { battle: '普通战斗', elite: '精英战斗', boss: 'Boss 战' };

function selectedEnemyId() {
  const o = store.obs;
  if (store.ui.selectedEnemy && o.enemies.find((e) => e.enemyId === store.ui.selectedEnemy)) return store.ui.selectedEnemy;
  return o.enemies[0] ? o.enemies[0].enemyId : null;
}

function intentChip(it) {
  let body;
  if (it.kind === 'purge') {
    body = [`${KIND_ICONS.purge} ${it.value}${it.hits > 1 ? `×${it.hits}` : ''} `, arrow(it.arrow, ARROW_TEXT[it.arrow])];
  } else if (it.kind === 'status') {
    body = `${it.target === 'self' ? '自身' : ''}${STATUS_NAMES[it.statusKind]} ${it.value}`;
  } else if (it.kind === 'ward') body = `🛡 ${it.value}`;
  else if (it.kind === 'heal') body = `✚ 回复 ${it.value}`;
  else if (it.kind === 'add_curse') body = '☣ 塞入厄咒骰';
  else if (it.kind === 'disable_slot') body = `⛓ 禁用技能槽 ${it.value} 回合`;
  else if (it.kind === 'drain_hand') body = `🂠 你下回合少抽 ${it.value}`;
  else body = it.kind;
  const tip = it.kind === 'purge' ? `意图骰 ${it.pip} → 基础 ${it.raw}，学派 ${mulText(it.schoolMul)}${it.monoPermille ? `，单系惩罚 +${it.monoPermille / 10}%` : ''}` : `意图骰 ${it.pip}`;
  return h('span', { class: 'intent-chip', title: tip }, h('span', { class: 'muted' }, `${PIPS[it.pip - 1] || it.pip} `), body);
}

function enemyCard(e) {
  const sel = selectedEnemyId() === e.enemyId;
  return h('div', {
    class: `enemy tier-${e.tier} ${sel ? 'sel' : ''}`, 'data-target': e.enemyId,
    onclick: () => { store.ui.selectedEnemy = e.enemyId; render(); }
  },
  h('div', { class: 'portrait', style: { background: `radial-gradient(circle, var(--${e.school}) 0%, transparent 75%)` } }, MONSTER_ICONS[e.school]),
  h('b', {}, e.name), e.tier !== 'normal' ? h('span', { class: 'muted' }, e.tier === 'boss' ? ' 👑' : ' ☠') : null,
  h('div', { style: { margin: '4px 0' } }, sch(e.school), ' ', store.obs.toggles.schoolCounter ? arrow(e.arrowVsYou, ARROW_TEXT[e.arrowVsYou]) : null,
    e.phaseCount > 1 ? h('span', { class: 'muted', style: { fontSize: '12px' } }, ` 阶段 ${e.phase}/${e.phaseCount}`) : null),
  h('div', { class: 'bar hp', style: { width: '150px' } }, h('i', { style: { width: `${(e.hp / e.hpMax) * 100}%` } }), h('span', {}, `${e.hp}/${e.hpMax}`)),
  e.ward ? h('div', { style: { fontSize: '13px', marginTop: '4px' } }, `🛡 ${e.ward}`) : null,
  statusChips(e.statuses),
  h('div', { class: 'intent' }, e.intent.length ? e.intent.map(intentChip) : h('span', { class: 'muted' }, '（无意图）')),
  h('div', { class: 'muted', style: { fontSize: '11px', marginTop: '6px' } }, e.taunt));
}

function previewFor(hint, enemyId) {
  if (!hint || !hint.previews) return null;
  return hint.previews.find((p) => p.enemyId === enemyId) || hint.previews[0];
}

function previewText(pv) {
  if (!pv) return '';
  if (pv.mulPermille === 1000 && (pv.relicMulPermille || 1000) === 1000) return `= ${pv.value}${pv.execute ? '（处决×2）' : ''}`;
  return `${pv.raw} ${mulText(pv.mulPermille)}${pv.relicMulPermille && pv.relicMulPermille !== 1000 ? ` ${mulText(pv.relicMulPermille)}` : ''} = ${pv.value} ${pv.arrow === 'up' ? '↑克' : pv.arrow === 'down' ? '↓被克' : ''}${pv.execute ? '（处决×2）' : ''}`;
}

export function attemptPlay(slotId) {
  const ui = store.ui;
  const dieId = ui.selectedDie;
  if (!dieId) { toast('先选择一颗手骰', 'info'); return; }
  const hint = store.hints && store.hints[dieId] && store.hints[dieId][slotId];
  if (!hint || !hint.legal) { play('ui_deny'); toast(hint ? hint.reason : '无法放置'); return; }
  if (hint.needsTarget === 'die') {
    const others = store.obs.hand.filter((d) => d.dieId !== dieId);
    if (others.length) {
      ui.targetMode = { dieId, slotId };
      render();
      toast('选择要作用的另一颗手骰（Esc 取消）', 'info');
      return;
    }
    send({ kind: 'play_face', dieId, slotId, targetId: null });
    return;
  }
  if (hint.needsTarget === 'enemy') { send({ kind: 'play_face', dieId, slotId, targetId: selectedEnemyId() }); return; }
  send({ kind: 'play_face', dieId, slotId });
}

function clickDie(dieId) {
  const ui = store.ui;
  if (ui.targetMode) {
    if (dieId === ui.targetMode.dieId) { ui.targetMode = null; render(); return; }
    send({ kind: 'play_face', dieId: ui.targetMode.dieId, slotId: ui.targetMode.slotId, targetId: dieId });
    return;
  }
  ui.selectedDie = ui.selectedDie === dieId ? null : dieId;
  render();
}

function endTurn() {
  if (!legalOf('end_turn').length) {
    play('ui_deny');
    const n = store.obs.hand.filter((d) => d.isCorrupt).length;
    toast(`还有 ${n} 颗魔化骰未处理：打出到工具槽 / 弃掉（承受 50%）/ 净化`);
    document.querySelectorAll('.die.corrupt').forEach((el) => { el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash'); });
    return;
  }
  send({ kind: 'end_turn' });
}

function showFaces(d) {
  openModal(h('div', { class: 'col' }, h('h3', {}, d.name, ' ', sch(d.dieSchool), ' ', h('span', { class: 'muted' }, RISK_NAMES[d.riskTier])), facesGrid(d.faces, { selected: d.faceIndex })));
}

export function renderBattle() {
  const o = store.obs;
  const ui = store.ui;
  const hints = store.hints || {};
  const sel = ui.selectedDie && o.hand.find((d) => d.dieId === ui.selectedDie) ? ui.selectedDie : null;
  if (!sel) ui.selectedDie = null;
  const tgtEnemy = selectedEnemyId();
  const p = o.player;
  const corruptN = o.hand.filter((d) => d.isCorrupt).length;
  let preview = '';

  const slots = h('div', { class: 'slots' }, o.slots.map((s, i) => {
    const hint = sel ? hints[sel] && hints[sel][s.slotId] : null;
    const pv = hint && hint.legal ? previewFor(hint, tgtEnemy) : null;
    if (pv && ui.hoverSlot === s.slotId) preview = previewText(pv);
    const cls = ['slot', `k-${s.slotKind}`];
    if (s.disabled) cls.push('disabled');
    if (s.filledBy) cls.push('filled');
    if (sel) cls.push(hint && hint.legal ? 'ok' : 'bad');
    return h('div', {
      class: cls.join(' '), 'data-slot': s.slotId,
      onclick: () => attemptPlay(s.slotId),
      onmouseenter: () => { if (sel) { ui.hoverSlot = s.slotId; updatePreview(); } },
      onmouseleave: () => { ui.hoverSlot = null; updatePreview(); },
      ondragover: (e) => { e.preventDefault(); e.currentTarget.classList.add('dragover'); },
      ondragleave: (e) => e.currentTarget.classList.remove('dragover'),
      ondrop: (e) => { e.preventDefault(); e.currentTarget.classList.remove('dragover'); attemptPlay(s.slotId); }
    },
    h('span', { class: 'key' }, h('kbd', {}, SLOT_KEYS[i] || '')),
    h('span', { class: 'kind' }, `${s.slotKindName}槽`),
    h('span', { class: 'muted' }, s.constraintText, s.bonus ? ` · +${s.bonus}` : '', s.reusable ? ' · 可重复' : ''),
    s.disabled ? h('span', { class: 'reason' }, '⛓ 已禁用') : null,
    s.filledBy ? h('span', {}, `已放置：${s.filledBy.label}`) : null,
    hint && !hint.legal && !s.filledBy && !s.disabled ? h('span', { class: 'reason' }, hint.reason) : null,
    pv ? h('span', { class: 'preview' }, previewText(pv)) : null);
  }));

  const hand = h('div', { class: 'hand' }, o.hand.length ? o.hand.map((d, i) => h('div', {
    class: `die risk-${d.riskTier} ${d.isCorrupt ? 'corrupt' : ''} ${sel === d.dieId ? 'sel' : ''} ${ui.targetMode && ui.targetMode.dieId !== d.dieId ? 'target-mode' : ''}`,
    draggable: 'true', title: `${d.name}（${SCHOOL_NAMES[d.dieSchool]}·${RISK_NAMES[d.riskTier]}）右键查看全部骰面`,
    onclick: () => clickDie(d.dieId),
    oncontextmenu: (e) => { e.preventDefault(); showFaces(d); },
    ondragstart: (e) => { ui.selectedDie = d.dieId; e.dataTransfer.setData('text/plain', d.dieId); setTimeout(render, 0); }
  },
  h('span', { class: 'key' }, h('kbd', {}, String(i + 1))),
  h('span', { class: 'pip' }, PIPS[d.pip - 1]),
  h('span', { class: `lbl sch-${d.school}` }, `${KIND_ICONS[d.kind] || ''} ${d.label}`),
  d.corruptionValue && d.kind !== 'selfCorruption' ? h('span', { class: 'nm', style: { color: 'var(--danger)' } }, `自损 ${d.corruptionValue}`) : null,
  h('span', { class: 'nm' }, d.name))) : h('div', { class: 'muted' }, '（手中没有骰子）'));

  const selDie = sel ? o.hand.find((d) => d.dieId === sel) : null;
  const reroll = legalOf('use_ability', (a) => a.abilityId === 'warrior_reroll');
  const focusLegal = sel ? legalOf('use_ability', (a) => a.abilityId === 'warrior_focus' && a.dieId === sel) : [];
  const focusAb = p.abilities.find((a) => a.id === 'warrior_focus');
  const purifyOk = sel ? legalOf('purify', (a) => a.dieId === sel).length > 0 : false;
  const discardCost = sel && hints[sel] ? hints[sel].__discardCost : 0;

  const previewEl = h('span', { class: 'preview-text gold', id: 'preview-text' }, preview || (sel ? '悬停技能槽查看伤害预览' : '选择手骰 → 放入技能槽'));
  function updatePreview() {
    const el = document.getElementById('preview-text');
    if (!el) return;
    const hint = sel && ui.hoverSlot ? hints[sel][ui.hoverSlot] : null;
    const pv = hint && hint.legal ? previewFor(hint, tgtEnemy) : null;
    el.textContent = pv ? `伤害/数值预览：${previewText(pv)}` : hint && !hint.legal ? `✕ ${hint.reason}` : (sel ? '悬停技能槽查看伤害预览' : '选择手骰 → 放入技能槽');
  }

  const actionbar = h('div', { class: 'actionbar' },
    h('button', { class: 'primary', onclick: endTurn, title: corruptN ? `还有 ${corruptN} 颗魔化骰未处理` : '空格' }, corruptN ? `结束回合（${corruptN} 颗魔化骰未处理）` : '结束回合 ␣'),
    h('button', { disabled: !reroll.length, onclick: () => send({ kind: 'use_ability', abilityId: 'warrior_reroll' }) }, `⟳ 重掷全部（${(p.abilities.find((a) => a.id === 'warrior_reroll') || {}).usesLeft || 0}）`),
    h('button', { disabled: !purifyOk, onclick: () => send({ kind: 'purify', dieId: sel }), title: '无代价清除选中的魔化骰 (P)' }, `✧ 净化（${p.purifyLeft}）`),
    h('button', { class: 'danger', disabled: !sel, onclick: () => send({ kind: 'discard_die', dieId: sel }), title: 'D' }, sel && discardCost ? `弃骰（自损 ${discardCost}）` : '弃骰'),
    focusAb && focusAb.usesLeft > 0 && selDie ? h('span', { class: 'row', style: { gap: '4px' } }, h('span', { class: 'muted' }, '专注改点：'),
      [1, 2, 3, 4, 5, 6].map((v) => h('button', { class: 'small', disabled: !focusLegal.find((a) => a.value === v), onclick: () => send({ kind: 'use_ability', abilityId: 'warrior_focus', dieId: sel, value: v }) }, PIPS[v - 1]))) : null,
    animState.running ? h('button', { class: 'small', onclick: () => { stopAnim(); } }, '跳过动画') : null,
    previewEl);

  const counterPanel = h('div', { class: 'panel counter-wheel' },
    h('b', {}, '学派克制方阵'),
    o.toggles.schoolCounter ? h('div', {},
      sch('blade'), ' 克 ', sch('verdant'), h('br'), sch('verdant'), ' 克 ', sch('arcana'), h('br'), sch('arcana'), ' 克 ', sch('blade'), h('br'),
      h('span', { class: 'muted' }, `克制 ${mulText(o.config.counterMulPermille.advantage)} / 被克 ${mulText(o.config.counterMulPermille.disadvantage)}`), h('br'),
      '你的主学派：', sch(p.deckSchool), h('br'),
      h('span', { class: 'muted', style: { fontSize: '12px' } }, '敌人按你的主学派计算对你的克制')) : h('div', { class: 'muted' }, '学派克制已关闭（×1.00）'));

  const sideInfo = h('div', { class: 'panel col', style: { gap: '6px', fontSize: '13px' } },
    h('div', { class: 'piles' }, `抽骰堆 ${o.piles.draw} · 弃骰堆 ${o.piles.discard} · 骰池 ${o.piles.deck}`),
    p.charge ? h('div', {}, `🗡 蓄势 ${p.charge}（锋刃攻击 +${p.charge}）`) : h('div', { class: 'muted' }, '🗡 蓄势 0（锋刃攻击累积）'),
    p.statuses.length ? statusChips(p.statuses) : null,
    h('div', { class: 'muted' }, `每回合抽 ${o.config.drawPerTurn} · 手骰上限 ${o.config.handMax}`));

  const log = h('div', { class: 'panel' }, h('b', {}, '战斗记录'), h('div', { class: 'log' }, store.log.slice(-40).reverse().map((t) => h('div', {}, t))));

  return h('div', { class: 'screen col', style: { gap: '12px' } },
    h('div', { class: 'row' }, h('h2', { style: { margin: 0 } }, BATTLE_KIND[o.battleKind] || '战斗'), h('span', { class: 'muted' }, `第 ${o.turn} 回合`),
      ...o.battleHints.map((t) => h('span', { class: 'hint-banner' }, '⚠ ', t))),
    h('div', { class: 'battle' },
      h('div', { class: 'col' },
        h('div', { class: 'enemies' }, o.enemies.map(enemyCard)),
        h('div', { class: 'muted', style: { fontSize: '12px' } }, '点击敌人切换攻击目标 · 技能槽：'),
        slots,
        h('div', { class: 'muted', style: { fontSize: '12px' } }, '手骰（拖到技能槽，或点选骰子再点槽；右键看骰面）：'),
        hand,
        actionbar),
      h('div', { class: 'side' }, counterPanel, sideInfo, log)));
}

export function battleKey(e) {
  const o = store.obs;
  if (!o || o.phase !== 'player_turn') return;
  const k = e.key.toLowerCase();
  if (/^[1-8]$/.test(k)) {
    const d = o.hand[parseInt(k, 10) - 1];
    if (d) clickDie(d.dieId);
    e.preventDefault();
  } else if (['q', 'w', 'e', 'r', 't', 'y', 'u'].includes(k)) {
    const s = o.slots[['q', 'w', 'e', 'r', 't', 'y', 'u'].indexOf(k)];
    if (s) attemptPlay(s.slotId);
  } else if (k === ' ') {
    e.preventDefault();
    endTurn();
  } else if (k === 'escape') {
    store.ui.selectedDie = null;
    store.ui.targetMode = null;
    render();
  } else if (k === 'd' && store.ui.selectedDie) {
    send({ kind: 'discard_die', dieId: store.ui.selectedDie });
  } else if (k === 'p' && store.ui.selectedDie) {
    send({ kind: 'purify', dieId: store.ui.selectedDie });
  }
}
