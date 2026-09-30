// anim.js — 事件动画队列（GAME-00 §10.2 第 6 条）：按序播放，播放期间禁用输入，可跳过
import { play } from './audio.js';
import { STATUS_NAMES, SCHOOL_NAMES, mulText } from './common.js';

let queue = [];
let timer = null;
let doneCb = null;
export const animState = { running: false };

function anchor(target) {
  const el = document.querySelector(`[data-target="${target}"]`);
  if (!el) return { x: window.innerWidth / 2, y: window.innerHeight / 3 };
  const r = el.getBoundingClientRect();
  return { x: r.left + r.width / 2, y: r.top + r.height / 3, el };
}

function float(target, text, cls) {
  const a = anchor(target);
  if (!a.el) return; // 目标不在当前页面（如战斗已结束），只记日志不飘字
  const el = document.createElement('div');
  el.className = `float ${cls}`;
  el.textContent = text;
  el.style.left = `${a.x}px`;
  el.style.top = `${a.y}px`;
  document.getElementById('float-layer').appendChild(el);
  setTimeout(() => el.remove(), 1000);
  if (a.el && cls !== 'heal' && cls !== 'ward') {
    a.el.classList.remove('shake');
    void a.el.offsetWidth;
    a.el.classList.add('shake');
  }
}

/** 返回该事件的日志文本；同时触发飘字与音效 */
export function describe(ev, names) {
  const n = (id) => (id === 'player' ? '你' : names[id] || id);
  switch (ev.type) {
    case 'damage': {
      const counter = ev.mulPermille > 1000, weak = ev.mulPermille < 1000;
      const cls = ev.target === 'player' ? 'normal' : counter ? 'counter' : weak ? 'weak' : 'normal';
      return {
        text: `${n(ev.source && ev.source.startsWith('e-') ? ev.source : 'player')} → ${n(ev.target)}：${ev.raw !== undefined && ev.mulPermille !== 1000 ? `${ev.raw} ${mulText(ev.mulPermille)} = ` : ''}${ev.value}${ev.dealt !== ev.value ? `（实伤 ${ev.dealt}）` : ''}${ev.execute ? ' 处决!' : ''}`,
        fx: () => { float(ev.target, `-${ev.dealt}${counter ? '!' : ''}`, cls); play(counter ? 'hit_counter' : weak ? 'hit_weak' : 'hit_normal'); }
      };
    }
    case 'self_corruption': return { text: `自魔化 ${ev.value}`, fx: () => { float('player', `☣-${ev.value}`, 'self'); play('self_corruption'); } };
    case 'ward_gain': return { text: `${n(ev.target)} 获得光盾 ${ev.value}`, fx: () => { float(ev.target, `🛡+${ev.value}`, 'ward'); play('ward_gain'); } };
    case 'ward_absorb': return { text: `${n(ev.target)} 光盾吸收 ${ev.value}`, fx: () => play('ward_break') };
    case 'mend': return ev.value ? { text: `${n(ev.target)} 恢复 ${ev.value}`, fx: () => float(ev.target, `+${ev.value}`, 'heal') } : null;
    case 'status_applied': return { text: `${n(ev.target)} 获得 ${STATUS_NAMES[ev.statusKind]} ${ev.stacks}`, fx: () => float(ev.target, `${STATUS_NAMES[ev.statusKind]}+${ev.stacks}`, 'weak') };
    case 'status_tick': return ev.value ? { text: `${n(ev.target)} 受到${STATUS_NAMES[ev.statusKind]} ${ev.value}`, fx: () => float(ev.target, `-${ev.value}`, 'self') } : null;
    case 'status_immune': return { text: `${n(ev.target)} 免疫${STATUS_NAMES[ev.statusKind]}` };
    case 'enemy_died': return { text: `${ev.name} 被净化！`, fx: () => play('enemy_die') };
    case 'boss_phase': return { text: `⚠ ${n(ev.enemyId)} 进入第 ${ev.phase} 阶段（${SCHOOL_NAMES[ev.school]}）`, fx: () => play('hit_counter') };
    case 'player_lost_heart': return { text: `💔 失去 1 心，剩余 ${ev.heartsLeft}`, fx: () => play('defeat') };
    case 'revive': return { text: `✨ 复苏：净化值回复到 ${ev.virtue}` };
    case 'roll': return { text: `掷骰：${ev.pips.join(' ')}`, fx: () => play('dice_roll') };
    case 'play_face': return { text: `打出「${ev.label}」`, fx: () => play('dice_place') };
    case 'discard': return { text: `弃掉骰子${ev.corrupt ? '（魔化代价）' : ''}` };
    case 'purify': return { text: `净化 1 颗魔化骰（剩余 ${ev.purifyLeft}）` };
    case 'gold': return { text: `星屑 ${ev.value > 0 ? '+' : ''}${ev.value}` };
    case 'curse_added': return { text: '☣ 一颗厄咒骰混入了你的骰池' };
    case 'slot_disabled': return { text: `⛓ 技能槽 ${ev.slotId} 被禁用` };
    case 'drain': return { text: `下回合少抽 ${ev.value} 颗骰` };
    case 'relic_gained': return { text: `获得遗物「${ev.name}」` };
    case 'die_gained': return { text: '获得新骰子' };
    case 'battle_end': return { text: ev.result === 'win' ? `战斗胜利！星屑 +${ev.drops.starShards}` : '战斗失败', fx: () => play(ev.result === 'win' ? 'victory' : 'defeat') };
    case 'turn_start': return { text: `—— 第 ${ev.turn} 回合 ——` };
    case 'die_modified': return { text: `骰子点数变为 ${ev.pip}` };
    case 'duplicate': return { text: `复制「${ev.label}」` };
    case 'reshuffle': return { text: '弃骰堆洗回抽骰堆' };
    case 'hand_overflow': return { text: '手骰已满，多余骰子进入弃骰堆' };
    default: return null;
  }
}

export function playEvents(events, names, log, onDone) {
  stop();
  queue = events.map((e) => describe(e, names)).filter(Boolean);
  for (const q of queue) log.push(q.text);
  while (log.length > 60) log.shift();
  const fxq = queue.filter((q) => q.fx);
  if (!fxq.length) { onDone && onDone(); return; }
  animState.running = true;
  doneCb = onDone;
  let i = 0;
  const step = () => {
    if (i >= fxq.length) { stop(); return; }
    fxq[i++].fx();
    timer = setTimeout(step, 170);
  };
  step();
}

export function stop() {
  clearTimeout(timer);
  timer = null;
  const was = animState.running;
  animState.running = false;
  queue = [];
  if (was && doneCb) { const cb = doneCb; doneCb = null; cb(); }
}
