// Starts the `ai` CLI without a shell. On Windows, Node only starts .exe files that way, while npm and the
// installers make `ai.cmd`: this finds what the shim really runs, as the CLI does for the tools it starts.
'use strict';
const fs = require('fs');
const path = require('path');
const childProcess = require('child_process');

// The last line of a shim: optionally Node, then a path next to the shim, then every argument
const SHIM_LAUNCH = /(?:("%_prog%"|"%~?dp0%?\\node\.exe"|\bnode(?:\.exe)?)\s+)?"%~?dp0%?\\([^"%\r\n]+)"\s+%\*[ \t]*\r?$/gm;
// Text cmd.exe leaves alone inside double quotes: no quote, no %VAR% or !VAR!, no line break
const CMD_QUOTABLE = /^[^"%!\r\n]*$/;

function isShim(file) { return /\.(cmd|bat)$/i.test(file); }

/** An environment variable whatever its case: a copy of process.env on Windows keeps names like `Path`.
 * The exact name wins when a copy has both. */
function getEnv(env, name) {
  if (env[name] !== undefined) return env[name];
  const key = Object.keys(env).find((k) => k.toUpperCase() === name);
  return key === undefined ? undefined : env[key];
}

function isFile(file) {
  try { return fs.statSync(file).isFile(); } catch (_) { return false; }
}

/** Full path of a program on PATH. Only absolute folders count, so a workspace cannot plant one. */
function which(name, env = process.env, platform = process.platform) {
  if (name.includes('/') || name.includes('\\')) return isFile(name) ? name : null;
  const extensions = path.extname(name) ? [''] : (getEnv(env, 'PATHEXT') || '.COM;.EXE;.BAT;.CMD').split(';').filter(Boolean);
  for (const folder of (getEnv(env, 'PATH') || '').split(platform === 'win32' ? ';' : ':')) {
    const clean = folder.replace(/"/g, '');
    if (!path.isAbsolute(clean)) continue;
    for (const extension of extensions) {
      const found = path.join(clean, name + extension);
      if (isFile(found)) return found;
    }
  }
  return null;
}

/** What an npm, pnpm or yarn shim runs, or null when it is not one we recognize. */
function shimCommand(shim, env = process.env) {
  let text;
  try { text = fs.readFileSync(shim, 'utf8'); } catch (_) { return null; }
  const launches = new Set();
  for (const found of text.matchAll(SHIM_LAUNCH)) launches.add(`${found[1] ? 1 : 0}|${found[2]}`);
  if (launches.size !== 1) return null;
  const [needsNode, relative] = [...launches][0].split(/\|(.*)/s);
  const folder = path.dirname(shim);
  const target = path.normalize(path.join(folder, relative.replace(/\\/g, path.sep)));
  if (!isFile(target)) return null;
  if (needsNode === '0') return /\.exe$/i.test(target) ? [target] : null;
  const beside = path.join(folder, 'node.exe');
  const node = isFile(beside) ? beside : which('node', env, 'win32');
  return node && !isShim(node) ? [node, target] : null;
}

/** How to start `bin` with `args`: { file, args, verbatim }, or null when it cannot be done safely. */
function plan(bin, args, env = process.env, platform = process.platform) {
  if (platform !== 'win32') return { file: bin, args };
  const found = which(bin, env, platform);
  if (!found) return { file: bin, args }; // the usual "not found" error, which offers to install the CLI
  if (!isShim(found)) return { file: found, args };
  const direct = shimCommand(found, env);
  if (direct) return { file: direct[0], args: [...direct.slice(1), ...args] };
  // A launcher we don't know, like the installer's ai.cmd: cmd.exe, with every part quoted. The prompt
  // never goes here (it travels in a file), so a part cmd.exe would read as a command is simply refused.
  const parts = [found, ...args];
  if (!parts.every((part) => CMD_QUOTABLE.test(part) && !part.endsWith('\\'))) return null;
  const shell = getEnv(env, 'COMSPEC') || path.join(getEnv(env, 'SYSTEMROOT') || 'C:\\Windows', 'System32', 'cmd.exe');
  return { file: shell, args: ['/d', '/s', '/c', `"${parts.map((part) => `"${part}"`).join(' ')}"`], verbatim: true };
}

/** child_process.spawn for the `ai` CLI. Throws, like spawn, when it cannot start it. */
function launch(bin, args, options = {}) {
  const how = plan(bin, args, options.env || process.env);
  if (!how) throw new Error(`The ${bin} launcher on PATH cannot take this request safely. Install the CLI with npm.`);
  return childProcess.spawn(how.file, how.args, how.verbatim ? { ...options, windowsVerbatimArguments: true } : options);
}

/** The prompt in a file only this user can read, for `ai --prompt-file`. No command line limits its size. */
function promptFile(text) {
  const folder = fs.mkdtempSync(path.join(require('os').tmpdir(), 'omni-ai-prompt-'));
  const file = path.join(folder, 'prompt.txt');
  fs.writeFileSync(file, text, { encoding: 'utf8', mode: 0o600 });
  return { file, remove: () => { try { fs.rmSync(folder, { recursive: true, force: true }); } catch (_) { /* gone */ } } };
}

module.exports = { launch, plan, promptFile, shimCommand, which };
