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
  }

  // Bind to a webview from either the sidebar view or an editor panel.
  attach(webview) {
    this.webview = webview;
    webview.options = { enableScripts: true };
    webview.html = this.html();
    webview.onDidReceiveMessage((msg) => {
      if (msg.type === 'send') this.send(msg.text, { tool: msg.tool, mode: msg.mode });
      else if (msg.type === 'cancel') this.cancel();
      else if (msg.type === 'ready') { this.sendDefaults(); this.loadLast(); }
    });
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
    args.push('--', text);
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
  :root { --brand1:#f7a63b; --brand2:#7c5cff; }
  * { box-sizing: border-box; }
  body { font-family: var(--vscode-font-family); font-size: var(--vscode-font-size); color: var(--vscode-foreground); margin: 0; display: flex; flex-direction: column; height: 100vh; }
  #log { flex: 1; overflow-y: auto; padding: 16px; }
  #welcome { height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; gap: 8px; opacity: .92; }
  #welcome h1 { margin: 6px 0 0; font-size: 20px; font-weight: 600; }
  #welcome .tag { font-size: 12px; opacity: .6; max-width: 320px; }
  #welcome .hint { font-size: 12px; opacity: .5; margin-top: 10px; }
  .mark { filter: drop-shadow(0 2px 10px rgba(124,92,255,.35)); }
  .turn { margin: 14px 0; line-height: 1.5; white-space: pre-wrap; word-break: break-word; }
  .turn .who { display: inline-flex; align-items: center; gap: 6px; font-weight: 600; font-size: 12px; opacity: .8; margin-bottom: 3px; }
  .turn.user .who { color: var(--vscode-textLink-foreground); }
  .dot { width: 14px; height: 14px; border-radius: 50%; background: linear-gradient(135deg, var(--brand1), var(--brand2)); display: inline-block; }
  .err { color: var(--vscode-errorForeground); opacity: .85; }
  #barwrap { padding: 10px 12px 12px; border-top: 1px solid var(--vscode-panel-border); }
  #bar { display: flex; align-items: flex-end; gap: 8px; border: 1px solid var(--vscode-input-border, #3a3a3a); border-radius: 12px; padding: 8px 10px; background: var(--vscode-input-background); }
  #bar:focus-within { border-color: var(--brand2); }
  #q { flex: 1; resize: none; background: transparent; color: var(--vscode-input-foreground); border: none; outline: none; font: inherit; max-height: 160px; }
  .icon-btn { border: none; border-radius: 8px; width: 30px; height: 30px; cursor: pointer; color: #fff; display: grid; place-items: center; }
  #send { background: linear-gradient(135deg, var(--brand1), var(--brand2)); }
  #stop { background: var(--vscode-button-secondaryBackground, #444); }
  .icon-btn:disabled { opacity: .4; cursor: default; }
  .act { font-size: 12px; opacity: .6; margin: 4px 0; font-style: italic; }
  .controls { display: flex; gap: 8px; align-items: center; margin-top: 8px; }
  .controls label { font-size: 11px; opacity: .6; }
  .controls select { background: var(--vscode-input-background); color: var(--vscode-input-foreground); border: 1px solid var(--vscode-input-border, #3a3a3a); border-radius: 6px; padding: 2px 6px; font: inherit; font-size: 11px; }
</style></head><body>
<div id="log">
  <div id="welcome">
    ${LOGO}
    <h1>Omni AI</h1>
    <div class="tag">Claude · Codex · Cursor · Antigravity · Grok · DeepSeek · OpenRouter — one chat, automatic routing.</div>
    <div class="hint">Ask anything. Enter sends · Shift+Enter new line.</div>
  </div>
</div>
<div id="barwrap">
  <div id="bar">
    <textarea id="q" rows="1" placeholder="Ask anything…"></textarea>
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
<script>
  const vs = acquireVsCodeApi();
  const log = document.getElementById('log'), q = document.getElementById('q');
  const sendBtn = document.getElementById('send'), stopBtn = document.getElementById('stop');
  const toolSel = document.getElementById('tool'), modeSel = document.getElementById('mode');
  const welcome = document.getElementById('welcome');
  let answer = null, header = null, sendTime = 0, firstOut = 0;
  const hhmm = (d) => new Date(d).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const dur = (ms) => ms >= 60000 ? Math.floor(ms / 60000) + 'm' + Math.round(ms % 60000 / 1000) + 's' : (ms / 1000).toFixed(1) + 's';
  function clearWelcome() { if (welcome && welcome.parentNode) welcome.remove(); }
  function turn(role, text, time) {
    clearWelcome();
    const d = document.createElement('div'); d.className = 'turn ' + role;
    const who = document.createElement('div'); who.className = 'who';
    if (role === 'assistant') { const dot = document.createElement('span'); dot.className = 'dot'; who.appendChild(dot); who.appendChild(document.createTextNode('Omni')); }
    else { who.textContent = 'You'; }
    const meta = document.createElement('span'); meta.className = 'act'; meta.style.marginLeft = '8px'; meta.style.fontStyle = 'normal';
    if (time) meta.textContent = hhmm(time);
    who.appendChild(meta);
    const body = document.createElement('div'); body.textContent = text || '';
    d.appendChild(who); d.appendChild(body); log.appendChild(d); log.scrollTop = log.scrollHeight;
    return { body, meta };
  }
  function act(text) { clearWelcome(); const d = document.createElement('div'); d.className = 'act'; d.textContent = text; log.appendChild(d); log.scrollTop = log.scrollHeight; }
  function autosize() { q.style.height = 'auto'; q.style.height = Math.min(q.scrollHeight, 160) + 'px'; }
  function send() {
    const text = q.value.trim(); if (!text) return;
    turn('user', text, Date.now()); q.value = ''; autosize();
    answer = null; header = null; sendTime = Date.now(); firstOut = 0;
    sendBtn.disabled = true; stopBtn.disabled = false;
    vs.postMessage({ type: 'send', text, tool: toolSel.value, mode: modeSel.value });
  }
  sendBtn.onclick = send;
  stopBtn.onclick = () => vs.postMessage({ type: 'cancel' });
  q.addEventListener('input', autosize);
  q.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });
  window.addEventListener('message', (e) => {
    const m = e.data;
    if (m.type === 'history') {
      for (const msg of (m.messages || [])) turn(msg.role === 'assistant' ? 'assistant' : 'user', msg.content);
      answer = null;
    } else if (m.type === 'defaults') {
      if (m.tool) toolSel.value = m.tool; if (m.mode) modeSel.value = m.mode;
    } else if (m.type === 'seed') {
      q.value = m.text; autosize(); q.focus();
    } else if (m.type === 'act') {
      act(m.text);
    } else if (m.type === 'out') {
      if (m.stream === 'err') { act('✗ ' + m.text); }
      else {
        if (!answer) { const t = turn('assistant', ''); answer = t.body; header = t.meta; }
        if (!firstOut) { firstOut = Date.now(); if (header) header.textContent = 'thought ' + dur(firstOut - sendTime); }
        answer.textContent += m.text;
      }
      log.scrollTop = log.scrollHeight;
    } else if (m.type === 'done') {
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
}

function deactivate() {}

module.exports = { activate, deactivate };
