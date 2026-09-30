// kernel/loader.js — 配表加载 + 校验 + 冻结（GAME-01 §1.1 / §8）
// 这是 kernel/ 内唯一允许读文件的模块。

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseConstraint } from './slots.js';
import { computeRiskTier } from './risk.js';

export { computeRiskTier, faceHasSelfCorruption, selfCorruptionValue } from './risk.js';

const ID_RE = /^[a-z][a-z0-9_]*$/;
const FACE_KINDS = ['purge', 'ward', 'mend', 'status', 'selfCorruption', 'draw', 'reroll', 'modify', 'gold', 'nothing', 'duplicate_face'];
const STATUS_KINDS = ['poison', 'burn', 'weak', 'strength', 'regen'];
const SCHOOL_ENUM = ['blade', 'arcana', 'verdant', 'neutral'];
const TABLE_FILES = ['schools', 'difficulty', 'dice', 'skills', 'characters', 'monsters', 'stages', 'events', 'relics', 'shops'];

export const DEFAULT_CONTENT_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'content');

function fail(code, msg) {
  const e = new Error(`${code}: ${msg}`);
  e.code = code;
  throw e;
}

function deepFreeze(o) {
  if (o && typeof o === 'object' && !Object.isFrozen(o)) {
    Object.freeze(o);
    for (const k of Object.keys(o)) deepFreeze(o[k]);
  }
  return o;
}

function validateFace(id, f, i) {
  if (f.faceIndex !== i) fail('E_CONTENT_FACES', `${id} faceIndex ${i}`);
  if (!FACE_KINDS.includes(f.kind)) fail('E_CONTENT_FACE_KIND', `${id}[${i}] kind ${f.kind}`);
  if (f.kind === 'status' && !STATUS_KINDS.includes(f.statusKind)) fail('E_CONTENT_FACE_KIND', `${id}[${i}] statusKind`);
  if (!SCHOOL_ENUM.includes(f.school)) fail('E_CONTENT_FACE_KIND', `${id}[${i}] school ${f.school}`);
  if (!Number.isInteger(f.value)) fail('E_CONTENT_FACE_KIND', `${id}[${i}] value`);
}

function validateDie(id, def, warnings) {
  if (!ID_RE.test(id)) fail('E_CONTENT_ID_FORMAT', id);
  if (!Array.isArray(def.faces) || def.faces.length !== 6) fail('E_CONTENT_FACES', `${id} must have 6 faces`);
  def.faces.forEach((f, i) => validateFace(id, f, i));
  const tier = computeRiskTier(def);
  if (def.riskTier && def.riskTier !== tier) fail('E_CONTENT_RISK_MISMATCH', `${id}: declared ${def.riskTier}, computed ${tier}`);
  // 学派设计规则（GAME-00 §5.5，决策 D-10）
  if (def.category === 'skill' && def.school !== 'neutral') {
    const own = def.faces.filter((f) => f.school === def.school).length;
    const cross = def.faces.filter((f) => f.school !== def.school && f.school !== 'neutral').length;
    if (own < 2 || cross < 1) fail('E_CONTENT_SCHOOL_RULE', `${id}: own=${own} cross=${cross}`);
  }
  void warnings;
  return tier;
}

export function loadTables(dir = DEFAULT_CONTENT_DIR) {
  const raw = {};
  for (const name of TABLE_FILES) {
    const file = path.join(dir, `${name}.json`);
    raw[name] = JSON.parse(fs.readFileSync(file, 'utf8'));
    if (!raw[name].$schemaVersion) fail('E_CONTENT_SCHEMA', `${name}.json missing $schemaVersion`);
  }
  return buildTables(raw);
}

export function buildTables(raw) {
  const warnings = [];
  const dice = {};
  const skills = {};
  for (const [id, def] of Object.entries(raw.dice.dice)) dice[id] = { ...def, id, riskTier: validateDie(id, def, warnings) };
  for (const [id, def] of Object.entries(raw.skills.skills)) skills[id] = { ...def, id, riskTier: validateDie(id, def, warnings) };
  const allDice = { ...dice, ...skills };

  const forgeFaces = {};
  for (const [id, ff] of Object.entries(raw.dice.forgeFaces)) {
    if (!ID_RE.test(id)) fail('E_CONTENT_ID_FORMAT', id);
    forgeFaces[id] = { id, weight: ff.weight, face: ff.face };
  }

  const monsters = {};
  for (const [id, m] of Object.entries(raw.monsters.monsters)) {
    if (!ID_RE.test(id)) fail('E_CONTENT_ID_FORMAT', id);
    if (!SCHOOL_ENUM.includes(m.school)) fail('E_CONTENT_MONSTER', `${id} school`);
    if (!['normal', 'elite', 'boss'].includes(m.tier)) fail('E_CONTENT_MONSTER', `${id} tier`);
    const slotSets = m.phases ? m.phases.map((p) => p.slots) : [m.slots];
    for (const ss of slotSets) {
      if (!ss.length) fail('E_CONTENT_MONSTER', `${id} slots empty`);
      for (const s of ss) {
        parseConstraint(s.constraint);
        if (s.effect.kind === 'add_curse' && !dice[s.effect.dieId]) fail('E_CONTENT_DANGLING_REF', `${id} → ${s.effect.dieId}`);
      }
    }
    monsters[id] = { ...m, id };
  }
  const groups = {};
  for (const [id, g] of Object.entries(raw.monsters.groups)) {
    for (const mid of g.members) if (!monsters[mid]) fail('E_CONTENT_DANGLING_REF', `${id} → ${mid}`);
    groups[id] = { ...g, id };
  }

  const st = raw.stages;
  for (let i = 1; i <= st.stageCount; i++) if (!st.stages.find((s) => s.stage === i)) fail('E_CONTENT_STAGES', `missing stage ${i}`);
  const last = st.stages.find((s) => s.stage === st.stageCount);
  if (!last.boss) fail('E_CONTENT_STAGES', 'final stage must have boss');
  for (const s of st.stages) {
    if (s.boss && !monsters[s.bossId]) fail('E_CONTENT_DANGLING_REF', `stage ${s.stage} boss`);
    if (s.nodeBudget.length !== s.rows) fail('E_CONTENT_STAGES', `stage ${s.stage} nodeBudget`);
  }

  const events = {};
  for (const [id, ev] of Object.entries(raw.events.events)) {
    if (!ID_RE.test(id)) fail('E_CONTENT_ID_FORMAT', id);
    if (ev.options.length < 3) fail('E_CONTENT_EVENT', `${id} needs ≥3 options`);
    if (!ev.options.some((o) => o.risk === 'positive')) fail('E_CONTENT_EVENT', `${id} needs a positive option`);
    if (!ev.options.some((o) => o.risk === 'risk')) fail('E_CONTENT_EVENT', `${id} needs a risky option`);
    for (const o of ev.options) for (const out of o.outcomes) for (const eff of out.effect) {
      if (eff.defId && !allDice[eff.defId]) fail('E_CONTENT_DANGLING_REF', `${id} → ${eff.defId}`);
      if (eff.faceId && !forgeFaces[eff.faceId]) fail('E_CONTENT_DANGLING_REF', `${id} → ${eff.faceId}`);
    }
    events[id] = { ...ev, id };
  }

  const relics = {};
  for (const [id, r] of Object.entries(raw.relics.relics)) {
    if (!ID_RE.test(id)) fail('E_CONTENT_ID_FORMAT', id);
    relics[id] = { ...r, id };
  }
  const rl = Object.values(relics);
  if (rl.filter((r) => r.tags.includes('school')).length < 6) fail('E_CONTENT_RELIC_RULE', 'need ≥6 school relics');
  if (rl.filter((r) => r.tags.includes('cost')).length < 2) fail('E_CONTENT_RELIC_RULE', 'need ≥2 cost relics');

  for (const [cid, c] of Object.entries(raw.characters.characters)) {
    for (const d of c.startingDeck) if (!allDice[d]) fail('E_CONTENT_DANGLING_REF', `${cid} → ${d}`);
    for (const s of c.startingSlots) parseConstraint(s.constraint);
  }

  return deepFreeze({
    schemaVersion: 'content-1',
    schools: raw.schools,
    difficulty: raw.difficulty,
    dice, skills, allDice, forgeFaces,
    characters: raw.characters.characters,
    monsters, groups,
    stages: st,
    events, relics,
    shops: raw.shops,
    warnings
  });
}

/** 按开关过滤（relics=false 时遗物/遗物事件被过滤并记 warning；events=false 时事件池不加载） */
export function filterByToggles(tables, toggles) {
  const warnings = [];
  let events = tables.events;
  let relics = tables.relics;
  if (!toggles.events) events = {};
  if (!toggles.relics) {
    relics = {};
    const kept = {};
    for (const [id, ev] of Object.entries(events)) {
      if (ev.relicsOnly) warnings.push(`event ${id} filtered: relics disabled`);
      else kept[id] = ev;
    }
    events = kept;
  }
  return { ...tables, events, relics, warnings: [...tables.warnings, ...warnings] };
}
