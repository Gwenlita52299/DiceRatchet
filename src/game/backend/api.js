// backend/api.js — 路由与序列化（GAME-00 §9）。后端是薄壳：不含任何规则判定。

import crypto from 'node:crypto';
import * as K from '../kernel/index.js';
import { ApiError } from './errors.js';
import { validateActionSyntax, validateGameRequest } from './validate.js';

export const API_VERSION = '1.0.0';
const BUILD_TIME = new Date().toISOString();

function randomSeed() { return crypto.randomInt(0, 4294967295); }
function newRunId() { return `r-${Date.now().toString(36)}${crypto.randomBytes(3).toString('hex')}`; }

function view(state, tables) {
  const observation = K.observe(state, 'full', tables);
  return {
    observation,
    obsDigest: K.digest(observation),
    legalActions: K.legalActions(state, tables),
    hints: K.playHints(state, tables)
  };
}

export function createApi({ tables, sessions }) {
  function startGame(body) {
    const req = validateGameRequest(body, tables);
    const seed = req.seed === null ? randomSeed() : req.seed;
    const config = K.applyDifficulty({ ...req, seed }, req.difficulty, tables);
    let state;
    try {
      state = K.createGame({ ...config, runId: newRunId() }, tables);
    } catch (e) {
      throw new ApiError(e.code || 'E_KERNEL_EXCEPTION', e.message);
    }
    const sess = sessions.create(state, config);
    const v = view(state, tables);
    sessions.appendLog(sess, { type: 'run_start', sessionId: sess.sessionId, runId: state.runId, configHash: state.configHash, obsDigest: v.obsDigest });
    return { sessionId: sess.sessionId, configHash: state.configHash, runId: state.runId, ...v };
  }

  function mustSession(sid) {
    const s = sessions.get(sid);
    if (!s) throw new ApiError('E_STATE_NO_SESSION');
    return s;
  }

  function doAction(sid, body) {
    const sess = mustSession(sid);
    if (sess.suspended) throw new ApiError('E_STATE_SUSPENDED');
    if (sess.state.phase === 'settle') throw new ApiError('E_STATE_FINISHED');
    const action = validateActionSyntax(body && body.action);
    const r = K.reduce(sess.state, action, tables);
    if (r.error) throw new ApiError(r.error.code, r.error.message);
    if (r.illegal) {
      sess.illegalCount += 1;
      sess.consecutiveIllegal += 1;
      if (sess.consecutiveIllegal >= 3) sess.suspended = true;
      sessions.appendLog(sess, { runId: sess.state.runId, step: sess.step, action, illegal: true, code: r.code, reason: r.reason });
      return { __ok: false, error: { code: 'E_ACTION_ILLEGAL', message: r.reason, reason: r.code, suspended: sess.suspended } };
    }
    sess.consecutiveIllegal = 0;
    sess.state = r.state;
    sess.actions.push(action);
    sess.step += 1;
    const v = view(sess.state, tables);
    sessions.appendLog(sess, {
      runId: sess.state.runId, configHash: sess.state.configHash, step: sess.step, phase: sess.state.phase, stage: sess.state.stage,
      turn: sess.state.turn, obsDigest: v.obsDigest, action, events: r.events, rngDigest: K.digest(sess.state.rng), illegal: false
    });
    if (sess.state.phase === 'settle') {
      sessions.appendLog(sess, { type: 'run_end', runId: sess.state.runId, outcome: sess.state.outcome, steps: sess.step });
    }
    return { ...v, events: r.events, outcome: sess.state.outcome };
  }

  async function handle(method, pathname, body) {
    const parts = pathname.split('/').filter(Boolean); // ['api', ...]
    const route = parts.slice(1);
    if (route[0] === 'health' && method === 'GET') {
      return { version: API_VERSION, kernelVersion: K.KERNEL_VERSION, buildTime: BUILD_TIME, sessions: sessions.map.size };
    }
    if (route[0] === 'config' && route[1] === 'schema' && method === 'GET') return K.configSchema(tables);
    if (route[0] === 'seed' && method === 'POST') return { seed: randomSeed() };
    if (route[0] === 'game' && route.length === 1 && method === 'POST') return startGame(body);
    if (route[0] === 'game' && route.length === 2 && method === 'GET') {
      const s = mustSession(route[1]);
      return { sessionId: s.sessionId, runId: s.state.runId, suspended: s.suspended, ...view(s.state, tables) };
    }
    if (route[0] === 'game' && route[2] === 'action' && method === 'POST') return doAction(route[1], body);
    if (route[0] === 'game' && route[2] === 'reset' && method === 'POST') {
      const s = mustSession(route[1]);
      const cfg = (body && body.config) || { difficulty: s.config.difficulty, character: s.config.character, toggles: s.config.toggles };
      sessions.delete(route[1]);
      return startGame(cfg);
    }
    if (route[0] === 'replay' && method === 'POST') {
      if (!body || !body.config || !Array.isArray(body.actions)) throw new ApiError('E_API_BAD_REQUEST', '需要 {config, actions[]}');
      const req = validateGameRequest(body.config, tables);
      if (req.seed === null) throw new ApiError('E_API_BAD_REQUEST', 'replay 需要显式 seed');
      body.actions.forEach(validateActionSyntax);
      const config = K.applyDifficulty(req, req.difficulty, tables);
      const r = K.replay({ ...config, runId: body.config.runId }, body.actions, tables);
      return { steps: r.steps.map((s) => ({ obsDigest: s.obsDigest, events: s.events })), outcome: r.outcome };
    }
    if (route[0] === 'content' && route[1] && method === 'GET') {
      // 公开配表（玩家可见的图鉴信息）；不含任何对局状态
      const map = { skills: tables.skills, dice: tables.dice, monsters: tables.monsters, relics: tables.relics, schools: tables.schools, stages: tables.stages, difficulty: tables.difficulty };
      if (!map[route[1]]) throw new ApiError('E_API_NOT_FOUND', `未知配表 ${route[1]}`);
      return map[route[1]];
    }
    throw new ApiError('E_API_NOT_FOUND');
  }

  return { handle };
}
