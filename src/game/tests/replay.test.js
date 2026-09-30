// T-09 确定性 / golden 对拍：重放动作脚本，obsDigest 与 rngDigest 序列逐行相等
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as K from '../kernel/index.js';
import { tables } from './helpers.js';

const DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), 'golden');
const files = fs.existsSync(DIR) ? fs.readdirSync(DIR).filter((f) => f.endsWith('.jsonl')) : [];

test('至少 3 组 golden 快照（含 1 组全开关关闭）', () => {
  assert.ok(files.length >= 3);
  const metas = files.map((f) => JSON.parse(fs.readFileSync(path.join(DIR, f), 'utf8').split('\n')[0]));
  assert.ok(metas.some((m) => Object.values(m.config.toggles).every((v) => v === false)));
});

for (const f of files) {
  test(`golden ${f} 逐行一致`, () => {
    const lines = fs.readFileSync(path.join(DIR, f), 'utf8').trim().split('\n').map((l) => JSON.parse(l));
    const meta = lines[0];
    const steps = lines.filter((l) => typeof l.step === 'number');
    let state = K.createGame(meta.config, tables);
    assert.equal(K.digest(K.observe(state, 'full', tables)), steps[0].obsDigest, 'step 0');
    for (const s of steps.slice(1)) {
      const r = K.reduce(state, s.action, tables);
      assert.equal(!!r.illegal, !!s.illegal, `step ${s.step} illegal flag`);
      state = r.state;
      assert.equal(K.digest(K.observe(state, 'full', tables)), s.obsDigest, `obsDigest 分歧 @ step ${s.step} ${JSON.stringify(s.action)}`);
      assert.equal(K.digest(state.rng), s.rngDigest, `rngDigest 分歧 @ step ${s.step}`);
    }
    // replay 便利函数与逐步 reduce 一致
    const rp = K.replay(meta.config, steps.slice(1).map((s) => s.action), tables);
    assert.deepEqual(rp.digests, steps.map((s) => s.obsDigest));
  });
}
