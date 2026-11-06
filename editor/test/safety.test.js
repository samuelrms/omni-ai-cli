// Runs extension.js against a stub of the vscode API. Webview content is treated as hostile.
const test = require('node:test');
const assert = require('node:assert');
const Module = require('module');
const vm = require('vm');

const state = { user: {}, workspace: {}, sent: [], executed: [], updates: [], providers: {}, commands: {} };
const vscode = {
  workspace: {
    workspaceFolders: [],
    getConfiguration: () => ({
      get: (key, fallback) => state.workspace[key] ?? state.user[key] ?? fallback,
      inspect: (key) => ({ globalValue: state.user[key], workspaceValue: state.workspace[key] }),
      update: async (key, value, global) => { state.updates.push([key, value, global]); },
    }),
  },
  window: {
    registerWebviewViewProvider: (id, provider) => { state.providers[id] = provider; return { dispose() {} }; },
    createTerminal: () => ({ show() {}, sendText: (text) => state.sent.push(text), exitStatus: undefined }),
    showWarningMessage: async () => undefined,
    showInformationMessage: async () => undefined,
    showInputBox: async () => 'hello',
    activeTextEditor: null,
  },
  commands: {
    registerCommand: (id, fn) => { state.commands[id] = fn; return { dispose() {} }; },
    executeCommand: async (id) => { state.executed.push(id); },
  },
  Uri: { file: (p) => ({ fsPath: p }), joinPath: () => ({}) },
  ViewColumn: { Active: 1 },
  env: {},
};
const load = Module._load;
Module._load = function (request, ...rest) { return request === 'vscode' ? vscode : load.call(this, request, ...rest); };

const childProcess = require('child_process');
const spawned = [];
const realSpawn = childProcess.spawn;
childProcess.spawn = (bin, args, options) => {
  spawned.push([bin, ...args]);
  return realSpawn(process.execPath, ['-e', ''], options);
};

const extension = require('../extension.js');
extension.activate({ subscriptions: [], extensionUri: {}, workspaceState: { get: (k, d) => d, update() {} }, globalState: { get: () => true, update() {} } });

function chatView() {
  let handler;
  const webview = { options: {}, html: '', onDidReceiveMessage: (fn) => { handler = fn; }, postMessage() {} };
  state.providers['omni-ai.chat'].resolveWebviewView({ webview, onDidDispose() {}, onDidChangeVisibility() {} });
  return { webview, handler };
}
function renderer() {
  const { html } = chatView().webview;
  const start = html.indexOf('var F = String.fromCharCode(96)');
  const end = html.indexOf('var hhmm');
  const context = {};
  vm.runInNewContext(html.slice(start, end), context);
  return context;
}
const reset = () => { Object.assign(state, { user: {}, workspace: {}, sent: [], executed: [], updates: [] }); spawned.length = 0; };

test('every inline script needs the nonce, and inline handlers are refused', () => {
  for (const html of [chatView().webview.html]) {
    const csp = html.match(/script-src ([^;]+);/)[1];
    assert.ok(!csp.includes('unsafe-inline'));
    const nonce = csp.match(/'nonce-([^']+)'/)[1];
    for (const tag of html.match(/<script(?![^>]*\bsrc=)[^>]*>/g)) assert.ok(tag.includes(`nonce="${nonce}"`), tag);
  }
  assert.notEqual(chatView().webview.html.match(/'nonce-([^']+)'/)[1], chatView().webview.html.match(/'nonce-([^']+)'/)[1]);
});

test('model text cannot leave an attribute', () => {
  const { md } = renderer();
  const fence = '`'.repeat(3);
  const payloads = [
    '`x' + fence + '" style="animation:blink 1s" onanimationstart="alert(1)" x="' + fence + '.js`',
    '`a" onclick="alert(1)" x=".js`',
    "`a' onclick='alert(1)' x='.js`",
    '`' + fence + 'mermaid\n" onload="alert(1)\n' + fence + '.js`',
    '<img src=x onerror=alert(1)>',
  ];
  for (const text of payloads) {
    const out = md(text);
    assert.doesNotMatch(out, /\son[a-z]+\s*=\s*["']/, out);
    assert.doesNotMatch(out, /<img/, out);
    assert.doesNotMatch(out, /style="animation/, out);
  }
});

test('markdown keeps working', () => {
  const { md } = renderer();
  assert.match(md('**bold** and `src/a.js`'), /<b>bold<\/b> and <code class="file" title="Open file" data-p="src\/a.js">src\/a.js<\/code>/);
  assert.match(md('# T'), /<h1>T<\/h1>/);
  assert.match(md('```js\nif (a < 1) "x"\n```'), /<pre><code>if \(a &lt; 1\) &quot;x&quot;\n<\/code><\/pre>/);
  assert.match(md('stray \u00000\u0000 marker'), /stray 0 marker/);
  assert.match(md("it's"), /it&#39;s/);
});

test('the slash menu does not build rows from names as HTML', () => {
  const { html } = chatView().webview;
  assert.ok(!html.includes("el.innerHTML = '<b>' + c.cmd"));
  assert.ok(html.includes('name.textContent = c.cmd'));
});

test('the webview cannot change the executable or run other programs', () => {
  reset();
  const { handler } = chatView();
  handler({ type: 'setDefault', key: 'binPath', value: '/bin/sh' });
  handler({ type: 'setDefault', key: 'claudeBin', value: '/bin/sh' });
  handler({ type: 'setDefault', key: 'tool', value: 'x; touch y' });
  handler({ type: 'setDefault', key: 'mode', value: 'other' });
  assert.deepEqual(state.updates, []);
  handler({ type: 'setDefault', key: 'mode', value: 'plan' });
  handler({ type: 'setDefault', key: 'tool', value: 'codex' });
  assert.deepEqual(state.updates, [['mode', 'plan', true], ['tool', 'codex', true]]);
  handler({ type: 'cli', args: ['-c', 'touch y'] });
  handler({ type: 'cli', args: 'nope' });
  handler({ type: 'cli', args: ['--mode', 'yolo', 'do it'] });
  assert.deepEqual(spawned.filter((call) => call.includes('-c') || call.includes('--mode')), []);
  handler({ type: 'command', id: 'workbench.action.terminal.sendSequence' });
  handler({ type: 'command', id: 'omni-ai.history.focus' });
  assert.deepEqual(state.executed, ['omni-ai.history.focus']);
});

test('allowed CLI commands still run', () => {
  reset();
  const { handler } = chatView();
  handler({ type: 'cli', args: ['--usage'] });
  assert.ok(spawned.some((call) => call[0] === 'ai' && call[1] === '--usage'));
});

test('repository settings do not reach the terminal or the executable', async () => {
  reset();
  state.workspace = { binPath: '/evil/x', tool: 'x; touch y #', mode: 'yolo', claudeBin: '/evil/c' };
  state.commands['omni-ai.doctor']();
  await state.commands['omni-ai.ask']();
  assert.deepEqual(state.sent, ['ai doctor', "ai 'hello'"]);
});

test('user settings still apply, and odd paths are quoted', async () => {
  reset();
  state.user = { binPath: '/opt/my tools/ai;x', tool: 'codex', mode: 'plan' };
  await state.commands['omni-ai.ask']();
  assert.deepEqual(state.sent, ["'/opt/my tools/ai;x' --codex --mode plan 'hello'"]);
  reset();
  state.user = { binPath: 'C:\\tools\\ai.cmd' };
  state.commands['omni-ai.doctor']();
  assert.deepEqual(state.sent, ['C:\\tools\\ai.cmd doctor']);
});

test('the manifest keeps risky settings out of workspaces', () => {
  const manifest = require('../package.json');
  const props = manifest.contributes.configuration.properties;
  assert.equal(props['omni-ai.binPath'].scope, 'machine');
  assert.equal(props['omni-ai.mode'].scope, 'machine');
  assert.equal(manifest.capabilities.untrustedWorkspaces.supported, false);
});

test.after(() => setImmediate(() => process.exit(process.exitCode || 0)));
