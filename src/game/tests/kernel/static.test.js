// K-2 / K-3 / §5.2 纪律的 CI 扫描
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const kernelFiles = fs.readdirSync(path.join(ROOT, 'kernel')).filter((f) => f.endsWith('.js'));
const strip = (src) => src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '').replace(/'(?:\\.|[^'\\])*'/g, "''").replace(/`(?:\\.|[^`\\])*`/g, '``');

test('K-2 内核零环境依赖（Math.random/Date/performance/process/console/fetch/BigInt）', () => {
  for (const f of kernelFiles) {
    const src = strip(fs.readFileSync(path.join(ROOT, 'kernel', f), 'utf8'));
    for (const bad of [/Math\.random/, /\bDate\b/, /\bperformance\b/, /\bprocess\./, /\bconsole\./, /\bfetch\(/, /\bBigInt\b/, /\bundefined\b\s*[,;)]/]) {
      if (f === 'loader.js' && /process/.test(bad.source)) continue;
      assert.ok(!bad.test(src), `${f} 命中 ${bad}`);
    }
    if (f !== 'loader.js') assert.ok(!/node:fs/.test(fs.readFileSync(path.join(ROOT, 'kernel', f), 'utf8')), `${f} 不得读文件`);
  }
});

test('K-3 内核无浮点字面量', () => {
  for (const f of kernelFiles) {
    const src = strip(fs.readFileSync(path.join(ROOT, 'kernel', f), 'utf8'));
    assert.ok(!/(?<![\w.])\d+\.\d+/.test(src), `${f} 含浮点字面量`);
  }
});

test('§5.2 observe 显式构造，不透传 state', () => {
  const src = fs.readFileSync(path.join(ROOT, 'kernel', 'index.js'), 'utf8');
  const body = src.slice(src.indexOf('export function observe('), src.indexOf('export function playHints('));
  assert.ok(!/\.\.\.state\b/.test(body));
  assert.ok(!/JSON\.parse\(JSON\.stringify\(state/.test(body));
});

test('kernel/ 不依赖 backend/ 与 frontend/；frontend 不含伤害公式', () => {
  for (const f of kernelFiles) {
    const src = fs.readFileSync(path.join(ROOT, 'kernel', f), 'utf8');
    assert.ok(!/from ['"]\.\.\/(backend|frontend)/.test(src), f);
  }
  const walk = (d) => fs.readdirSync(d, { withFileTypes: true }).flatMap((e) => (e.isDirectory() ? walk(path.join(d, e.name)) : [path.join(d, e.name)]));
  for (const f of walk(path.join(ROOT, 'frontend', 'js'))) {
    const src = fs.readFileSync(f, 'utf8');
    assert.ok(!/counterMulPermille\.advantage\s*\*/.test(src) && !/Math\.floor\([^)]*mulPermille/.test(src), `${f} 含规则计算`);
  }
});
