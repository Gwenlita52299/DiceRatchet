// kernel/shop.js — 商店、宝箱、神殿、休息（GAME-00 §8.5 / §5.1 博弈性要求 2、3），消费 shop / loot 流

import { randint, sample, choice } from './rng.js';
import { counterOf, applyMul, SCHOOLS } from './counter.js';
import { addDieToDeck, removeDieFromDeck, deckSchoolInfo, dieDef, pad } from './state.js';
import { chapterDef, stageDef } from './map.js';
import * as R from './relic.js';

export function stageTheme(state, tables) {
  return chapterDef(tables, state.chapter).theme;
}

function skillsOf(tables, school, safeOnly) {
  return Object.keys(tables.skills)
    .filter((id) => tables.skills[id].school === school && (!safeOnly || tables.skills[id].riskTier === 'safe'))
    .sort();
}

function price(state, lo, hi, stream) {
  return applyMul(randint(state, stream, lo, hi), state.config.shopPriceMulPermille);
}

function genShopItems(ctx) {
  const { state, tables } = ctx;
  const cfg = tables.shops;
  const counter = counterOf(stageTheme(state, tables));
  const all = Object.keys(tables.skills).sort();
  const items = [];
  const picked = new Set();
  const first = choice(state, 'shop', skillsOf(tables, counter, false));
  picked.add(first);
  items.push({ defId: first, kind: 'die' });
  const bias = tables.difficulty.common.biasStrengthPermille;
  for (let i = 1; i < cfg.shop_dice.count; i++) {
    const biased = randint(state, 'shop', 1, 1000) <= bias;
    const pool = (biased ? skillsOf(tables, counter, false) : all).filter((id) => !picked.has(id));
    const id = choice(state, 'shop', pool.length ? pool : all.filter((x) => !picked.has(x)));
    picked.add(id);
    items.push({ defId: id, kind: 'die' });
  }
  for (const it of items) it.price = price(state, cfg.shop_dice.priceMin, cfg.shop_dice.priceMax, 'shop');
  if (state.config.toggles.relics) {
    const pool = R.relicPool(state, tables, ['common', 'rare']);
    for (const rid of sample(state, 'shop', pool, cfg.shop_relic.count)) {
      items.push({ defId: rid, kind: 'relic', price: price(state, cfg.shop_relic.priceMin, cfg.shop_relic.priceMax, 'shop') });
    }
  }
  state.shopSeq += 1;
  return items.map((it, i) => ({ ...it, itemId: `s-${pad(state.shopSeq, 3)}-${i}`, sold: false }));
}

export function enterShop(ctx) {
  const { state, tables } = ctx;
  state.node.shop = { items: [], refreshLeft: tables.shops.shop_dice.refreshMax, freeRemoveUsed: false };
  state.node.shop.items = genShopItems(ctx);
  state.phase = 'shop';
}

export function removePrice(state, tables) {
  if (R.freeRemoveAvailable(state)) return 0;
  const c = tables.shops.shop_remove;
  return applyMul(c.base + c.step * state.player.removeCount, state.config.shopPriceMulPermille);
}

export function refreshPrice(state, tables) {
  return applyMul(tables.shops.shop_dice.refreshCost, state.config.shopPriceMulPermille);
}

export function shopBuy(ctx, itemId) {
  const { state, events } = ctx;
  const it = state.node.shop.items.find((x) => x.itemId === itemId);
  state.player.starShards -= it.price;
  it.sold = true;
  if (it.kind === 'die') {
    const id = addDieToDeck(state, it.defId);
    events.push({ type: 'die_gained', dieId: id, defId: it.defId });
  } else {
    R.grantRelic(ctx, it.defId, 'shop');
  }
}

export function shopRefresh(ctx) {
  const { state, tables } = ctx;
  state.player.starShards -= refreshPrice(state, tables);
  state.node.shop.refreshLeft -= 1;
  state.node.shop.items = genShopItems(ctx);
  ctx.events.push({ type: 'shop_refresh' });
}

export function shopRemove(ctx, dieId) {
  const { state, tables } = ctx;
  const cost = removePrice(state, tables);
  if (cost === 0 && R.freeRemoveAvailable(state)) state.node.shop.freeRemoveUsed = true;
  else state.player.removeCount += 1;
  state.player.starShards -= cost;
  const defId = state.player.dice[dieId].defId;
  removeDieFromDeck(state, dieId);
  ctx.events.push({ type: 'die_removed', dieId, defId });
}

export function removableDice(state, tables) {
  if (state.player.deck.length <= 5) return [];
  return state.player.deck.filter((id) => dieDef(tables, state.player.dice[id].defId).category !== 'cursed');
}

// ---------------- 宝箱：两同学派 + 一克制当前敌学派 ----------------
export function enterTreasure(ctx) {
  const { state, tables } = ctx;
  const safeOnly = !!stageDef(tables, state.stage).tutorial;
  const counter = counterOf(stageTheme(state, tables));
  let build = deckSchoolInfo(state, tables).school;
  if (build === 'neutral' || build === counter) {
    build = choice(state, 'loot', SCHOOLS.filter((s) => s !== counter));
  }
  const offers = [
    ...sample(state, 'loot', skillsOf(tables, build, safeOnly), 2),
    ...sample(state, 'loot', skillsOf(tables, counter, safeOnly), 1)
  ];
  if (offers.length < 3) {
    // 教学关安全骰不足时，从其余安全技能骰（含通用）补齐
    const rest = Object.keys(tables.skills).filter((id) => !offers.includes(id) && (!safeOnly || tables.skills[id].riskTier === 'safe')).sort();
    offers.push(...sample(state, 'loot', rest, 3 - offers.length));
  }
  state.node.treasure = { offers, counterSchool: counter, buildSchool: build };
  state.phase = 'treasure';
}

export function treasurePick(ctx, defId) {
  const { state, tables, events } = ctx;
  if (defId === 'skip') {
    const g = applyMul(tables.shops.treasure.skipGold, R.goldMul(state, tables));
    state.player.starShards += g;
    events.push({ type: 'gold', value: g });
  } else {
    const id = addDieToDeck(state, defId);
    events.push({ type: 'die_gained', dieId: id, defId });
  }
  state.node.treasure = null;
}

// ---------------- 神殿 / 休息 ----------------
export function enterShrine(ctx) {
  const { state, tables } = ctx;
  const options = [
    { optionId: 'heart', label: '祈祷：回复 1 心', available: state.player.hearts < state.player.heartsMax },
    { optionId: 'gold', label: `求财：得 ${applyMul(60, R.goldMul(state, tables))} 星屑`, available: true }
  ];
  if (state.config.toggles.relics) {
    const pool = R.relicPool(state, tables, ['rare']);
    if (pool.length) {
      const rid = choice(state, 'loot', pool);
      options.push({ optionId: 'relic', label: `供奉：获得遗物「${tables.relics[rid].name}」`, available: true, relicId: rid });
    }
  }
  state.node.shrine = { options };
  state.phase = 'shrine';
}

export function shrineChoose(ctx, optionId) {
  const { state, tables, events } = ctx;
  const opt = state.node.shrine.options.find((o) => o.optionId === optionId);
  state.node.shrine = null;
  if (optionId === 'heart') {
    state.player.hearts = Math.min(state.player.heartsMax, state.player.hearts + 1);
    events.push({ type: 'heart_gained', hearts: state.player.hearts });
  } else if (optionId === 'gold') {
    const g = applyMul(60, R.goldMul(state, tables));
    state.player.starShards += g;
    events.push({ type: 'gold', value: g });
  } else if (optionId === 'relic') {
    return R.grantRelic(ctx, opt.relicId, null);
  }
  return true;
}

export function enterRest(ctx) {
  const { state } = ctx;
  state.node.rest = {
    options: [
      { optionId: 'heart', label: '休憩：回复 1 心', available: state.player.hearts < state.player.heartsMax },
      { optionId: 'vigor', label: '冥想：净化上限永久 +1', available: true }
    ]
  };
  state.phase = 'rest';
}

export function restChoose(ctx, optionId) {
  const { state, events } = ctx;
  state.node.rest = null;
  if (optionId === 'heart') {
    state.player.hearts = Math.min(state.player.heartsMax, state.player.hearts + 1);
    events.push({ type: 'heart_gained', hearts: state.player.hearts });
  } else {
    state.player.virtueMax += 1;
    events.push({ type: 'virtue_max_up', virtueMax: state.player.virtueMax });
  }
}
