// render/map.js — 地图页：逐层节点、路径连线、节点预览
import { h, sch, arrow, NODE_INFO, ARROW_TEXT } from '../common.js';
import { store, send } from '../main.js';

export function renderMap() {
  const o = store.obs;
  const m = o.map;
  const track = h('div', { class: 'stage-track' }, o.stages.map((s) => h('div', {
    class: `stage-pip ${s.stage === o.stage ? 'cur' : ''} ${s.stage < o.stage ? 'done' : ''} ${s.boss ? 'boss' : ''} ${s.elite ? 'elite' : ''}`
  }, `${s.stage}`)));
  const rowsEl = h('div', { class: 'map-rows' });
  m.rows.forEach((row) => {
    if (!Array.isArray(row)) {
      rowsEl.append(h('div', { class: 'map-row' }, Array.from({ length: row.count }, () => h('div', { class: 'map-node placeholder' }, h('div', { class: 'ico' }, '？'), '未揭示'))));
      return;
    }
    rowsEl.append(h('div', { class: 'map-row' }, row.map((n) => {
      const [ico, name] = NODE_INFO[n.kind] || ['?', n.kind];
      const cur = n.nodeId === m.currentNodeId;
      return h('div', {
        class: `map-node ${n.reachable ? 'reach' : ''} ${n.visited ? 'visited' : ''} ${cur ? 'current' : ''}`, 'data-node': n.nodeId,
        onclick: n.reachable ? () => send({ kind: 'map_choose', nodeId: n.nodeId }) : null
      }, h('div', { class: 'ico' }, ico), h('b', {}, name),
      n.preview ? h('div', { style: { fontSize: '12px' } }, n.preview.enemyNames.join(' + ')) : null,
      n.preview ? h('div', {}, sch(n.school), ' ', o.toggles.schoolCounter ? arrow(n.preview.arrow, ARROW_TEXT[n.preview.arrow]) : null) : null);
    })));
  });
  const wrap = h('div', { class: 'map-wrap panel' }, rowsEl);
  const reach = m.rows.flat().filter((n) => n && n.reachable);
  // 线性模式：自动推进
  if (!o.toggles.mapBranching && reach.length === 1 && !store.busy) {
    clearTimeout(store.ui.autoAdvance);
    store.ui.autoAdvance = setTimeout(() => { if (store.obs && store.obs.phase === 'map') send({ kind: 'map_choose', nodeId: reach[0].nodeId }); }, 700);
  }
  requestAnimationFrame(() => drawEdges(wrap, m));
  return h('div', { class: 'screen col' },
    track,
    h('div', { class: 'row' }, h('h2', { style: { margin: 0 } }, `第 ${o.stage} 关 · 选择下一个节点`),
      !o.toggles.mapBranching ? h('span', { class: 'muted' }, '（线性模式：自动推进…）') : null),
    wrap,
    h('div', { class: 'muted', style: { fontSize: '13px' } }, '节点下方箭头：', arrow('up', '↑ 敌人克你的主学派'), ' · ', arrow('down', '↓ 你的主学派克它'), ' · 只揭示当前与下一行'));
}

function drawEdges(wrap, m) {
  const svgNS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(svgNS, 'svg');
  svg.setAttribute('class', 'map-svg');
  const base = wrap.getBoundingClientRect();
  for (const row of m.rows) {
    if (!Array.isArray(row)) continue;
    for (const n of row) {
      const a = wrap.querySelector(`[data-node="${n.nodeId}"]`);
      if (!a) continue;
      for (const e of n.edges) {
        const b = wrap.querySelector(`[data-node="${e}"]`);
        if (!b) continue;
        const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
        const line = document.createElementNS(svgNS, 'line');
        line.setAttribute('x1', ra.left + ra.width / 2 - base.left);
        line.setAttribute('y1', ra.top - base.top);
        line.setAttribute('x2', rb.left + rb.width / 2 - base.left);
        line.setAttribute('y2', rb.bottom - base.top);
        const onPath = m.path.includes(n.nodeId) && m.path.includes(e);
        line.setAttribute('stroke', onPath ? '#f5c542' : '#4a5070');
        line.setAttribute('stroke-width', onPath ? '3' : '2');
        line.setAttribute('stroke-dasharray', onPath ? '' : '6 5');
        svg.appendChild(line);
      }
    }
  }
  wrap.querySelector('.map-svg')?.remove();
  wrap.prepend(svg);
}
