// kernel/relic.js — 遗物效果查询与获取（GAME-00 §8.3 / GAME-02 §7）
// 遗物效果只读 state，不消费 RNG（遗物不得修改 RNG 消费顺序）。

export function hasRelic(state, id) {
  return state.config.toggles.relics && state.player.relics.includes(id);
}

function relicOp(tables, id) {
  return tables.relics[id] ? tables.relics[id].effect : null;
}

/** 打出攻击面时的平值加成 */
export function attackFlatBonus(state, tables, school) {
  let b = 0;
  if (school === 'blade' && hasRelic(state, 'relic_blade_mark')) b += relicOp(tables, 'relic_blade_mark').value;
  if (hasRelic(state, 'relic_last_stand') && state.player.virtue <= relicOp(tables, 'relic_last_stand').threshold) {
    b += relicOp(tables, 'relic_last_stand').value;
  }
  b += deckThinBonus(state, tables);
  return b;
}

export function deckThinBonus(state, tables) {
  if (!hasRelic(state, 'relic_deck_thin')) return 0;
  const op = relicOp(tables, 'relic_deck_thin');
  return state.player.deck.length <= op.threshold ? op.value : 0;
}

export function counterBonusPermille(state, tables) {
  return hasRelic(state, 'relic_school_prism') ? relicOp(tables, 'relic_school_prism').value : 0;
}

export function relicMulPermille(state, tables, enemy) {
  if (hasRelic(state, 'relic_boss_trophy') && (enemy.tier === 'elite' || enemy.tier === 'boss')) {
    return relicOp(tables, 'relic_boss_trophy').mulPermille;
  }
  return 1000;
}

export function drawDelta(state, tables) {
  let d = 0;
  if (hasRelic(state, 'relic_arcana_loop') && state.player.arcanaLastTurn >= relicOp(tables, 'relic_arcana_loop').need) d += 1;
  if (hasRelic(state, 'relic_stubborn_mix')) d -= relicOp(tables, 'relic_stubborn_mix').drawPenalty;
  return d;
}

export function monoPenaltyDisabled(state) {
  return hasRelic(state, 'relic_stubborn_mix');
}

export function goldMul(state, tables) {
  let m = state.config.goldMulPermille;
  if (hasRelic(state, 'relic_greedy_eye')) m = Math.floor((m * relicOp(tables, 'relic_greedy_eye').goldPermille) / 1000);
  return m;
}

export function battleStartVirtueLoss(state, tables) {
  return hasRelic(state, 'relic_greedy_eye') ? relicOp(tables, 'relic_greedy_eye').virtueLoss : 0;
}

export function battleStartWard(state, tables) {
  return hasRelic(state, 'relic_iron_bark') ? relicOp(tables, 'relic_iron_bark').value : 0;
}

export function purifyBonus(state, tables) {
  return hasRelic(state, 'relic_purifier') ? relicOp(tables, 'relic_purifier').value : 0;
}

export function swiftHandActive(state) {
  return hasRelic(state, 'relic_swift_hand') && !state.player.firstPlayDone;
}

export function verdantMend(state, tables) {
  return hasRelic(state, 'relic_verdant_root') ? relicOp(tables, 'relic_verdant_root').value : 0;
}

export function extraHeartLoss(state) {
  return hasRelic(state, 'relic_glass_heart') ? 1 : 0;
}

export function bladeEchoValue(state, tables) {
  return hasRelic(state, 'relic_blade_echo') ? relicOp(tables, 'relic_blade_echo').value : 0;
}

export function freeRemoveAvailable(state) {
  return hasRelic(state, 'relic_deck_thin') && state.node && state.node.shop && !state.node.shop.freeRemoveUsed;
}

function onGain(state, tables, id) {
  if (id === 'relic_glass_heart') {
    const v = relicOp(tables, id).virtueMax;
    state.player.virtueMax += v;
  }
}

function onLose(state, tables, id) {
  if (id === 'relic_glass_heart') {
    const v = relicOp(tables, id).virtueMax;
    state.player.virtueMax -= v;
    state.player.virtue = Math.min(state.player.virtue, state.player.virtueMax);
  }
}

/**
 * 获得遗物；栏位满时进入 relic_swap 阶段（必须选择丢弃哪一个或放弃新遗物）。
 * returnPhase=null 表示完成后继续节点队列。返回 true 表示已直接获得。
 */
export function grantRelic(ctx, relicId, returnPhase) {
  const { state, tables, events } = ctx;
  if (!state.config.toggles.relics || !tables.relics[relicId]) return true;
  if (state.player.relics.includes(relicId)) return true;
  if (state.player.relics.length < state.config.relicSlots) {
    state.player.relics.push(relicId);
    onGain(state, tables, relicId);
    events.push({ type: 'relic_gained', relicId, name: tables.relics[relicId].name });
    return true;
  }
  state.pendingRelic = { relicId, returnPhase };
  state.phase = 'relic_swap';
  events.push({ type: 'relic_swap_required', relicId });
  return false;
}

export function swapRelic(ctx, dropId) {
  const { state, tables, events } = ctx;
  const pending = state.pendingRelic;
  if (dropId !== 'skip') {
    state.player.relics = state.player.relics.filter((r) => r !== dropId);
    onLose(state, tables, dropId);
    events.push({ type: 'relic_lost', relicId: dropId });
    state.player.relics.push(pending.relicId);
    onGain(state, tables, pending.relicId);
    events.push({ type: 'relic_gained', relicId: pending.relicId, name: tables.relics[pending.relicId].name });
  }
  state.pendingRelic = null;
  return pending.returnPhase;
}

/** 随机遗物候选（不含已拥有；rarity 过滤），用于奖励/商店/事件 */
export function relicPool(state, tables, rarities) {
  return Object.keys(tables.relics)
    .filter((id) => rarities.includes(tables.relics[id].rarity) && !state.player.relics.includes(id))
    .sort();
}
