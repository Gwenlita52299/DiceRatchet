// tests/bot-match.js —— 默认局「一局」的口径测量：N 局 greedy_counter 的结局与步数分布。
//
// 用法：
//   node tests/bot-match.js <count> [stageCount] [difficulty] [seedBase]        # 每 seed 一行 JSON（默认 200 6 hard 1）
//   node tests/bot-match.js <count> [stageCount] [difficulty] [seedBase] --stats # 追加 p50/p90 汇总表
//
// `--stats` 是 **Spec §8.6 表格的登记生成命令**：通关率、env 步 p50/p90、决策点 p50/p90。
// 口径：
//   env 步  = 每成功 reduce 计 1 步（内核 `state.steps`）
//   决策点  = `decisionIndex` 终值（Spec §4.3 / B-12 规则，见 scripts/bots.js::decisionKey）
// 无 `--stats` 时 stdout 仍是纯 JSON 数组（tests/golden/test_bot_match.py 依赖此格式）。
import { loadTables } from '../kernel/loader.js';
import { runGame } from '../scripts/bots.js';

const argv = process.argv.slice(2);
const stats = argv.includes('--stats');
const pos = argv.filter((a) => !a.startsWith('--'));
const count = parseInt(pos[0], 10) || 200;
const stageCount = parseInt(pos[1], 10) || 6;
const difficulty = pos[2] || 'hard';
const seedBase = parseInt(pos[3], 10) || 1;

const tables = loadTables();
const out = [];
for (let i = 0; i < count; i++) {
  const seed = seedBase + i;
  try {
    const r = runGame({ difficulty, seed, strategy: 'greedy_counter', stageCount, tables, maxSteps: 4000 });
    out.push({ seed, outcome: r.outcome, stage: r.stage, steps: r.steps, decisions: r.decisions });
  } catch (err) {
    out.push({ seed, error: String(err.message).slice(0, 300) });
  }
}

if (stats) {
  const ok = out.filter((x) => !x.error);
  const pct = (arr, p) => {
    if (!arr.length) return NaN;
    const s = [...arr].sort((a, b) => a - b);
    const idx = Math.min(s.length - 1, Math.max(0, Math.ceil((p / 100) * s.length) - 1));
    return s[idx];
  };
  const wins = ok.filter((x) => x.outcome === 'win').length;
  const steps = ok.map((x) => x.steps);
  const decisions = ok.map((x) => x.decisions);
  const rate = ok.length ? (wins * 100) / ok.length : NaN;
  console.log(`# 默认局口径（Spec §8.6）：difficulty=${difficulty} stageCount=${stageCount} `
    + `n=${out.length} seed=${seedBase}..${seedBase + count - 1}`);
  console.log(`| 局型 | greedy_counter 通关率 | env 步 p50/p90 | 决策点 p50/p90 | 错误局 |`);
  console.log(`|---|---|---|---|---|`);
  console.log(`| ${difficulty} × ${stageCount} 关 | ${rate.toFixed(1)}% (${wins}/${ok.length}) `
    + `| ${pct(steps, 50)} / ${pct(steps, 90)} | ${pct(decisions, 50)} / ${pct(decisions, 90)} `
    + `| ${out.length - ok.length} |`);
  console.log(`\n复现：node src/game/tests/bot-match.js ${count} ${stageCount} ${difficulty} ${seedBase} --stats`);
}

console.log(JSON.stringify(out));
