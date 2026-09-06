#!/usr/bin/env node
'use strict';

// Generic PreToolUse guard. The engine is project-agnostic; the rules it enforces live in the
// project at .claude/agent-guard.json, so one installed plugin serves every repo.
//
// Protocol: exit 2 blocks the tool call and hands stderr back to the model. Exit 0 allows.
// Every failure path here deliberately allows — a guard that wedges the session is worse than
// one that misses a case.

const fs = require('fs');
const path = require('path');

function allow() {
  process.exit(0);
}

let payload;
try {
  payload = JSON.parse(fs.readFileSync(0, 'utf8'));
} catch {
  allow();
}

const projectDir = process.env.CLAUDE_PROJECT_DIR || process.cwd();
const claudeDir = path.join(projectDir, '.claude');

// agent-guard.json is the documented name; rules.json is accepted so an older setup keeps working.
let config = null;
for (const name of ['agent-guard.json', 'rules.json']) {
  const file = path.join(claudeDir, name);
  if (!fs.existsSync(file)) continue;
  try {
    config = JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (e) {
    // Worth saying out loud — a typo here silently disarms every rule — but not worth blocking on.
    process.stderr.write(`agent-guard: cannot parse ${file} — ${e.message}\n`);
    allow();
  }
  break;
}

// No rules file means the project has not opted in. Installed plugin, zero behaviour.
if (!config || !Array.isArray(config.rules)) allow();

const tool = String(payload.tool_name || '');
const input = payload.tool_input || {};

// Absolute in either convention: POSIX, drive-lettered, or a UNC share. Checked by hand rather
// than with path.isAbsolute because this runs on Windows against paths that are often POSIX.
function isAbsolute(p) {
  return p.startsWith('/') || p.startsWith('\\') || /^[A-Za-z]:/.test(p);
}

// A relative path would silently miss a rule written with a leading slash, and a guard that stops
// matching depending on how the call was phrased is worse than no guard. Resolve, then normalise
// separators so one pattern covers POSIX, Windows and UNC alike.
const rawPath = String(input.file_path || '');
const filePath = rawPath
  ? (isAbsolute(rawPath) ? rawPath : path.resolve(projectDir, rawPath)).replace(/\\/g, '/')
  : '';
const command = String(input.command || '');
const body = JSON.stringify(input);
const subject = filePath || command || tool;

function log(verdict, id) {
  try {
    const line = [new Date().toISOString(), verdict, id, subject].join('\t');
    fs.appendFileSync(path.join(claudeDir, 'agent-guard.log'), line + '\n');
  } catch {
    // Logging is best-effort and never affects the verdict.
  }
}

// A rule matches when every condition it declares matches. A rule that declares none matches
// nothing — the alternative is an empty rule silently blocking the entire session.
function matches(rule) {
  if (!(rule.tools || ['Edit', 'Write']).includes(tool)) return false;

  const tests = [
    [rule.path, () => filePath.includes(rule.path)],
    [rule.pathRegex, () => new RegExp(rule.pathRegex).test(filePath)],
    [rule.command, () => command.includes(rule.command)],
    [rule.commandRegex, () => new RegExp(rule.commandRegex).test(command)],
    [rule.content, () => body.includes(rule.content)],
    [rule.contentRegex, () => new RegExp(rule.contentRegex).test(body)],
  ];

  let declared = 0;
  for (const [value, test] of tests) {
    if (!value) continue;
    declared++;
    if (!test()) return false;
  }
  return declared > 0;
}

const bypassed = process.env.AGENT_GUARD_OFF === '1';

for (const rule of config.rules) {
  const id = rule.id || 'unnamed';

  let hit;
  try {
    hit = matches(rule);
  } catch (e) {
    process.stderr.write(`agent-guard: rule "${id}" is malformed — ${e.message}\n`);
    continue;
  }
  if (!hit) continue;

  if (bypassed) {
    log('BYPASS', id);
    continue;
  }

  // "warn" lets a candidate rule collect evidence in the log before it is promoted to blocking.
  if (rule.action === 'warn') {
    log('WARN', id);
    process.stderr.write(`agent-guard [${id}]: ${rule.message}\n`);
    continue;
  }

  log('BLOCK', id);
  process.stderr.write(`BLOCKED by rule "${id}".\n\n${rule.message}\n`);
  process.exit(2);
}

allow();
