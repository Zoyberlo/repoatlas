#!/usr/bin/env node
'use strict';

// Checks a project's rules file rather than the engine. `selftest.js` proves guard.js works;
// this proves the rules in front of it are alive.
//
// The failure it exists for: a rule whose path was mistyped, or whose directory has since been
// renamed, never fires. Nothing says so. It sits in the file looking like coverage, and the
// behaviour it was written to stop goes unstopped — which is worse than having written no rule,
// because someone believes it is there.
//
// Usage:
//   node scripts/check.js                       # rules of the current project
//   node scripts/check.js --project /path/to    # another project
//   node scripts/check.js --cases cases.json    # also assert what each case should trip

const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const GUARD = path.join(__dirname, 'guard.js');

function arg(name, fallback) {
  const i = process.argv.indexOf(name);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const projectDir = path.resolve(arg('--project', process.env.CLAUDE_PROJECT_DIR || process.cwd()));
const casesFile = arg('--cases', null);

function readRules() {
  for (const name of ['agent-guard.json', 'rules.json']) {
    const file = path.join(projectDir, '.claude', name);
    if (!fs.existsSync(file)) continue;
    try {
      return { file, config: JSON.parse(fs.readFileSync(file, 'utf8')) };
    } catch (e) {
      console.error(`cannot parse ${file} — ${e.message}`);
      process.exit(2);
    }
  }
  console.error(`no .claude/agent-guard.json under ${projectDir}`);
  process.exit(2);
}

const { file, config } = readRules();
const rules = Array.isArray(config.rules) ? config.rules : [];
console.log(`${file}: ${rules.length} rule(s)\n`);

// Ask the guard itself, so this checks the engine's real behaviour rather than a second
// implementation of the matching that could drift from it.
function fire(payload) {
  const result = spawnSync(process.execPath, [GUARD], {
    input: JSON.stringify(payload),
    encoding: 'utf8',
    env: { ...process.env, CLAUDE_PROJECT_DIR: projectDir, AGENT_GUARD_QUIET: '1' },
  });
  const out = (result.stderr || '') + (result.stdout || '');
  const block = out.match(/BLOCKED by rule "([^"]+)"/);
  const warn = out.match(/agent-guard \[([^\]]+)\]/);
  return {
    id: block ? block[1] : warn ? warn[1] : null,
    verdict: block ? 'block' : warn ? 'warn' : 'none',
    code: result.status,
  };
}

// A probe that a rule should match, derived from the rule's own conditions. A path rule is asked
// with its own substring; a regex rule cannot be inverted, so it is reported as unprovable rather
// than guessed at — a wrong guess would report a live rule as dead.
function probe(rule) {
  const tools = rule.tools || ['Edit', 'Write'];
  const tool = tools[0];
  if (tool === 'Bash') {
    if (rule.command) return { tool_name: 'Bash', tool_input: { command: rule.command } };
    return null;
  }
  if (!rule.path) return null;
  const file_path = rule.path.startsWith('/')
    ? path.posix.join(projectDir.replace(/\\/g, '/'), rule.path, 'probe.txt')
    : `${projectDir.replace(/\\/g, '/')}/probe-${rule.path}.txt`;
  const input = { file_path };
  if (rule.content) input.content = rule.content;
  return { tool_name: tool, tool_input: input };
}

let dead = 0;
const unprovable = new Set();
const live = new Set();

for (const rule of rules) {
  const id = rule.id || '(unnamed)';
  const declared = ['path', 'pathRegex', 'command', 'commandRegex', 'content', 'contentRegex']
    .filter((k) => rule[k]);
  if (declared.length === 0) {
    console.log(`  DEAD       ${id} — declares no condition, so it can never match`);
    dead++;
    continue;
  }
  const payload = probe(rule);
  if (!payload) {
    console.log(`  unprovable ${id} — ${declared.join(', ')}; give it a case in --cases`);
    unprovable.add(id);
    continue;
  }
  const got = fire(payload);
  if (got.id === id) {
    console.log(`  live       ${id} (${got.verdict})`);
    live.add(id);
  } else if (got.id) {
    console.log(`  SHADOWED   ${id} — its own probe trips "${got.id}" first`);
    dead++;
  } else {
    console.log(`  DEAD       ${id} — its own conditions do not match it`);
    dead++;
  }
}

let failed = dead;

// Cases are how a regex rule earns "live", and how a rule proves it stays off the paths it should
// not touch. Format: [{ "label": …, "payload": {…}, "expect": "rule-id"|null, "verdict": "block" }]
if (casesFile) {
  const cases = JSON.parse(fs.readFileSync(casesFile, 'utf8'));
  console.log(`\n${cases.length} case(s) from ${casesFile}`);
  for (const c of cases) {
    const got = fire(c.payload);
    const wantId = c.expect ?? null;
    const wantVerdict = c.verdict ?? (wantId ? null : 'none');
    const ok = got.id === wantId && (!wantVerdict || got.verdict === wantVerdict);
    failed += ok ? 0 : 1;
    // A rule a case fires is proven, whatever the probe could not derive.
    if (ok && got.id) {
      live.add(got.id);
      unprovable.delete(got.id);
    }
    const detail = `${got.id || 'nothing'} (${got.verdict}, exit ${got.code})`;
    const wanted = ok ? '' : `  wanted ${wantId || 'nothing'}${wantVerdict ? ` (${wantVerdict})` : ''}`;
    console.log(`  ${ok ? 'ok  ' : 'FAIL'} ${(c.label || '').padEnd(28)} ${detail}${wanted}`);
  }
}

console.log(`\n${live.size} live, ${unprovable.size} unprovable, ${dead} dead`);
if (unprovable.size) {
  console.log(`unproven: ${[...unprovable].join(', ')} — a regex rule needs a case`);
}
if (failed) {
  console.error('\nA dead rule is worse than a missing one: it looks like coverage.');
  process.exit(1);
}
