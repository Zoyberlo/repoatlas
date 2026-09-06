#!/usr/bin/env node
'use strict';

// Self-test for guard.js. Builds a throwaway project directory per case, so nothing here depends
// on the machine it runs on. Usage: node scripts/selftest.js

const { spawnSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const GUARD = path.join(__dirname, 'guard.js');
const BS = String.fromCharCode(92);

const RULES = {
  rules: [
    {
      id: 'blades',
      path: '/backend/resources/views/',
      message: 'blades are not edited',
    },
    {
      id: 'printable-blocks',
      path: 'NotesPrintableBlocksBuilder',
      message: 'reuse build()',
    },
    {
      id: 'model-defaults',
      path: '/backend/app/Models/',
      content: 'disable_delete',
      message: 'fix the importer instead',
    },
    {
      id: 'git-state-changing',
      tools: ['Bash'],
      commandRegex: '\\bgit\\s+(add|commit|push)\\b',
      message: 'print the command, do not run it',
    },
    {
      id: 'trial-rule',
      action: 'warn',
      path: '/frontend/src/utils/',
      message: 'shared helper — fix locally instead',
    },
  ],
};

function makeProject(config) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'agent-guard-'));
  fs.mkdirSync(path.join(dir, '.claude'));
  if (config !== null) {
    const body = typeof config === 'string' ? config : JSON.stringify(config);
    fs.writeFileSync(path.join(dir, '.claude', 'agent-guard.json'), body);
  }
  return dir;
}

function run(payload, { config = RULES, env = {} } = {}) {
  const dir = makeProject(config);
  const res = spawnSync(process.execPath, [GUARD], {
    input: typeof payload === 'string' ? payload : JSON.stringify(payload),
    encoding: 'utf8',
    env: { ...process.env, CLAUDE_PROJECT_DIR: dir, ...env },
  });
  const log = path.join(dir, '.claude', 'agent-guard.log');
  const logged = fs.existsSync(log) ? fs.readFileSync(log, 'utf8') : '';
  fs.rmSync(dir, { recursive: true, force: true });
  return { code: res.status, stderr: res.stderr || '', log: logged };
}

const edit = (file, extra = {}) => ({
  tool_name: 'Edit',
  tool_input: { file_path: file, new_string: 'x', ...extra },
});

const cases = [];
const check = (name, fn) => cases.push({ name, fn });

// --- the three rules ported from the hardcoded script, both path shapes ------------------------

check('rule1 blade, POSIX path', () =>
  run(edit('/home/z/proj/backend/resources/views/documents/fs/cover.blade.php')).code === 2);

check('rule1 blade, UNC path with backslashes', () =>
  run(edit(BS + BS + 'wsl.localhost' + BS + 'Ubuntu-24.04' + BS + 'proj' + BS + 'backend' +
           BS + 'resources' + BS + 'views' + BS + 'pdf' + BS + 'a.blade.php')).code === 2);

check('rule2 NotesPrintableBlocksBuilder', () =>
  run(edit('/p/backend/app/Services/ReportFS/Notes/NotesPrintableBlocksBuilder.php')).code === 2);

check('rule3 model + disable_delete', () =>
  run(edit('/p/backend/app/Models/ReportFS/Depreciation.php',
           { new_string: 'disable_delete => true' })).code === 2);

// --- the negatives that matter: the guard must not become a nuisance ---------------------------

check('rule3 model, unrelated edit passes', () =>
  run(edit('/p/backend/app/Models/ReportFS/Depreciation.php')).code === 0);

check('importer mentioning disable_delete passes', () =>
  run(edit('/p/backend/app/Imports/FsReport/RevReportImport.php',
           { new_string: 'disable_delete' })).code === 0);

check('ordinary service file passes', () =>
  run(edit('/p/backend/app/Services/ReportFS/LoanService.php')).code === 0);

check('frontend component passes', () =>
  run(edit('/p/frontend/src/pages/ReportPage/Notes/NotesPage.vue')).code === 0);

// A relative path must reach the same verdict as an absolute one, or a rule silently stops
// applying depending on how the tool call happened to be phrased.
check('rule1 blade, relative path', () =>
  run(edit('backend/resources/views/welcome.blade.php')).code === 2);

check('rule3 model + flag, relative path', () =>
  run(edit('backend/app/Models/ReportFS/Depreciation.php',
           { new_string: 'disable_delete' })).code === 2);

check('relative path outside any rule still passes', () =>
  run(edit('backend/app/Services/ReportFS/LoanService.php')).code === 0);

// --- tool scoping ------------------------------------------------------------------------------

check('Bash rule blocks git commit', () =>
  run({ tool_name: 'Bash', tool_input: { command: 'git commit -m wip' } }).code === 2);

check('Bash rule ignores read-only git', () =>
  run({ tool_name: 'Bash', tool_input: { command: 'git status --porcelain' } }).code === 0);

check('path rule does not fire on Bash', () =>
  run({ tool_name: 'Bash', tool_input: { command: 'cat backend/resources/views/x.blade.php' } }).code === 0);

// --- warn action -------------------------------------------------------------------------------

check('warn rule allows but records', () => {
  const r = run(edit('/p/frontend/src/utils/FsFormulas/index.js'));
  return r.code === 0 && r.log.includes('WARN') && r.stderr.includes('fix locally');
});

// --- fail-open behaviour: a broken guard must never wedge a session -----------------------------

check('malformed stdin allows', () => run('not json at all').code === 0);

check('empty tool_input allows', () =>
  run({ tool_name: 'Edit', tool_input: {} }).code === 0);

check('no rules file at all allows', () =>
  run(edit('/p/backend/resources/views/x.blade.php'), { config: null }).code === 0);

check('malformed rules file allows and complains', () => {
  const r = run(edit('/p/backend/resources/views/x.blade.php'), { config: '{ broken' });
  return r.code === 0 && r.stderr.includes('cannot parse');
});

check('rule with no conditions matches nothing', () => {
  const config = { rules: [{ id: 'empty', message: 'should never fire' }] };
  return run(edit('/p/anything.php'), { config }).code === 0;
});

check('malformed regex in one rule does not disarm the others', () => {
  const config = { rules: [
    { id: 'broken', pathRegex: '([unclosed', message: 'bad' },
    { id: 'good', path: '/backend/resources/views/', message: 'blades' },
  ] };
  return run(edit('/p/backend/resources/views/x.blade.php'), { config }).code === 2;
});

// --- escape hatch ------------------------------------------------------------------------------

check('AGENT_GUARD_OFF bypasses and records', () => {
  const r = run(edit('/p/backend/resources/views/x.blade.php'), { env: { AGENT_GUARD_OFF: '1' } });
  return r.code === 0 && r.log.includes('BYPASS');
});

let passed = 0;
let failed = 0;
for (const { name, fn } of cases) {
  let ok = false;
  let err = '';
  try {
    ok = fn();
  } catch (e) {
    err = ` (threw: ${e.message})`;
  }
  if (ok) {
    passed++;
    console.log(`  ok   ${name}`);
  } else {
    failed++;
    console.log(`  FAIL ${name}${err}`);
  }
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed === 0 ? 0 : 1);
