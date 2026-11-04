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

function firstLine(s) {
  const line = String(s).split('\n')[0].trim();
  return line.length > 80 ? line.slice(0, 79) + '…' : line;
}

/** Sidebar chat: spawns `ai` per prompt and streams its output into a webview. */
function workspaceCwd() {
  return (vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders[0])
    ? vscode.workspace.workspaceFolders[0].uri.fsPath : os.homedir();
}

class OmniChatView {
  constructor(context) {
    this.context = context;
    this.child = null;
    this.webview = null;
    this.attachments = [];
  }

  async attachFiles() {
    const picks = await vscode.window.showOpenDialog({ canSelectMany: true, openLabel: 'Attach as context' });
    if (!picks) return;
    for (const uri of picks) {
      try {
        const bytes = await vscode.workspace.fs.readFile(uri);
        let text = Buffer.from(bytes).toString('utf8');
        if (text.length > 100000) text = text.slice(0, 100000) + '\n…(truncated)';
        this.attachments.push({ name: uri.fsPath.split('/').pop(), path: uri.fsPath, content: text });
      } catch (e) { /* skip unreadable */ }
    }
    this.post({ type: 'attached', names: this.attachments.map((a) => a.name) });
  }

  // Bind to a webview from either the sidebar view or an editor panel.
  attach(webview) {
    this.webview = webview;
    webview.options = { enableScripts: true };
    webview.html = this.html();
    webview.onDidReceiveMessage((msg) => {
      if (msg.type === 'send') this.send(msg.text, { tool: msg.tool, mode: msg.mode });
      else if (msg.type === 'cancel') this.cancel();
      else if (msg.type === 'attach') this.attachFiles();
      else if (msg.type === 'doctor') this.runDoctor();
      else if (msg.type === 'openFile') this.openFile(msg.path);
      else if (msg.type === 'command') vscode.commands.executeCommand(msg.id);
      else if (msg.type === 'ready') { this.sendDefaults(); this.loadLast(); }
    });
  }

  // Open a file referenced in the chat (cmd/ctrl+click). Resolves relative to the workspace.
  async openFile(p) {
    if (!p) return;
    const path = require('path');
    const abs = path.isAbsolute(p) ? p : path.join(workspaceCwd(), p);
    try {
      const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(abs));
      await vscode.window.showTextDocument(doc, { preview: true });
    } catch (e) {
      vscode.window.showWarningMessage('Omni AI: could not open ' + p);
    }
  }

  // Run the health check and stream it into the chat (not a terminal).
  runDoctor() {
    if (this.child) return;
    const bin = config().get('binPath', 'ai');
    this.post({ type: 'start', prompt: 'doctor' });
    let child;
    try { child = spawn(bin, ['--doctor'], { cwd: workspaceCwd(), env: { ...process.env, NO_COLOR: '1' } }); }
    catch (e) { this.post({ type: 'out', stream: 'err', text: String(e) }); this.post({ type: 'done', code: 1 }); return; }
    this.child = child;
    child.stdout.on('data', (d) => this.post({ type: 'out', stream: 'out', text: d.toString() }));
    child.stderr.on('data', (d) => this.post({ type: 'out', stream: 'err', text: d.toString() }));
    child.on('error', (e) => this.post({ type: 'out', stream: 'err', text: String(e.message || e) }));
    child.on('close', (code) => { this.child = null; this.post({ type: 'done', code }); });
  }

  resolveWebviewView(view) {
    this.view = view;
    this.attach(view.webview);
  }

  post(message) {
    if (this.webview) this.webview.postMessage(message);
  }

  // On open, show the last Omni conversation for this project, so you pick up where you left off.
  async loadLast() {
    const raw = await runCapture(['history', '--json', '--source', 'omni']);
    const items = safeJson(raw, []);
    const cwd = workspaceCwd();
    const last = items.find((it) => it.cwd === cwd) || items[0];
    if (!last) return;
    const conv = safeJson(await runCapture(['history', last.id, '--json']), null);
    if (conv && conv.messages) this.post({ type: 'history', messages: conv.messages, title: conv.title });
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

  // Tell the webview the configured defaults so the mode/tool pickers start in sync.
  sendDefaults() {
    const cfg = config();
    this.post({ type: 'defaults', tool: cfg.get('tool', 'auto'), mode: cfg.get('mode', 'default') });
  }

  send(text, opts) {
    if (!text || !text.trim()) return;
    if (this.child) return; // one request at a time
    opts = opts || {};
    const cfg = config();
    const bin = cfg.get('binPath', 'ai');
    const tool = opts.tool || cfg.get('tool', 'auto');
    const mode = opts.mode || cfg.get('mode', 'default');
    const args = [];
    if (tool && tool !== 'auto') args.push('--' + tool);
    if (mode && mode !== 'default') args.push('--mode', mode);
    let prompt = text;
    if (this.attachments.length) {
      const ctx = this.attachments.map((a) => `--- ${a.path} ---\n${a.content}`).join('\n\n');
      prompt = `Context files:\n\n${ctx}\n\n---\n\n${text}`;
      this.attachments = [];
      this.post({ type: 'attached', names: [] });
    }
    args.push('--', prompt);
    this.post({ type: 'start', prompt: text });
    let child;
    try {
      // SMART_AI_EVENTS makes the router emit JSON events (route, provider, actions, answer),
      // so the panel can show the thinking/activity like the other tools.
      child = spawn(bin, args, { cwd: workspaceCwd(), env: { ...process.env, NO_COLOR: '1', SMART_AI_EVENTS: '1' } });
    } catch (e) {
      this.post({ type: 'out', stream: 'err', text: String(e) });
      this.post({ type: 'done', code: 1 });
      return;
    }
    this.child = child;
    let buf = '';
    const feed = (chunk) => {
      buf += chunk;
      let nl;
      while ((nl = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, nl); buf = buf.slice(nl + 1);
        this._event(line);
      }
    };
    child.stdout.on('data', (d) => feed(d.toString()));
    child.stderr.on('data', (d) => this.post({ type: 'out', stream: 'err', text: d.toString() }));
    child.on('error', (e) => this.post({ type: 'out', stream: 'err', text: String(e.message || e) }));
    child.on('close', (code) => { if (buf.trim()) this._event(buf); this.child = null; this.post({ type: 'done', code }); });
  }

  // Map one router event line to a webview update: activity ('act') or answer text ('out').
  _event(line) {
    line = line.trim();
    if (!line) return;
    let ev;
    try { ev = JSON.parse(line); } catch (e) {
      // non-JSON line = plain answer text (e.g. a tool that does not emit events)
      this.post({ type: 'out', stream: 'out', text: line + '\n' });
      return;
    }
    const kind = ev.type;
    const act = (text) => this.post({ type: 'act', text });
    const answer = (text) => { if (text) this.post({ type: 'out', stream: 'out', text }); };
    if (kind === 'smart.route') {
      act((ev.label === 'complex' ? '⤷ complex task → Claude' : '⤷ simple → alternative') + (ev.by ? ' · ' + ev.by : ''));
    } else if (kind === 'smart.provider') {
      act('● ' + ev.provider + (ev.profile ? ' · ' + ev.profile : ''));
    } else if (kind === 'smart.delta') {
      answer(ev.text || '');
    } else if (kind === 'smart.answer') {
      answer(ev.content || '');
    } else if (kind === 'item.started' || kind === 'item.completed') {
      const it = ev.item || {}; const done = kind === 'item.completed';
      if (it.type === 'agent_message' && done) answer(it.text || '');
      else if (it.type === 'command_execution') act((done ? '✓ ran' : '● running') + ' ' + firstLine(it.command || ''));
      else if (it.type === 'file_change' && done) act('✎ edited ' + (it.changes || []).map((c) => c.path).join(', '));
      else if (it.type === 'mcp_tool_call' && !done) act('⚙ ' + (it.server || '') + ' · ' + (it.tool || ''));
      else if (it.type === 'web_search' && !done) act('🔎 ' + firstLine(it.query || ''));
    } else if (kind === 'assistant') {
      for (const b of ((ev.message || {}).content || [])) {
        if (b.type === 'text') answer(b.text || '');
        else if (b.type === 'thinking') act('… ' + firstLine(b.thinking || b.text || 'thinking'));
        else if (b.type === 'tool_use') act('⚙ ' + (b.name || 'tool'));
      }
    } else if (kind === 'result') {
      if (ev.is_error) this.post({ type: 'out', stream: 'err', text: String(ev.result || 'error') });
      else answer(ev.result || '');
    }
  }

  html() {
    const LOGO = `<svg class="mark" viewBox="0 0 48 48" width="56" height="56" aria-hidden="true">
      <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" stop-color="#f7a63b"/><stop offset=".5" stop-color="#ff5c5c"/><stop offset="1" stop-color="#7c5cff"/>
      </linearGradient></defs>
      <path fill="none" stroke="url(#g)" stroke-width="5" stroke-linecap="round"
        d="M16 10a18 12 -20 1 0 16 28 18 12 -20 1 0 -16 -28"/>
      <path fill="#ff5c5c" d="M24 17l2.3 4.7L31 24l-4.7 2.3L24 31l-2.3-4.7L17 24l4.7-2.3z"/>
    </svg>`;
    return `<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline';">
<style>
  :root { --brand1:#f7a63b; --brand2:#7c5cff; --col: 820px; }
  * { box-sizing: border-box; }
  body { font-family: var(--vscode-font-family); font-size: var(--vscode-font-size); color: var(--vscode-foreground); margin: 0; display: flex; flex-direction: column; height: 100vh; }
  #log { flex: 1; overflow-y: auto; padding: 16px 16px 4px; }
  .col { max-width: var(--col); margin: 0 auto; }
  #welcome { height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; gap: 8px; opacity: .92; }
  #welcome h1 { margin: 6px 0 0; font-size: 20px; font-weight: 600; }
  #welcome .tag { font-size: 12px; opacity: .6; max-width: 340px; }
  #welcome .hint { font-size: 12px; opacity: .5; margin-top: 10px; }
  .mark { filter: drop-shadow(0 2px 10px rgba(124,92,255,.35)); }
  .turn { margin: 16px 0; line-height: 1.55; }
  .turn .who { display: inline-flex; align-items: center; gap: 6px; font-weight: 600; font-size: 12px; opacity: .85; margin-bottom: 4px; }
  .turn.user .who { color: var(--vscode-textLink-foreground); }
  .turn .body { word-break: break-word; }
  .turn .body p { margin: 6px 0; }
  .turn .body h1,.turn .body h2,.turn .body h3 { margin: 10px 0 4px; }
  .turn .body ul { margin: 4px 0; padding-left: 20px; }
  .turn .body code { background: var(--vscode-textCodeBlock-background, rgba(127,127,127,.18)); padding: 1px 4px; border-radius: 4px; font-family: var(--vscode-editor-font-family, monospace); font-size: .92em; }
  .turn .body pre { background: var(--vscode-textCodeBlock-background, rgba(127,127,127,.14)); padding: 10px 12px; border-radius: 8px; overflow-x: auto; }
  .turn .body pre code { background: none; padding: 0; }
  .turn .body code.file { cursor: pointer; text-decoration: underline dotted; text-underline-offset: 2px; }
  .turn .body code.file:hover { color: var(--brand2); }
  .dot { width: 14px; height: 14px; border-radius: 50%; background: linear-gradient(135deg, var(--brand1), var(--brand2)); display: inline-block; }
  .err { color: var(--vscode-errorForeground); opacity: .85; }
  #barwrap { padding: 8px 12px 10px; border-top: 1px solid var(--vscode-panel-border); position: relative; }
  #chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 6px; }
  .chip { font-size: 11px; background: rgba(124,92,255,.18); border: 1px solid var(--brand2); border-radius: 10px; padding: 1px 8px; }
  #bar { display: flex; align-items: center; gap: 6px; border: 1px solid var(--vscode-input-border, #3a3a3a); border-radius: 12px; padding: 4px 6px 4px 10px; background: var(--vscode-input-background); }
  #bar:focus-within { border-color: var(--brand2); }
  #q { flex: 1; resize: none; background: transparent; color: var(--vscode-input-foreground); border: none; outline: none; font: inherit; line-height: 1.4; min-height: 22px; max-height: 160px; padding: 2px 0; }
  .icon-btn { border: none; border-radius: 8px; width: 28px; height: 28px; cursor: pointer; color: #fff; display: grid; place-items: center; font-size: 15px; flex: none; }
  #attach { background: transparent; color: var(--vscode-foreground); opacity: .7; }
  #send { background: linear-gradient(135deg, var(--brand1), var(--brand2)); }
  #stop { background: var(--vscode-button-secondaryBackground, #444); }
  .icon-btn:disabled { opacity: .35; cursor: default; }
  .act { font-size: 12px; opacity: .6; margin: 4px 0; font-style: italic; }
  .controls { display: flex; gap: 8px; align-items: center; margin-top: 6px; }
  .controls label { font-size: 11px; opacity: .6; }
  .controls select { background: var(--vscode-input-background); color: var(--vscode-input-foreground); border: 1px solid var(--vscode-input-border, #3a3a3a); border-radius: 6px; padding: 2px 6px; font: inherit; font-size: 11px; }
  #menu { position: absolute; left: 12px; right: 12px; bottom: 100%; background: var(--vscode-editorWidget-background, #252526); border: 1px solid var(--vscode-panel-border); border-radius: 8px; overflow: hidden; display: none; max-width: var(--col); margin: 0 auto; }
  #menu .mi { padding: 6px 10px; cursor: pointer; display: flex; gap: 8px; font-size: 12px; }
  #menu .mi:hover, #menu .mi.sel { background: var(--vscode-list-hoverBackground); }
  #menu .mi b { color: var(--brand2); }
  #plusmenu { position: absolute; left: 12px; bottom: 100%; margin-bottom: 4px; background: var(--vscode-editorWidget-background, #252526); border: 1px solid var(--vscode-panel-border); border-radius: 8px; overflow: hidden; min-width: 240px; }
  #plusmenu .mi { padding: 8px 12px; cursor: pointer; font-size: 12px; }
  #plusmenu .mi:hover { background: var(--vscode-list-hoverBackground); }
</style></head><body>
<div id="log">
  <div id="thread" class="col">
    <div id="welcome">
      ${LOGO}
      <h1>Omni AI</h1>
      <div class="tag">Claude · Codex · Cursor · Antigravity · Grok · DeepSeek · OpenRouter — one chat, automatic routing.</div>
      <div class="hint">Ask anything. Enter sends · Shift+Enter new line · / for commands.</div>
    </div>
  </div>
</div>
<div id="barwrap">
  <div class="col">
    <div id="menu"></div>
    <div id="plusmenu" style="display:none">
      <div class="mi" id="pm-attach">📎 Attach files as context</div>
      <div class="mi" id="pm-voice">🎙 Voice input (dictate)</div>
    </div>
    <div id="chips"></div>
    <div id="bar">
      <button id="attach" class="icon-btn" title="Attach files as context">+</button>
      <textarea id="q" rows="1" placeholder="Ask anything…  (/ for commands)"></textarea>
      <button id="stop" class="icon-btn" title="Stop" disabled>◼</button>
      <button id="send" class="icon-btn" title="Send">↑</button>
    </div>
    <div class="controls">
      <label>tool</label>
      <select id="tool">
        <option value="auto">auto</option><option>claude</option><option>codex</option><option>cursor</option>
        <option value="agy">antigravity</option><option>openrouter</option><option>grok</option><option>deepseek</option>
      </select>
      <label>mode</label>
      <select id="mode">
        <option value="default">default</option><option>plan</option><option>edits</option><option>auto</option><option>yolo</option>
      </select>
    </div>
  </div>
</div>
<script>
  var vs = acquireVsCodeApi();
  var log = document.getElementById('log'), q = document.getElementById('q');
  var sendBtn = document.getElementById('send'), stopBtn = document.getElementById('stop'), attachBtn = document.getElementById('attach');
  var toolSel = document.getElementById('tool'), modeSel = document.getElementById('mode');
  var chips = document.getElementById('chips'), menu = document.getElementById('menu');
  var welcome = document.getElementById('welcome');
  var answer = null, header = null, sendTime = 0, firstOut = 0, menuIdx = 0;
  var COMMANDS = [
    { cmd: '/clear', desc: 'Clear this chat' },
    { cmd: '/doctor', desc: 'Health check (terminal)' },
    { cmd: '/history', desc: 'Open history' },
    { cmd: '/plan', desc: 'Set mode: plan' },
    { cmd: '/edits', desc: 'Set mode: edits' },
    { cmd: '/default', desc: 'Set mode: default' }
  ];
  var F = String.fromCharCode(96);
  function isPath(t){ return /\\.(ts|tsx|js|jsx|py|go|rs|java|rb|json|md|ya?ml|sh|css|scss|html|sql|toml|txt)$/i.test(t) || (/\\//.test(t) && /\\.[a-z0-9]+$/i.test(t)); }
  function esc(t){ return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
  function md(s){
    var codes = [];
    s = s.replace(new RegExp(F+F+F+'([\\\\s\\\\S]*?)'+F+F+F,'g'), function(_,c){ codes.push(c); return '\\u0000'+(codes.length-1)+'\\u0000'; });
    s = esc(s);
    s = s.replace(/^### (.*)$/gm,'<h3>$1</h3>').replace(/^## (.*)$/gm,'<h2>$1</h2>').replace(/^# (.*)$/gm,'<h1>$1</h1>');
    s = s.replace(/^\\s*[-*] (.*)$/gm,'<li>$1</li>');
    s = s.replace(/(<li>[\\s\\S]*?<\\/li>)/g, function(m){ return '<ul>'+m+'</ul>'; });
    s = s.replace(new RegExp(F+'([^'+F+']+)'+F,'g'), function(_, c){
      return isPath(c) ? '<code class="file" title="Open file" data-p="' + c.replace(/"/g,'&quot;') + '">' + c + '</code>' : '<code>' + c + '</code>';
    });
    s = s.replace(/\\*\\*([^*]+)\\*\\*/g,'<b>$1</b>').replace(/\\*([^*]+)\\*/g,'<i>$1</i>');
    s = s.replace(/\\[([^\\]]+)\\]\\((https?:[^)]+)\\)/g,'<span class="link">$1</span>');
    s = s.replace(/\\n/g,'<br>');
    s = s.replace(/\\u0000(\\d+)\\u0000/g, function(_,i){ return '<pre><code>'+esc(codes[i])+'</code></pre>'; });
    return s;
  }
  var hhmm = function(d){ return new Date(d).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); };
  var dur = function(ms){ return ms >= 60000 ? Math.floor(ms/60000)+'m'+Math.round(ms%60000/1000)+'s' : (ms/1000).toFixed(1)+'s'; };
  function clearWelcome(){ if (welcome && welcome.parentNode) welcome.remove(); }
  var thread = document.getElementById('thread');
  thread.addEventListener('click', function(e){
    var el = e.target;
    if (el && el.classList && el.classList.contains('file')) { vs.postMessage({ type: 'openFile', path: el.getAttribute('data-p') }); }
  });
  function turn(role, text, time){
    clearWelcome();
    var d = document.createElement('div'); d.className = 'turn ' + role;
    var who = document.createElement('div'); who.className = 'who';
    if (role === 'assistant'){ var dot = document.createElement('span'); dot.className = 'dot'; who.appendChild(dot); who.appendChild(document.createTextNode('Omni')); }
    else { who.textContent = 'You'; }
    var meta = document.createElement('span'); meta.className = 'act'; meta.style.marginLeft = '8px'; meta.style.fontStyle = 'normal';
    if (time) meta.textContent = hhmm(time);
    who.appendChild(meta);
    var body = document.createElement('div'); body.className = 'body'; body._raw = text || '';
    body.innerHTML = md(body._raw);
    d.appendChild(who); d.appendChild(body); thread.appendChild(d); log.scrollTop = log.scrollHeight;
    return { body: body, meta: meta };
  }
  function act(text){ clearWelcome(); var d = document.createElement('div'); d.className = 'act'; d.textContent = text; thread.appendChild(d); log.scrollTop = log.scrollHeight; }
  function autosize(){ q.style.height = 'auto'; q.style.height = Math.min(q.scrollHeight, 160) + 'px'; }
  function send(){
    var text = q.value.trim(); if (!text) return;
    turn('user', text, Date.now()); q.value = ''; autosize(); hideMenu();
    answer = null; header = null; sendTime = Date.now(); firstOut = 0;
    sendBtn.disabled = true; stopBtn.disabled = false;
    vs.postMessage({ type: 'send', text: text, tool: toolSel.value, mode: modeSel.value });
  }
  function runCommand(cmd){
    q.value = ''; hideMenu(); autosize();
    if (cmd === '/clear'){ thread.innerHTML = ''; }
    else if (cmd === '/doctor'){
      turn('user', '/doctor', Date.now());
      answer = null; header = null; sendTime = Date.now(); firstOut = 0;
      sendBtn.disabled = true; stopBtn.disabled = false;
      vs.postMessage({ type: 'doctor' });
    }
    else if (cmd === '/history'){ vs.postMessage({ type: 'command', id: 'omni-ai.history.focus' }); }
    else if (cmd === '/plan'){ modeSel.value = 'plan'; }
    else if (cmd === '/edits'){ modeSel.value = 'edits'; }
    else if (cmd === '/default'){ modeSel.value = 'default'; }
  }
  function startVoice(){
    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR){ act('Voice input is not available in this webview.'); return; }
    var r = new SR(); r.lang = navigator.language || 'en-US'; r.interimResults = true; r.continuous = false;
    var base = q.value;
    r.onresult = function(ev){ var t = ''; for (var i = 0; i < ev.results.length; i++) t += ev.results[i][0].transcript; q.value = (base ? base + ' ' : '') + t; autosize(); };
    r.onerror = function(){ act('Voice input error (check mic permission).'); };
    try { r.start(); act('🎙 listening…'); } catch (e) { act('Voice input could not start.'); }
  }
  function matches(){ var v = q.value.trim().toLowerCase(); return COMMANDS.filter(function(c){ return c.cmd.indexOf(v) === 0; }); }
  function showMenu(){
    var list = matches(); if (!q.value.startsWith('/') || !list.length){ hideMenu(); return; }
    menuIdx = Math.min(menuIdx, list.length - 1);
    menu.innerHTML = '';
    list.forEach(function(c, i){
      var el = document.createElement('div'); el.className = 'mi' + (i === menuIdx ? ' sel' : '');
      el.innerHTML = '<b>' + c.cmd + '</b><span style="opacity:.6">' + c.desc + '</span>';
      el.onclick = function(){ runCommand(c.cmd); };
      menu.appendChild(el);
    });
    menu.style.display = 'block';
  }
  function hideMenu(){ menu.style.display = 'none'; menuIdx = 0; }
  var plusmenu = document.getElementById('plusmenu');
  attachBtn.onclick = function(){ plusmenu.style.display = plusmenu.style.display === 'none' ? 'block' : 'none'; };
  document.getElementById('pm-attach').onclick = function(){ plusmenu.style.display = 'none'; vs.postMessage({ type: 'attach' }); };
  document.getElementById('pm-voice').onclick = function(){ plusmenu.style.display = 'none'; startVoice(); };
  document.addEventListener('click', function(e){ if (!plusmenu.contains(e.target) && e.target !== attachBtn) plusmenu.style.display = 'none'; });
  sendBtn.onclick = send;
  stopBtn.onclick = function(){ vs.postMessage({ type: 'cancel' }); };
  q.addEventListener('input', function(){ autosize(); showMenu(); });
  q.addEventListener('keydown', function(e){
    if (menu.style.display === 'block'){
      var list = matches();
      if (e.key === 'ArrowDown'){ e.preventDefault(); menuIdx = (menuIdx + 1) % list.length; showMenu(); return; }
      if (e.key === 'ArrowUp'){ e.preventDefault(); menuIdx = (menuIdx - 1 + list.length) % list.length; showMenu(); return; }
      if (e.key === 'Enter'){ e.preventDefault(); if (list[menuIdx]) runCommand(list[menuIdx].cmd); return; }
      if (e.key === 'Escape'){ hideMenu(); return; }
    }
    if (e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); send(); }
  });
  window.addEventListener('message', function(e){
    var m = e.data;
    if (m.type === 'history'){
      (m.messages || []).forEach(function(msg){ turn(msg.role === 'assistant' ? 'assistant' : 'user', msg.content); });
      answer = null;
    } else if (m.type === 'defaults'){
      if (m.tool) toolSel.value = m.tool; if (m.mode) modeSel.value = m.mode;
    } else if (m.type === 'attached'){
      chips.innerHTML = ''; (m.names || []).forEach(function(n){ var c = document.createElement('span'); c.className = 'chip'; c.textContent = '📎 ' + n; chips.appendChild(c); });
    } else if (m.type === 'seed'){
      q.value = m.text; autosize(); q.focus();
    } else if (m.type === 'act'){
      act(m.text);
    } else if (m.type === 'out'){
      if (m.stream === 'err'){ act('✗ ' + m.text); }
      else {
        if (!answer){ var t = turn('assistant', ''); answer = t.body; header = t.meta; }
        if (!firstOut){ firstOut = Date.now(); if (header) header.textContent = 'thought ' + dur(firstOut - sendTime); }
        answer._raw += m.text; answer.innerHTML = md(answer._raw);
      }
      log.scrollTop = log.scrollHeight;
    } else if (m.type === 'done'){
      sendBtn.disabled = false; stopBtn.disabled = true;
      if (header && sendTime) header.textContent = (firstOut ? 'thought ' + dur(firstOut - sendTime) + ' · ' : '') + 'done ' + hhmm(Date.now()) + ' · ' + dur(Date.now() - sendTime);
      answer = null; header = null;
    }
  });
  vs.postMessage({ type: 'ready' });
</script></body></html>`;
  }
}

function activate(context) {
  const chatView = new OmniChatView(context);
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider('omni-ai.chat', chatView),
    vscode.window.registerWebviewViewProvider('omni-ai.history', new OmniHistoryView(chatView))
  );

  const focusChat = () => vscode.commands.executeCommand('omni-ai.chat.focus');
  const openChatPanel = () => {
    const panel = vscode.window.createWebviewPanel('omni-ai.chatPanel', 'Omni AI', vscode.ViewColumn.Active,
      { enableScripts: true, retainContextWhenHidden: true });
    panel.iconPath = vscode.Uri.joinPath(context.extensionUri, 'icon.png');
    const chat = new OmniChatView(context);
    chat.attach(panel.webview);
    panel.onDidDispose(() => chat.cancel());
  };

  const cmds = {
    'omni-ai.ask': ask,
    'omni-ai.explainSelection': explainSelection,
    'omni-ai.runOnSelection': runOnSelection,
    'omni-ai.openChat': focusChat,
    'omni-ai.openChatPanel': openChatPanel,
    'omni-ai.openChatTerminal': openChat,
    'omni-ai.doctor': doctor,
  };
  for (const [id, fn] of Object.entries(cmds)) {
    context.subscriptions.push(vscode.commands.registerCommand(id, fn));
  }

  ensureCli(context);
  firstRun(context);
}

// Install the omni-ai-cli (the `ai` command) in a terminal with the user's chosen manager.
function installCli(manager) {
  const term = vscode.window.createTerminal('Omni AI setup');
  term.show();
  const cmd = manager === 'brew'
    ? 'brew install samuelrms/tap/omni-ai-cli'
    : (manager === 'script'
      ? 'curl -fsSL https://raw.githubusercontent.com/samuelrms/omni-ai-cli/main/get.sh | bash'
      : 'npm install -g omni-ai-cli');
  term.sendText(cmd);
}

// If the `ai` CLI is missing, offer to install it (the extension only drives the CLI).
function ensureCli(context) {
  const bin = config().get('binPath', 'ai');
  let child;
  try { child = spawn(bin, ['--version'], { env: process.env }); } catch (e) { child = null; }
  const offer = () => {
    vscode.window.showWarningMessage(
      'Omni AI needs the `ai` CLI. Install it now?', 'npm', 'Homebrew', 'Script'
    ).then((pick) => {
      if (pick === 'npm') installCli('npm');
      else if (pick === 'Homebrew') installCli('brew');
      else if (pick === 'Script') installCli('script');
    });
  };
  if (!child) { offer(); return; }
  child.on('error', offer);
  child.on('close', (code) => { if (code !== 0) offer(); });
}

// On first activation, open the setup walkthrough so new users learn the commands.
function firstRun(context) {
  if (context.globalState.get('omni-ai.welcomed')) return;
  context.globalState.update('omni-ai.welcomed', true);
  vscode.commands.executeCommand('workbench.action.openWalkthrough', 'samuelrms.omni-ai-cli#omni-ai.start', false)
    .then(undefined, () => { /* walkthrough may be unavailable; ignore */ });
}

function deactivate() {}

module.exports = { activate, deactivate };
