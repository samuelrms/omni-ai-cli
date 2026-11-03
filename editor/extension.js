// Omni AI CLI — VS Code / Cursor extension.
// Thin wrapper around the `ai` CLI: it never reimplements routing, it just drives the command
// in an integrated terminal so logins, streaming and the chat work exactly as in a real terminal.
const vscode = require('vscode');
const os = require('os');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

/** Single-quote a string for POSIX shells (the integrated terminal on macOS/Linux). */
function shq(s) {
  return "'" + String(s).replace(/'/g, "'\\''") + "'";
}

function config() {
  return vscode.workspace.getConfiguration('omni-ai');
}

/** The base command with the configured tool and mode flags. */
function baseCommand() {
  const cfg = config();
  const bin = cfg.get('binPath', 'ai');
  const tool = cfg.get('tool', 'auto');
  const mode = cfg.get('mode', 'default');
  let cmd = bin;
  if (tool && tool !== 'auto') cmd += ' --' + tool;
  if (mode && mode !== 'default') cmd += ' --mode ' + mode;
  return cmd;
}

let terminal;
function omniTerminal() {
  if (!terminal || terminal.exitStatus !== undefined) {
    terminal = vscode.window.createTerminal('Omni AI');
  }
  terminal.show();
  return terminal;
}

function run(commandLine) {
  omniTerminal().sendText(commandLine, true);
}

/** Write text to a temp file and return a shell snippet that expands to its contents. */
function fromTempFile(text) {
  const file = path.join(os.tmpdir(), `omni-ai-${Date.now()}.txt`);
  fs.writeFileSync(file, text, 'utf8');
  return `"$(cat ${shq(file)})"`;
}

async function ask() {
  const prompt = await vscode.window.showInputBox({ prompt: 'Omni AI: ask', placeHolder: 'Your question' });
  if (!prompt) return;
  run(`${baseCommand()} ${shq(prompt)}`);
}

function selectionText() {
  const editor = vscode.window.activeTextEditor;
  if (!editor) return '';
  const sel = editor.selection;
  return sel.isEmpty ? editor.document.getText() : editor.document.getText(sel);
}

function explainSelection() {
  const text = selectionText();
  if (!text.trim()) {
    vscode.window.showWarningMessage('Omni AI: nothing selected.');
    return;
  }
  run(`${baseCommand()} ${shq('Explain this code:')} ${fromTempFile(text)}`);
}

async function runOnSelection() {
  const text = selectionText();
  if (!text.trim()) {
    vscode.window.showWarningMessage('Omni AI: nothing selected.');
    return;
  }
  const prompt = await vscode.window.showInputBox({ prompt: 'Omni AI: prompt for the selection' });
  if (!prompt) return;
  run(`${baseCommand()} ${shq(prompt)} ${fromTempFile(text)}`);
}

function openChat() {
  const cfg = config();
  run(cfg.get('binPath', 'ai'));
}

function doctor() {
  run(`${config().get('binPath', 'ai')} doctor`);
}

/** Tool/mode flags as an argv array, for spawning the CLI directly (no shell). */
function baseArgs() {
  const cfg = config();
  const args = [];
  const tool = cfg.get('tool', 'auto');
  const mode = cfg.get('mode', 'default');
  if (tool && tool !== 'auto') args.push('--' + tool);
  if (mode && mode !== 'default') args.push('--mode', mode);
  return args;
}

/** Run `ai <args>` and resolve with its stdout (for --json queries). */
function runCapture(args) {
  return new Promise((resolve) => {
    const cfg = config();
    const bin = cfg.get('binPath', 'ai');
    const cwd = (vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders[0])
      ? vscode.workspace.workspaceFolders[0].uri.fsPath : os.homedir();
    let out = '';
    let child;
    try {
      child = spawn(bin, args, { cwd, env: { ...process.env, NO_COLOR: '1' } });
    } catch (e) {
      resolve(''); return;
    }
    child.stdout.on('data', (d) => { out += d.toString(); });
    child.on('error', () => resolve(''));
    child.on('close', () => resolve(out));
  });
}

/** Sidebar history: lists past conversations across tools and opens one; "Continue" seeds the chat. */
class OmniHistoryView {
  constructor(chatView) {
    this.chatView = chatView;
  }

  resolveWebviewView(view) {
    this.view = view;
    view.webview.options = { enableScripts: true };
    view.webview.html = this.html();
    view.webview.onDidReceiveMessage(async (msg) => {
      if (msg.type === 'list') {
        const raw = await runCapture(['history', '--json']);
        this.post({ type: 'items', items: safeJson(raw, []) });
      } else if (msg.type === 'open') {
        const raw = await runCapture(['history', msg.id, '--json']);
        this.post({ type: 'conversation', conv: safeJson(raw, null) });
      } else if (msg.type === 'continue') {
        const raw = await runCapture(['history', msg.id, '--json']);
        const conv = safeJson(raw, null);
        if (conv && this.chatView) this.chatView.continueFrom(conv);
      }
    });
  }

  post(message) {
    if (this.view) this.view.webview.postMessage(message);
  }

  html() {
    return `<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline';">
<style>
  body { font-family: var(--vscode-font-family); font-size: var(--vscode-font-size); color: var(--vscode-foreground); margin: 0; }
  #search { width: 100%; box-sizing: border-box; padding: 6px 8px; background: var(--vscode-input-background); color: var(--vscode-input-foreground); border: none; border-bottom: 1px solid var(--vscode-panel-border); }
  .item { padding: 8px; border-bottom: 1px solid var(--vscode-panel-border); cursor: pointer; }
  .item:hover { background: var(--vscode-list-hoverBackground); }
  .src { font-size: 10px; text-transform: uppercase; opacity: .7; margin-right: 6px; }
  .omni { color: #22d3ee; } .claude { color: #e0b341; } .codex { color: #4ade80; } .cursor { color: #a78bfa; }
  .meta { opacity: .6; font-size: 11px; }
  .title { display: block; margin-top: 2px; }
  #transcript { padding: 8px; white-space: pre-wrap; word-break: break-word; display: none; }
  #bar { padding: 8px; display: none; gap: 6px; }
  button { background: var(--vscode-button-background); color: var(--vscode-button-foreground); border: none; border-radius: 4px; padding: 6px 10px; cursor: pointer; }
  .you { color: var(--vscode-textLink-foreground); font-weight: 600; }
  .turn { margin: 8px 0; }
</style></head><body>
<input id="search" placeholder="Filter conversations…">
<div id="list"></div>
<div id="bar"><button id="back">← Back</button><button id="continue">Continue in chat</button></div>
<div id="transcript"></div>
<script>
  const vs = acquireVsCodeApi();
  const list = document.getElementById('list'), tr = document.getElementById('transcript');
  const bar = document.getElementById('bar'), search = document.getElementById('search');
  let items = [], current = null;
  function render(filter) {
    list.innerHTML = '';
    for (const it of items) {
      if (filter && !(it.title || '').toLowerCase().includes(filter)) continue;
      const d = document.createElement('div'); d.className = 'item';
      const when = new Date((it.updated || 0) * 1000).toLocaleString();
      d.innerHTML = '<span class="src ' + it.source + '">' + it.source + '</span>' +
        '<span class="meta">' + it.count + ' msg · ' + when + '</span>' +
        '<span class="title"></span>';
      d.querySelector('.title').textContent = it.title || '(no title)';
      d.onclick = () => vs.postMessage({ type: 'open', id: it.id });
      list.appendChild(d);
    }
  }
  search.addEventListener('input', () => render(search.value.trim().toLowerCase()));
  document.getElementById('back').onclick = () => { tr.style.display = 'none'; bar.style.display = 'none'; list.style.display = ''; search.style.display = ''; };
  document.getElementById('continue').onclick = () => { if (current) vs.postMessage({ type: 'continue', id: current.id }); };
  window.addEventListener('message', (e) => {
    const m = e.data;
    if (m.type === 'items') { items = m.items || []; render(''); }
    else if (m.type === 'conversation' && m.conv) {
      current = m.conv; tr.innerHTML = ''; list.style.display = 'none'; search.style.display = 'none';
      bar.style.display = 'flex'; tr.style.display = 'block';
      for (const msg of m.conv.messages) {
        const t = document.createElement('div'); t.className = 'turn';
        const who = document.createElement('span'); who.className = msg.role === 'user' ? 'you' : '';
        who.textContent = msg.role === 'user' ? 'you' : '●'; t.appendChild(who);
        t.appendChild(document.createTextNode(' ' + msg.content)); tr.appendChild(t);
      }
    }
  });
  vs.postMessage({ type: 'list' });
</script></body></html>`;
  }
}

function safeJson(text, fallback) {
  try { return JSON.parse(text); } catch (e) { return fallback; }
}

/** Sidebar chat: spawns `ai` per prompt and streams its output into a webview. */
class OmniChatView {
  constructor(context) {
    this.context = context;
    this.child = null;
  }

  resolveWebviewView(view) {
    this.view = view;
    view.webview.options = { enableScripts: true };
    view.webview.html = this.html();
    view.webview.onDidReceiveMessage((msg) => {
      if (msg.type === 'send') this.send(msg.text);
      else if (msg.type === 'cancel') this.cancel();
    });
  }

  post(message) {
    if (this.view) this.view.webview.postMessage(message);
  }

  continueFrom(conv) {
    const tail = (conv.messages || []).slice(-8)
      .map((m) => (m.role === 'user' ? 'User: ' : 'Assistant: ') + m.content).join('\n\n');
    const prompt = `Continue this conversation (from ${conv.source}). Context:\n\n${tail}\n\nContinue from here.`;
    try { if (this.view) this.view.show(true); } catch (e) { /* view not ready */ }
    this.post({ type: 'seed', text: prompt });
    this.send(prompt);
  }

  cancel() {
    if (this.child) {
      try { this.child.kill('SIGTERM'); } catch (e) { /* already gone */ }
      this.child = null;
    }
  }

  send(text) {
    if (!text || !text.trim()) return;
    if (this.child) return; // one request at a time
    const cfg = config();
    const bin = cfg.get('binPath', 'ai');
    const cwd = (vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders[0])
      ? vscode.workspace.workspaceFolders[0].uri.fsPath : os.homedir();
    this.post({ type: 'start', prompt: text });
    let child;
    try {
      child = spawn(bin, [...baseArgs(), text], { cwd, env: { ...process.env, NO_COLOR: '1' } });
    } catch (e) {
      this.post({ type: 'out', stream: 'err', text: String(e) });
      this.post({ type: 'done', code: 1 });
      return;
    }
    this.child = child;
    child.stdout.on('data', (d) => this.post({ type: 'out', stream: 'out', text: d.toString() }));
    child.stderr.on('data', (d) => this.post({ type: 'out', stream: 'err', text: d.toString() }));
    child.on('error', (e) => this.post({ type: 'out', stream: 'err', text: String(e.message || e) }));
    child.on('close', (code) => { this.child = null; this.post({ type: 'done', code }); });
  }

  html() {
    return `<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline';">
<style>
  body { font-family: var(--vscode-font-family); font-size: var(--vscode-font-size); color: var(--vscode-foreground); margin: 0; display: flex; flex-direction: column; height: 100vh; }
  #log { flex: 1; overflow-y: auto; padding: 8px; white-space: pre-wrap; word-break: break-word; }
  .msg { margin: 8px 0; }
  .you { color: var(--vscode-textLink-foreground); font-weight: 600; }
  .err { color: var(--vscode-errorForeground); opacity: .8; }
  #bar { display: flex; gap: 6px; padding: 8px; border-top: 1px solid var(--vscode-panel-border); }
  #q { flex: 1; resize: none; background: var(--vscode-input-background); color: var(--vscode-input-foreground); border: 1px solid var(--vscode-input-border); border-radius: 4px; padding: 6px; font: inherit; }
  button { background: var(--vscode-button-background); color: var(--vscode-button-foreground); border: none; border-radius: 4px; padding: 6px 10px; cursor: pointer; }
  button:disabled { opacity: .5; cursor: default; }
</style></head><body>
<div id="log"></div>
<div id="bar">
  <textarea id="q" rows="2" placeholder="Ask anything… (Enter to send)"></textarea>
  <button id="send">Send</button>
  <button id="stop" disabled>Stop</button>
</div>
<script>
  const vs = acquireVsCodeApi();
  const log = document.getElementById('log'), q = document.getElementById('q');
  const sendBtn = document.getElementById('send'), stopBtn = document.getElementById('stop');
  let answer = null;
  function el(cls, text) { const d = document.createElement('div'); d.className = 'msg ' + (cls||''); d.textContent = text||''; log.appendChild(d); log.scrollTop = log.scrollHeight; return d; }
  function send() {
    const text = q.value.trim(); if (!text) return;
    el('you', text); q.value = ''; answer = el('', '');
    sendBtn.disabled = true; stopBtn.disabled = false;
    vs.postMessage({ type: 'send', text });
  }
  sendBtn.onclick = send;
  stopBtn.onclick = () => vs.postMessage({ type: 'cancel' });
  q.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });
  window.addEventListener('message', (e) => {
    const m = e.data;
    if (m.type === 'out') {
      if (m.stream === 'err') { el('err', m.text); }
      else { if (!answer) answer = el('', ''); answer.textContent += m.text; }
      log.scrollTop = log.scrollHeight;
    } else if (m.type === 'done') {
      sendBtn.disabled = false; stopBtn.disabled = true; answer = null;
    }
  });
</script></body></html>`;
  }
}

function activate(context) {
  const chatView = new OmniChatView(context);
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider('omni-ai.chat', chatView),
    vscode.window.registerWebviewViewProvider('omni-ai.history', new OmniHistoryView(chatView))
  );

  const cmds = {
    'omni-ai.ask': ask,
    'omni-ai.explainSelection': explainSelection,
    'omni-ai.runOnSelection': runOnSelection,
    'omni-ai.openChat': openChat,
    'omni-ai.doctor': doctor,
  };
  for (const [id, fn] of Object.entries(cmds)) {
    context.subscriptions.push(vscode.commands.registerCommand(id, fn));
  }
}

function deactivate() {}

module.exports = { activate, deactivate };
