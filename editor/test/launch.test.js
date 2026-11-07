// How the extension starts the `ai` CLI. The plan tests run anywhere; the last ones start real
// processes and only run on Windows (the Windows shims job in CI).
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');
const { launch, plan, promptFile } = require('../launch.js');

const NPM_SHIM = `@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0

IF EXIST "%dp0%\\node.exe" (
  SET "_prog=%dp0%\\node.exe"
) ELSE (
  SET "_prog=node"
  SET PATHEXT=%PATHEXT:;.JS;=;%
)

endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\\node_modules\\omni-ai-cli\\bin\\ai.js" %*
`;
// What scripts/install.ps1 writes: not an npm shim, so it goes through cmd.exe
const INSTALLER_LAUNCHER = '@echo off\r\nset "PYTHONPATH=C:\\omni"\r\n"C:\\Python312\\python.exe" -P -m omni_ai %*\r\n';
const ATTACK = '" & echo planted> proof.txt & "';
const ECHO_ARGS = "require('fs').writeFileSync(process.env.RECORD, JSON.stringify(process.argv.slice(2)));\n";

function folder() {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'omni-launch-')));
  test.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

function npmInstall(dir, script = '') {
  fs.mkdirSync(path.join(dir, 'node_modules', 'omni-ai-cli', 'bin'), { recursive: true });
  fs.writeFileSync(path.join(dir, 'node_modules', 'omni-ai-cli', 'bin', 'ai.js'), script);
  fs.writeFileSync(path.join(dir, 'ai.cmd'), NPM_SHIM);
  return path.join(dir, 'node_modules', 'omni-ai-cli', 'bin', 'ai.js');
}

test('outside Windows the command is left as it is', () => {
  assert.deepEqual(plan('ai', ['--version'], {}, 'linux'), { file: 'ai', args: ['--version'] });
});

test('an npm install runs Node with the script, not cmd.exe', () => {
  const dir = folder();
  const script = npmInstall(dir);
  fs.writeFileSync(path.join(dir, 'node.exe'), '');
  const how = plan('ai', ['--claude', ATTACK], { PATH: dir, PATHEXT: '.COM;.EXE;.BAT;.CMD' }, 'win32');
  assert.deepEqual(how, { file: path.join(dir, 'node.exe'), args: [script, '--claude', ATTACK] });
});

test('a copy of the environment with Path in mixed case still works', () => {
  const dir = folder();
  npmInstall(dir);
  fs.writeFileSync(path.join(dir, 'node.exe'), '');
  assert.equal(plan('ai', [], { Path: dir, PathExt: '.CMD' }, 'win32').file, path.join(dir, 'node.exe'));
});

test('a relative PATH entry is never searched', () => {
  const dir = folder();
  npmInstall(dir);
  const relative = path.relative(process.cwd(), dir);
  assert.deepEqual(plan('ai', [], { PATH: relative, PATHEXT: '.CMD' }, 'win32'), { file: 'ai', args: [] });
});

test('another launcher goes through cmd.exe with every part quoted', () => {
  const dir = folder();
  fs.writeFileSync(path.join(dir, 'ai.cmd'), INSTALLER_LAUNCHER);
  const env = { PATH: dir, PATHEXT: '.cmd', ComSpec: 'C:\\Windows\\System32\\cmd.exe' };
  const how = plan('ai', ['--prompt-file', 'C:\\Users\\Ana Maria\\prompt.txt'], env, 'win32');
  assert.equal(how.file, env.ComSpec);
  assert.equal(how.verbatim, true);
  assert.deepEqual(how.args.slice(0, 3), ['/d', '/s', '/c']);
  assert.equal(how.args[3], `""${path.join(dir, 'ai.cmd')}" "--prompt-file" "C:\\Users\\Ana Maria\\prompt.txt""`);
});

test('another launcher refuses what cmd.exe would read as a command', () => {
  const dir = folder();
  fs.writeFileSync(path.join(dir, 'ai.cmd'), INSTALLER_LAUNCHER);
  for (const bad of [ATTACK, '%PATH%', '!x!', 'a\nb', 'C:\\folder\\']) {
    assert.equal(plan('ai', [bad], { PATH: dir, PATHEXT: '.CMD' }, 'win32'), null, bad);
  }
});

test('the prompt file is private and removed afterwards', () => {
  const sent = promptFile('olá ✓');
  assert.equal(fs.readFileSync(sent.file, 'utf8'), 'olá ✓');
  if (process.platform !== 'win32') {
    assert.equal(fs.statSync(sent.file).mode & 0o077, 0);
    assert.equal(fs.statSync(path.dirname(sent.file)).mode & 0o077, 0);
  }
  sent.remove();
  assert.ok(!fs.existsSync(path.dirname(sent.file)));
});

const windows = { skip: process.platform !== 'win32' && 'real Windows only' };

function finished(child) {
  return new Promise((resolve, reject) => { child.on('error', reject); child.on('close', resolve); });
}

test('Windows: the extension starts an npm-installed ai by name', windows, async () => {
  const dir = folder();
  npmInstall(dir, ECHO_ARGS);
  const record = path.join(dir, 'args.json');
  const env = { ...process.env, PATH: `${dir};${process.env.PATH}`, RECORD: record };
  // What failed before: Node alone does not find ai.cmd
  assert.equal(spawnSync('ai', ['--version'], { env, cwd: dir }).error.code, 'ENOENT');
  const code = await finished(launch('ai', ['--claude', ATTACK], { env, cwd: dir }));
  assert.equal(code, 0);
  assert.deepEqual(JSON.parse(fs.readFileSync(record, 'utf8')), ['--claude', ATTACK]);
  assert.ok(!fs.existsSync(path.join(dir, 'proof.txt')));
});

test('Windows: another launcher runs through cmd.exe and gets its arguments intact', windows, async () => {
  const dir = folder();
  const script = path.join(dir, 'echo.js');
  fs.writeFileSync(script, ECHO_ARGS);
  fs.writeFileSync(path.join(dir, 'ai.cmd'), `@echo off\r\n"${process.execPath}" "${script}" %*\r\n`);
  const record = path.join(dir, 'args.json');
  const env = { ...process.env, PATH: `${dir};${process.env.PATH}`, RECORD: record };
  const file = path.join(dir, 'with space', 'prompt.txt');
  const code = await finished(launch('ai', ['--prompt-file', file], { env, cwd: dir }));
  assert.equal(code, 0);
  assert.deepEqual(JSON.parse(fs.readFileSync(record, 'utf8')), ['--prompt-file', file]);
});
