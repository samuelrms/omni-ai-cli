#!/usr/bin/env node
// npm launcher: finds Python 3.11+ and runs the bundled smart_ai.py with the same arguments.
'use strict';
const { spawnSync, spawn } = require('child_process');
const path = require('path');

const root = path.resolve(__dirname, '..');
const script = path.join(root, 'smart_ai.py');
const candidates = process.env.SMART_AI_PYTHON
  ? [[process.env.SMART_AI_PYTHON]]
  : process.platform === 'win32'
    ? [['py', '-3'], ['python'], ['python3']]
    : [['python3'], ['python']];

const fs = require('fs');
const os = require('os');
const home = os.homedir();
const dataDir = process.platform === 'win32'
  ? path.join(process.env.LOCALAPPDATA || path.join(home, 'AppData', 'Local'), 'smart-ai')
  : path.join(home, '.local', 'share', 'smart-ai');
const cachedPython = path.join(home, '.config', 'smart-ai', 'python-path');

function works(cmd, pre) {
  const probe = spawnSync(cmd, [...pre, '-c', 'import sys; print(sys.version_info >= (3, 11))'], { encoding: 'utf8' });
  return probe.status === 0 && probe.stdout.trim() === 'True';
}

function findPython() {
  try {
    const saved = fs.readFileSync(cachedPython, 'utf8').trim();
    if (saved && works(saved, [])) return [saved, []];
  } catch (_) { /* first run */ }
  for (const [cmd, ...pre] of candidates) {
    if (works(cmd, pre)) return [cmd, pre];
  }
  return null;
}

// No usable Python: fetch a private Python 3.12 with uv (no sudo, system Python untouched)
function installPython() {
  if (process.env.SMART_AI_NO_AUTO_PYTHON === '1') return null;
  console.error('Python 3.11+ não encontrado. Instalando um Python 3.12 só para o Smart AI (sem sudo)...');
  const uvDir = path.join(dataDir, 'uv');
  const uv = path.join(uvDir, process.platform === 'win32' ? 'uv.exe' : 'uv');
  const env = { ...process.env, UV_INSTALL_DIR: uvDir, UV_NO_MODIFY_PATH: '1',
    UV_PYTHON_INSTALL_DIR: path.join(dataDir, 'python') };
  if (!fs.existsSync(uv)) {
    const setup = process.platform === 'win32'
      ? spawnSync('powershell', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command',
        'irm https://astral.sh/uv/install.ps1 | iex'], { stdio: 'inherit', env })
      : spawnSync('sh', ['-c', 'curl -LsSf https://astral.sh/uv/install.sh | sh'], { stdio: 'inherit', env });
    if (setup.status !== 0 || !fs.existsSync(uv)) return null;
  }
  // --no-bin: keep the private Python out of the user's PATH
  if (spawnSync(uv, ['python', 'install', '--no-bin', '3.12'], { stdio: 'inherit', env }).status !== 0 &&
      spawnSync(uv, ['python', 'install', '3.12'], { stdio: 'inherit', env }).status !== 0) return null;
  const found = spawnSync(uv, ['python', 'find', '3.12'], { encoding: 'utf8', env });
  const exe = found.stdout.trim();
  if (found.status !== 0 || !exe || !works(exe, [])) return null;
  fs.mkdirSync(path.dirname(cachedPython), { recursive: true });
  fs.writeFileSync(cachedPython, exe);
  console.error(`✓ Python pronto: ${exe}`);
  return [exe, []];
}

const python = findPython() || installPython();
if (!python) {
  console.error('Smart AI precisa do Python 3.11 ou mais novo e não conseguiu instalá-lo sozinho.');
  console.error(process.platform === 'win32'
    ? 'Instale em https://www.python.org/downloads/ (marque "Add to PATH").'
    : 'macOS: brew install python  ·  Ubuntu/Debian: sudo apt install python3');
  process.exit(1);
}

// Claude Code installed as this package's optional dependency is used when there's no global one
const env = { ...process.env, SMART_AI_INSTALLER: 'npm' };
env.PATH = [process.env.PATH, path.join(root, 'node_modules', '.bin')].join(path.delimiter);

const [cmd, pre] = python;
const child = spawn(cmd, [...pre, script, ...process.argv.slice(2)], { stdio: 'inherit', env });
// Ctrl-C reaches the child through the shared terminal; just don't die before it does
process.on('SIGINT', () => {});
process.on('SIGTERM', () => child.kill('SIGTERM'));
child.on('exit', (code, signal) => process.exit(signal ? 130 : code ?? 1));
