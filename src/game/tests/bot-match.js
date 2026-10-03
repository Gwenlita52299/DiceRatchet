// 临时/测试脚本：跑 N 局 greedy_counter（hard × stageCount），输出每 seed 结局 JSON。
// 用法：node src/game/tests/bot-match.js <count> [stageCount] [difficulty] [seedBase]
import { loadTables } from '../kernel/loader.js';
import { runGame } from '../scripts/bots.js';

const count = parseInt(process.argv[2], 10) || 200;
const stageCount = parseInt(process.argv[3], 10) || 6;
const difficulty = process.argv[4] || 'hard';
const seedBase = parseInt(process.argv[5], 10) || 1;
const tables = loadTables();
const out = [];
for (let i = 0; i < count; i++) {
  const seed = seedBase + i;
  try {
    const r = runGame({ difficulty, seed, strategy: 'greedy_counter', stageCount, tables, maxSteps: 4000 });
    out.push({ seed, outcome: r.outcome, stage: r.stage, steps: r.steps });
  } catch (err) {
    out.push({ seed, error: String(err.message).slice(0, 300) });
  }
}
console.log(JSON.stringify(out));
