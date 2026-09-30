import test from 'node:test';
import assert from 'node:assert/strict';
import * as K from '../../kernel/index.js';
import { tables, newGame } from '../helpers.js';

function atEvent(eventId, seed = 1) {
  const s = structuredClone(newGame({ seed }));
  s.stage = 5; s.chapter = 2;
  s.player.starShards = 500;
  s.node = { nodeId: 'n-test', kind: 'event', queue: ['complete'], event: { eventId, resolved: false, resultText: null, chosen: null } };
  s.phase = 'event';
  return s;
}

test('全部事件的全部选项都可执行，且结果可离开', () => {
  for (const [id, ev] of Object.entries(tables.events)) {
    ev.options.forEach((_, i) => {
      for (const seed of [1, 2, 3]) {
        const s = atEvent(id, seed);
        const r = K.reduce(s, { kind: 'event_choose', optionId: `opt_${i}` }, tables);
        assert.ok(!r.error && !r.illegal, `${id} opt_${i}: ${r.code || (r.error && r.error.message)}`);
        let st = r.state;
        if (st.phase === 'forge') st = K.reduce(st, { kind: 'shop_leave' }, tables).state;
        if (st.phase === 'relic_swap') st = K.reduce(st, { kind: 'relic_swap', relicId: 'skip' }, tables).state;
        if (st.phase === 'event') {
          assert.equal(st.node.event.resolved, true);
          st = K.reduce(st, { kind: 'shop_leave' }, tables).state;
        }
        assert.ok(['map', 'settle'].includes(st.phase), `${id} opt_${i} → ${st.phase}`);
      }
    });
  }
});

test('事件要求不满足时选项非法；厄咒骰可被事件移除', () => {
  const s = atEvent('ev_blood_pact');
  s.player.hearts = 2;
  assert.equal(K.isLegal(s, { kind: 'event_choose', optionId: 'opt_0' }, tables).code, 'E_ACTION_REQUIREMENT');
  const c = atEvent('ev_corrupt_spring');
  const r1 = K.reduce(c, { kind: 'event_choose', optionId: 'opt_0' }, tables).state; // 塞 2 颗滞涩骰
  const curses = (st) => st.player.deck.filter((id) => st.player.dice[id].defId.startsWith('die_curse')).length;
  assert.equal(curses(r1), 2);
  const c2 = structuredClone(r1);
  c2.node.event = { eventId: 'ev_corrupt_spring', resolved: false, resultText: null, chosen: null };
  const r2 = K.reduce(c2, { kind: 'event_choose', optionId: 'opt_1' }, tables).state;
  assert.equal(curses(r2), 0);
});

test('遗物：获取/替换/代价型效果', () => {
  const s = structuredClone(newGame());
  s.player.relics = ['relic_iron_bark', 'relic_purifier', 'relic_blade_mark'];
  s.node = { nodeId: 'n-t', kind: 'shrine', queue: ['complete'], shrine: { options: [{ optionId: 'relic', label: 'x', available: true, relicId: 'relic_glass_heart' }] } };
  s.phase = 'shrine';
  const r = K.reduce(s, { kind: 'shrine_choose', optionId: 'relic' }, tables);
  assert.equal(r.state.phase, 'relic_swap');
  const vm = r.state.player.virtueMax;
  const r2 = K.reduce(r.state, { kind: 'relic_swap', relicId: 'relic_iron_bark' }, tables);
  assert.ok(r2.state.player.relics.includes('relic_glass_heart'));
  assert.equal(r2.state.player.virtueMax, vm + 4);
  assert.equal(r2.state.phase, 'map');
  // 铁树皮：战斗开始 8 光盾
  const b = structuredClone(newGame());
  b.player.relics = ['relic_iron_bark'];
  assert.equal(K.debugStartBattle(b, ['mon_verdant_slime'], tables).player.ward, 8);
  // relics=false 时遗物无效
  const off = structuredClone(newGame({ toggles: { relics: false } }));
  off.player.relics = ['relic_iron_bark'];
  assert.equal(K.debugStartBattle(off, ['mon_verdant_slime'], tables).player.ward, 0);
});
