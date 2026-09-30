// kernel/event.js — 叙事事件（GAME-00 §8.4 / GAME-02 §6），消费 event 流

import { weighted, choice, randint } from './rng.js';
import { applyMul, counterOf } from './counter.js';
import { addDieToDeck, removeDieFromDeck, dieDef } from './state.js';
import { enterForge, applyForgedFace } from './forge.js';
import * as R from './relic.js';

export function eventPool(state, tables) {
  if (!state.config.toggles.events) return [];
  return Object.values(tables.events)
    .filter((ev) => (!ev.relicsOnly || state.config.toggles.relics)
      && ev.stageRange[0] <= state.stage && state.stage <= ev.stageRange[1]
      && !state.seenEvents.includes(ev.id))
    .map((ev) => ev.id)
    .sort();
}

export function enterEvent(ctx) {
  const { state, tables } = ctx;
  const pool = eventPool(state, tables);
  if (!pool.length) return false;
  const id = weighted(state, 'event', pool.map((k) => ({ key: k, weight: tables.events[k].weight })));
  state.seenEvents.push(id);
  state.node.event = { eventId: id, resolved: false, resultText: null, chosen: null };
  state.phase = 'event';
  ctx.events.push({ type: 'node_event', eventId: id });
  return true;
}

export function requirementMet(state, req) {
  if (!req) return true;
  if (typeof req.gold === "number" && state.player.starShards < req.gold) return false;
  if (typeof req.hearts === "number" && state.player.hearts < req.hearts) return false;
  return true;
}

export function requirementText(req) {
  if (!req) return null;
  const parts = [];
  if (typeof req.gold === "number") parts.push(`需要 ${req.gold} 星屑`);
  if (typeof req.hearts === "number") parts.push(`需要 ≥${req.hearts} 心`);
  return parts.join('，');
}

function randomSkill(state, tables, school) {
  const pool = Object.keys(tables.skills).filter((id) => !school || tables.skills[id].school === school).sort();
  return choice(state, 'event', pool);
}

function nonCursedDice(state, tables) {
  return state.player.deck.filter((id) => dieDef(tables, state.player.dice[id].defId).category !== 'cursed');
}

function chapterBossSchool(state, tables) {
  const bossStage = tables.stages.stages.find((s) => s.chapter === state.chapter && s.boss);
  const def = tables.monsters[bossStage.bossId];
  return def.phases ? def.phases[0].school : def.school;
}

/** 执行效果列表；返回 'forge' / 'relic_swap' / null */
function applyOps(ctx, ops) {
  const { state, tables, events } = ctx;
  const p = state.player;
  let next = null;
  for (const op of ops) {
    switch (op.op) {
      case 'gain_gold': {
        const g = applyMul(op.value, R.goldMul(state, tables));
        p.starShards += g; events.push({ type: 'gold', value: g }); break;
      }
      case 'lose_gold':
        p.starShards = Math.max(0, p.starShards - op.value); events.push({ type: 'gold', value: -op.value }); break;
      case 'lose_virtue':
        p.pendingVirtue -= op.value; events.push({ type: 'virtue_penalty', value: op.value }); break;
      case 'heal_virtue':
        p.pendingVirtue = Math.max(0, p.pendingVirtue) + op.value; events.push({ type: 'virtue_bonus', value: op.value }); break;
      case 'gain_die': {
        const n = op.count || 1;
        for (let i = 0; i < n; i++) {
          const defId = op.defId || randomSkill(state, tables, op.school);
          const id = addDieToDeck(state, defId);
          events.push({ type: 'die_gained', dieId: id, defId });
        }
        break;
      }
      case 'gain_die_counter_boss': {
        const defId = randomSkill(state, tables, counterOf(chapterBossSchool(state, tables)));
        events.push({ type: 'die_gained', dieId: addDieToDeck(state, defId), defId }); break;
      }
      case 'gain_die_random': {
        const defId = randomSkill(state, tables, null);
        events.push({ type: 'die_gained', dieId: addDieToDeck(state, defId), defId }); break;
      }
      case 'remove_die': {
        const pool = nonCursedDice(state, tables);
        if (p.deck.length > 5 && pool.length) {
          const id = choice(state, 'event', pool);
          const defId = p.dice[id].defId;
          removeDieFromDeck(state, id);
          events.push({ type: 'die_removed', dieId: id, defId });
        }
        break;
      }
      case 'gain_relic':
        if (state.config.toggles.relics) {
          const pool = R.relicPool(state, tables, ['common', 'rare']);
          if (pool.length && !R.grantRelic(ctx, choice(state, 'event', pool), 'event')) next = 'relic_swap';
        }
        break;
      case 'gain_heart':
        p.hearts = Math.min(p.heartsMax, p.hearts + op.value); events.push({ type: 'heart_gained', hearts: p.hearts }); break;
      case 'lose_heart':
        p.hearts = Math.max(0, p.hearts - op.value); events.push({ type: 'player_lost_heart', heartsLeft: p.hearts }); break;
      case 'add_curse':
        for (let i = 0; i < op.count; i++) events.push({ type: 'curse_added', dieId: addDieToDeck(state, op.defId), defId: op.defId });
        break;
      case 'remove_curses':
        for (const id of p.deck.slice()) {
          if (dieDef(tables, p.dice[id].defId).category === 'cursed') {
            events.push({ type: 'die_removed', dieId: id, defId: p.dice[id].defId });
            removeDieFromDeck(state, id);
          }
        }
        break;
      case 'modify_face': {
        const pool = nonCursedDice(state, tables);
        if (pool.length) {
          const id = choice(state, 'event', pool);
          const fi = randint(state, 'event', 0, 5);
          applyForgedFace(state, tables, id, fi, op.faceId);
          events.push({ type: 'die_forged', dieId: id, faceIndex: fi, faceId: op.faceId });
        }
        break;
      }
      case 'duplicate_die': {
        const pool = nonCursedDice(state, tables);
        if (pool.length) {
          const src = p.dice[choice(state, 'event', pool)];
          const id = addDieToDeck(state, src.defId);
          if (src.faces) { p.dice[id].faces = src.faces.map((f) => ({ ...f })); p.dice[id].forged = true; }
          events.push({ type: 'die_gained', dieId: id, defId: src.defId });
        }
        break;
      }
      case 'gain_status':
        p.pendingStatuses[op.statusKind] = (p.pendingStatuses[op.statusKind] || 0) + op.value;
        events.push({ type: 'pending_status', statusKind: op.statusKind, stacks: op.value }); break;
      case 'extra_draw':
        p.pendingDraw += op.value; events.push({ type: 'pending_draw', value: op.value }); break;
      case 'add_slot':
        state.slotSeq += 1;
        p.slots.push({ slotId: `slot_x${state.slotSeq}`, slotKind: 'attack', constraint: 'any', bonus: 0, filledBy: null, disabledUntilTurn: null, reusable: false });
        events.push({ type: 'slot_added' }); break;
      case 'goto_forge':
        next = 'forge'; break;
      default:
        break;
    }
  }
  return next;
}

export function eventChoose(ctx, optionId) {
  const { state, tables } = ctx;
  const ev = tables.events[state.node.event.eventId];
  const idx = parseInt(optionId.slice(4), 10);
  const opt = ev.options[idx];
  let outIdx = 0;
  if (opt.outcomes.length > 1) {
    outIdx = parseInt(weighted(state, 'event', opt.outcomes.map((o, i) => ({ key: String(i), weight: o.weight }))), 10);
  }
  const out = opt.outcomes[outIdx];
  state.node.event.resolved = true;
  state.node.event.chosen = optionId;
  state.node.event.resultText = out.text;
  ctx.events.push({ type: 'event_result', eventId: ev.id, optionId, text: out.text });
  const next = applyOps(ctx, out.effect);
  if (state.player.hearts <= 0) {
    state.outcome = 'lose';
    state.phase = 'settle';
    return;
  }
  if (next === 'forge') enterForge(ctx, true);
}
