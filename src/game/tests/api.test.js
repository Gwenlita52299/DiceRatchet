// 后端 API 契约 + T-10 无泄漏 + A8 在线/重放对拍
import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from '../server.js';
import { tables } from './helpers.js';
import { chooseAction } from '../scripts/bots.js';
import * as K from '../kernel/index.js';

const SEED = 3141592653; // 足够独特，便于在响应中做黑名单扫描
const BLACKLIST = ['"rng"', '"drawPile"', '"discardPile"', '"aiState"', '"enemyGroupId"', '"stateVersion"', '"s0"', '"s1"', '"queue"', String(SEED)];

async function withServer(fn) {
  const { server } = createServer({ log: false });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${server.address().port}`;
  const call = async (method, p, body) => {
    const res = await fetch(base + p, { method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined });
    return { status: res.status, json: await res.json(), text: null };
  };
  try { await fn(call); } finally { server.close(); }
}

test('健康检查 / schema / seed / content', async () => {
  await withServer(async (call) => {
    const h = await call('GET', '/api/health');
    assert.equal(h.json.ok, true);
    assert.ok(h.json.data.kernelVersion);
    const s = await call('GET', '/api/config/schema');
    assert.equal(s.json.data.difficulties.length, 3);
    assert.equal(s.json.data.toggles.length, 5);
    for (const k of Object.keys(tables.difficulty.keys)) assert.ok(k in s.json.data.difficulties[0], k);
    const seed = await call('POST', '/api/seed', { difficulty: 'easy' });
    assert.ok(Number.isInteger(seed.json.data.seed));
    const c = await call('GET', '/api/content/skills');
    assert.ok(c.json.data.skill_blade_flurry);
    assert.equal((await call('GET', '/api/nope')).status, 404);
  });
});

test('L1/L2 校验与连续 3 次非法挂起', async () => {
  await withServer(async (call) => {
    const g = await call('POST', '/api/game', { difficulty: 'medium', seed: 1 });
    const sid = g.json.data.sessionId;
    const bad = await call('POST', `/api/game/${sid}/action`, { action: { kind: 'fly' } });
    assert.equal(bad.status, 400);
    assert.equal(bad.json.error.code, 'E_ACTION_SYNTAX');
    for (let i = 0; i < 3; i++) {
      const r = await call('POST', `/api/game/${sid}/action`, { action: { kind: 'end_turn' } });
      assert.equal(r.status, 200);
      assert.equal(r.json.ok, false);
      assert.equal(r.json.error.code, 'E_ACTION_ILLEGAL');
    }
    const sus = await call('POST', `/api/game/${sid}/action`, { action: { kind: 'end_turn' } });
    assert.equal(sus.json.error.code, 'E_STATE_SUSPENDED');
    assert.equal((await call('GET', '/api/game/s-missing')).json.error.code, 'E_STATE_NO_SESSION');
    assert.equal((await call('POST', '/api/game', { difficulty: 'insane' })).status, 400);
  });
});

test('T-10 无泄漏 + A8 在线对局与 /api/replay 的 obsDigest 逐行一致', async () => {
  await withServer(async (call) => {
    const cfg = { difficulty: 'medium', seed: SEED, toggles: {} };
    const g = await call('POST', '/api/game', cfg);
    const sid = g.json.data.sessionId;
    const runId = g.json.data.runId;
    const digests = [g.json.data.obsDigest];
    const actions = [];
    const allText = [JSON.stringify(g.json)];
    // 本地镜像 state 仅用于驱动 bot 选动作（与服务端同配置同动作 → 同状态）
    let mirror = K.createGame({ ...K.applyDifficulty(cfg, 'medium', tables), runId }, tables);
    let rnd = () => 0.3;
    for (let i = 0; i < 250 && mirror.phase !== 'settle'; i++) {
      const a = chooseAction(mirror, tables, 'greedy_counter', rnd);
      const r = await call('POST', `/api/game/${sid}/action`, { action: a });
      assert.equal(r.json.ok, true, JSON.stringify(r.json.error));
      allText.push(JSON.stringify(r.json));
      digests.push(r.json.data.obsDigest);
      actions.push(a);
      mirror = K.reduce(mirror, a, tables).state;
      assert.equal(K.digest(K.observe(mirror, 'full', tables)), r.json.data.obsDigest);
    }
    const blob = allText.join('\n');
    for (const bad of BLACKLIST) assert.ok(!blob.includes(bad), `响应中出现黑名单字段 ${bad}`);
    for (const t of allText) assert.equal(JSON.parse(t).data.observation.seed, null);
    const rp = await call('POST', '/api/replay', { config: { ...cfg, runId }, actions });
    assert.deepEqual(rp.json.data.steps.map((s) => s.obsDigest), digests);
  });
});
