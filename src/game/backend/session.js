// backend/session.js — 会话表（内存 Map + TTL 清理）与动作轨迹（runs/<runId>/actions.jsonl）

import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const TTL_MS = 6 * 60 * 60 * 1000;

export class SessionStore {
  constructor({ runsDir, log = true } = {}) {
    this.map = new Map();
    this.runsDir = runsDir;
    this.log = log;
    this.timer = setInterval(() => this.sweep(), 10 * 60 * 1000);
    this.timer.unref();
  }

  create(state, config) {
    const sessionId = `s-${crypto.randomUUID()}`;
    const sess = { sessionId, state, config, actions: [], createdAt: Date.now(), lastAccess: Date.now(), consecutiveIllegal: 0, illegalCount: 0, suspended: false, step: 0 };
    this.map.set(sessionId, sess);
    return sess;
  }

  get(sessionId) {
    const s = this.map.get(sessionId);
    if (s) s.lastAccess = Date.now();
    return s || null;
  }

  delete(sessionId) { this.map.delete(sessionId); }

  sweep() {
    const now = Date.now();
    for (const [id, s] of this.map) if (now - s.lastAccess > TTL_MS) this.map.delete(id);
  }

  appendLog(sess, record) {
    if (!this.log || !this.runsDir || !sess.config.debug.logActions) return;
    try {
      const dir = path.join(this.runsDir, sess.state.runId);
      fs.mkdirSync(dir, { recursive: true });
      fs.appendFileSync(path.join(dir, 'actions.jsonl'), JSON.stringify({ ...record, ts: new Date().toISOString() }) + '\n');
    } catch { /* 日志失败不影响对局 */ }
  }
}
