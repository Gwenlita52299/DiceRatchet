#!/usr/bin/env node
// server.js — HTTP 入口：静态托管 frontend/ + /api 路由。零 npm 依赖。
// 用法：node server.js [--port 8080] [--host 127.0.0.1]

import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadTables } from './kernel/loader.js';
import { createApi } from './backend/api.js';
import { SessionStore } from './backend/session.js';
import { ApiError } from './backend/errors.js';

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND = path.join(ROOT, 'frontend');
const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon'
};

function arg(name, def) {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : def;
}

export function createServer({ runsDir = path.join(ROOT, 'runs'), log = true } = {}) {
  const tables = loadTables();
  const sessions = new SessionStore({ runsDir, log });
  const api = createApi({ tables, sessions });

  const send = (res, status, obj) => {
    const body = JSON.stringify(obj);
    res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
    res.end(body);
  };

  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://localhost');
    if (url.pathname.startsWith('/api/')) {
      let raw = '';
      req.on('data', (c) => {
        raw += c;
        if (raw.length > 1e6) { req.destroy(); }
      });
      req.on('end', async () => {
        try {
          let body = null;
          if (raw) {
            try { body = JSON.parse(raw); } catch { throw new ApiError('E_API_BAD_JSON'); }
          }
          const data = await api.handle(req.method, url.pathname, body);
          if (data && data.__ok === false) return send(res, 200, { ok: false, error: data.error });
          send(res, 200, { ok: true, data });
        } catch (e) {
          const err = e instanceof ApiError ? e : new ApiError('E_KERNEL_EXCEPTION', e.message);
          if (!(e instanceof ApiError)) console.error(e);
          send(res, err.status, { ok: false, error: { code: err.code, message: err.message, ...err.extra } });
        }
      });
      return;
    }
    // 静态文件
    let p = decodeURIComponent(url.pathname);
    if (p === '/') p = '/index.html';
    const file = path.normalize(path.join(FRONTEND, p));
    if (!file.startsWith(FRONTEND)) { res.writeHead(403); return res.end('forbidden'); }
    fs.readFile(file, (err, buf) => {
      if (err) { res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' }); return res.end('not found'); }
      res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-cache' });
      res.end(buf);
    });
  });
  return { server, tables, sessions };
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  const port = Number(arg('port', process.env.PORT || 8080));
  const host = arg('host', '127.0.0.1');
  const { server } = createServer();
  server.listen(port, host, () => {
    console.log(`GameRSI 骰子 roguelike 已启动 → http://${host}:${port}`);
  });
}
