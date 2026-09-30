// audio.js — 音效触发点（GAME-00 §10.4）。资产缺失时用 WebAudio 合成占位音。
let ctx = null;
let enabled = localStorage.getItem('rsi.sound') !== 'off';
const TONES = {
  dice_roll: [520, 0.05, 'triangle'], dice_place: [660, 0.06, 'square'], hit_normal: [220, 0.08, 'sawtooth'],
  hit_counter: [440, 0.12, 'sawtooth'], hit_weak: [150, 0.08, 'sawtooth'], ward_gain: [760, 0.08, 'sine'],
  ward_break: [300, 0.1, 'square'], self_corruption: [90, 0.18, 'sawtooth'], enemy_die: [180, 0.25, 'triangle'],
  victory: [880, 0.4, 'triangle'], defeat: [110, 0.5, 'sine'], ui_click: [900, 0.03, 'sine'], ui_deny: [140, 0.1, 'square']
};
export function play(name) {
  if (!enabled) return;
  const t = TONES[name];
  if (!t) return;
  try {
    ctx = ctx || new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.type = t[2];
    o.frequency.value = t[0];
    g.gain.setValueAtTime(0.06, ctx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + t[1]);
    o.connect(g).connect(ctx.destination);
    o.start();
    o.stop(ctx.currentTime + t[1]);
  } catch { /* 静默降级 */ }
}
export function toggleSound() { enabled = !enabled; localStorage.setItem('rsi.sound', enabled ? 'on' : 'off'); return enabled; }
export function soundOn() { return enabled; }
