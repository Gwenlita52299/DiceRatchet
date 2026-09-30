// main.js — 路由、状态、与后端交互（前端只渲染 Observation + 提交 Action）
import { api } from './net.js';
import { h, toast, closeModal } from './common.js';
import { playEvents, animState, stop as stopAnim } from './anim.js';
import { play } from './audio.js';
import { renderMenu, renderSetup, renderCodex } from './render/menu.js';
import { renderHud } from './render/hud.js';
import { renderMap } from './render/map.js';
import { renderBattle, battleKey } from './render/battle.js';
import { renderShop, renderEvent, renderTreasure, renderForge, renderChoice, renderRelicSwap } from './render/nodes.js';
import { renderSettle } from './render/settle.js';

export const store = {
  schema: null,
  sessionId: localStorage.getItem('rsi.session'),
  runId: null,
  obs: null,
  obsDigest: null,
  legal: [],
  hints: null,
  log: [],
  busy: false,
  ui: { selectedDie: null, selectedEnemy: null, targetMode: null, forge: {}, hoverSlot: null, autoAdvance: null }
};

const PHASE_ROUTE = {
  map: 'map', player_turn: 'battle', enemy_turn: 'battle', shop: 'shop', event: 'event', treasure: 'treasure',
  shrine: 'shrine', forge: 'forge', rest: 'rest', relic_swap: 'relic', settle: 'settle'
};
const GAME_ROUTES = new Set(Object.values(PHASE_ROUTE));

function route() { return (location.hash.replace(/^#\/?/, '') || 'menu').split('?')[0]; }
export function go(r) { if (route() !== r) location.hash = `#/${r}`; else render(); }

function applyData(d) {
  if (d.sessionId) { store.sessionId = d.sessionId; localStorage.setItem('rsi.session', d.sessionId); }
  if (d.runId) store.runId = d.runId;
  store.obs = d.observation;
  store.obsDigest = d.obsDigest;
  store.legal = d.legalActions || [];
  store.hints = d.hints || null;
}

function enemyNames() {
  const m = {};
  for (const e of (store.obs && store.obs.enemies) || []) m[e.enemyId] = e.name;
  return m;
}

export async function newGame(cfg) {
  store.busy = true;
  const r = await api('POST', '/game', cfg);
  store.busy = false;
  if (!r.ok) { toast(r.error.message); return; }
  store.log = [];
  store.ui = { selectedDie: null, selectedEnemy: null, targetMode: null, forge: {}, hoverSlot: null };
  applyData(r.data);
  localStorage.setItem('rsi.lastConfig', JSON.stringify(cfg));
  go(PHASE_ROUTE[store.obs.phase]);
}

export async function resume() {
  if (!store.sessionId) return false;
  const r = await api('GET', `/game/${store.sessionId}`);
  if (!r.ok) {
    localStorage.removeItem('rsi.session');
    store.sessionId = null;
    toast('存档会话已过期（服务器可能已重启），请开始新局', 'info');
    return false;
  }
  applyData(r.data);
  go(PHASE_ROUTE[store.obs.phase]);
  return true;
}

export async function send(action) {
  if (store.busy) return;
  if (animState.running) stopAnim();
  store.busy = true;
  play('ui_click');
  const r = await api('POST', `/game/${store.sessionId}/action`, { action });
  store.busy = false;
  if (!r.ok) {
    play('ui_deny');
    toast(r.error.message || r.error.code);
    if (r.error.code === 'E_STATE_NO_SESSION') { localStorage.removeItem('rsi.session'); go('menu'); }
    return;
  }
  const names = enemyNames();
  applyData(r.data);
  store.ui.selectedDie = null;
  store.ui.targetMode = null;
  if (store.obs.enemies.length && !store.obs.enemies.find((e) => e.enemyId === store.ui.selectedEnemy)) store.ui.selectedEnemy = null;
  for (const e of store.obs.enemies) names[e.enemyId] = e.name;
  const r2 = PHASE_ROUTE[store.obs.phase];
  if (route() !== r2) go(r2); else render();
  playEvents(r.data.events || [], names, store.log, () => render());
}

export function legalOf(kind, pred = () => true) {
  return store.legal.filter((a) => a.kind === kind && pred(a));
}

export function render() {
  const app = document.getElementById('app');
  const r = route();
  let page;
  if (GAME_ROUTES.has(r)) {
    if (!store.obs) { resume().then((ok) => { if (!ok) go('menu'); }); app.replaceChildren(h('div', { class: 'loading' }, '读取对局…')); return; }
    const want = PHASE_ROUTE[store.obs.phase];
    if (want !== r) { go(want); return; }
  }
  switch (r) {
    case 'menu': page = renderMenu(); break;
    case 'setup': page = renderSetup(); break;
    case 'codex': page = renderCodex(); break;
    case 'map': page = renderMap(); break;
    case 'battle': page = renderBattle(); break;
    case 'shop': page = renderShop(); break;
    case 'event': page = renderEvent(); break;
    case 'treasure': page = renderTreasure(); break;
    case 'forge': page = renderForge(); break;
    case 'shrine': page = renderChoice('shrine'); break;
    case 'rest': page = renderChoice('rest'); break;
    case 'relic': page = renderRelicSwap(); break;
    case 'settle': page = renderSettle(); break;
    default: page = renderMenu();
  }
  const nodes = [];
  if (GAME_ROUTES.has(r) && r !== 'settle') nodes.push(renderHud());
  nodes.push(page);
  app.replaceChildren(...nodes);
}

document.addEventListener('keydown', (e) => {
  if (e.target && ['INPUT', 'SELECT', 'TEXTAREA'].includes(e.target.tagName)) return;
  if (e.key === 'Escape') { closeModal(); }
  if (route() === 'battle') battleKey(e);
});
document.getElementById('modal').addEventListener('click', (e) => {
  if (e.target.id === 'modal' || e.target.hasAttribute('data-close')) closeModal();
});
window.addEventListener('hashchange', render);

(async function boot() {
  const r = await api('GET', '/config/schema');
  if (r.ok) store.schema = r.data;
  else toast('无法加载配置 schema：' + r.error.message);
  render();
})();
