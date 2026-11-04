// Omni AI CLI — VS Code / Cursor extension.
// Thin wrapper around the `ai` CLI: it never reimplements routing, it just drives the command
// in an integrated terminal so logins, streaming and the chat work exactly as in a real terminal.
const vscode = require('vscode');
const os = require('os');
const fs = require('fs');
const path = require('path');
const { spawn, spawnSync } = require('child_process');

// Paths git sees as changed right now (porcelain, relative to repo root).
function gitStatusPaths(cwd) {
  const paths = [];
  try {
    const r = spawnSync('git', ['status', '--porcelain'], { cwd, encoding: 'utf8' });
    if (r.status !== 0 || !r.stdout) return paths;
    r.stdout.split('\n').forEach((line) => {
      if (!line.trim()) return;
      let p = line.slice(3);
      if (p.indexOf(' -> ') >= 0) p = p.split(' -> ')[1]; // renames
      paths.push(p.replace(/^"|"$/g, ''));
    });
  } catch (e) { /* no git */ }
  return paths;
}

// Repo root for a workspace (so relative git paths resolve to real files).
function gitRoot(cwd) {
  try {
    const r = spawnSync('git', ['rev-parse', '--show-toplevel'], { cwd, encoding: 'utf8' });
    if (r.status === 0 && r.stdout.trim()) return r.stdout.trim();
  } catch (e) {}
  return cwd;
}

// Snapshot of currently-dirty files with their mtimes, so we can tell what was touched this turn
// even for files that were already modified before it.
function changedSnapshot(cwd) {
  const root = gitRoot(cwd);
  const map = {};
  gitStatusPaths(cwd).forEach((p) => {
    try { const st = fs.statSync(path.join(root, p)); map[p] = st.mtimeMs + ':' + st.size; } catch (e) { /* skip unstattable */ }
  });
  return map;
}

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
    this._saving = false;
    this._acc = '';
    this._user = '';
  }

  convKey() { return 'omni.conv.' + workspaceCwd(); }
  loadConv() { try { return this.context.workspaceState.get(this.convKey(), []); } catch (e) { return []; } }
  pushHistory() {
    try {
      const hist = this.loadConv();
      for (let i = 0; i < arguments.length; i++) hist.push(arguments[i]);
      this.context.workspaceState.update(this.convKey(), hist.slice(-200));
    } catch (e) { /* ignore */ }
  }
  saveTurn(user, assistant) { const ts = Date.now(); this.pushHistory({ role: 'user', content: user, ts }, { role: 'assistant', content: assistant, ts }); }
  clearConv() { try { this.context.workspaceState.update(this.convKey(), []); this.context.workspaceState.update(this.decKey(), {}); } catch (e) {} }
  decKey() { return 'omni.dec.' + workspaceCwd(); }
  loadDec() { try { return this.context.workspaceState.get(this.decKey(), {}); } catch (e) { return {}; } }
  saveDec(map) { try { this.context.workspaceState.update(this.decKey(), map); } catch (e) {} }

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
      else if (msg.type === 'doctor') this.runCli(['--doctor'], '/doctor');
      else if (msg.type === 'cli') this.runCli(msg.args, msg.label);
      else if (msg.type === 'openFile') this.openFile(msg.path);
      else if (msg.type === 'setDefault') { try { config().update(msg.key, msg.value, true); } catch (e) {} }
      else if (msg.type === 'newConversation') this.clearConv();
      else if (msg.type === 'remote') this.remoteControl(msg.name);
      else if (msg.type === 'diff') this.openDiff(msg.path);
      else if (msg.type === 'diffInline') this.getDiff(msg.path);
      else if (msg.type === 'setDecision') { const dec = this.loadDec(); dec[msg.path] = msg.state; this.saveDec(dec); }
      else if (msg.type === 'revert') this.revertFile(msg.path);
      else if (msg.type === 'command') vscode.commands.executeCommand(msg.id);
      else if (msg.type === 'ready') { this.sendDefaults(); this.sendMenu(); this.loadLast(); }
    });
  }

  // Resolve a (possibly relative/basename) path to an absolute file in the workspace.
  async resolvePath(p) {
    const path = require('path'), fs = require('fs');
    const abs = path.isAbsolute(p) ? p : path.join(workspaceCwd(), p);
    try { fs.accessSync(abs); return abs; } catch (e) {}
    try {
      const hits = await vscode.workspace.findFiles('**/' + p.split('/').pop(), '**/node_modules/**', 5);
      const exact = hits.find((u) => u.fsPath.endsWith(p)) || hits[0];
      if (exact) return exact.fsPath;
    } catch (e) {}
    return abs;
  }

  // Produce the git diff for a file and send it to the chat to render inline.
  getDiff(p) {
    const root = gitRoot(workspaceCwd());
    const run = (args) => new Promise((resolve) => {
      let out = '', err = '';
      let c;
      try { c = spawn('git', args, { cwd: root }); } catch (e) { resolve({ out: '', err: String(e) }); return; }
      c.stdout.on('data', (d) => { out += d.toString(); });
      c.stderr.on('data', (d) => { err += d.toString(); });
      c.on('error', (e) => resolve({ out: '', err: String(e.message || e) }));
      c.on('close', () => resolve({ out, err }));
    });
    (async () => {
      try {
        let r = await run(['diff', '--', p]);
        if (!r.out.trim()) { const s = await run(['diff', '--staged', '--', p]); if (s.out.trim()) r = s; }
        if (!r.out.trim()) { const n = await run(['diff', '--no-index', '--', '/dev/null', p]); if (n.out.trim()) r = n; }
        let text = r.out;
        if (!text.trim()) {
          // No git diff (e.g. not a repo / no changes): fall back to showing the file content.
          try { text = fs.readFileSync(path.isAbsolute(p) ? p : path.join(root, p), 'utf8'); }
          catch (e) { text = '(no diff available' + (r.err ? ': ' + r.err.trim() : '') + ')'; }
        }
        this.post({ type: 'diffText', path: p, text });
      } catch (e) {
        this.post({ type: 'diffText', path: p, text: '(diff failed: ' + String(e.message || e) + ')' });
      }
    })();
  }

  // Open the git diff (working tree vs HEAD) for a changed file so the user can review it.
  async openDiff(p) {
    const abs = await this.resolvePath(p);
    try { await vscode.commands.executeCommand('git.openChange', vscode.Uri.file(abs)); return; } catch (e) {}
    try { await vscode.window.showTextDocument(vscode.Uri.file(abs), { preview: true }); } catch (e) { vscode.window.showWarningMessage('Omni AI: could not open ' + p); }
  }

  // Reject a change: restore the file from git HEAD.
  async revertFile(p) {
    const abs = await this.resolvePath(p);
    const pick = await vscode.window.showWarningMessage('Revert ' + p + ' to the last committed version?', { modal: true }, 'Revert');
    if (pick !== 'Revert') return;
    let child;
    try { child = spawn('git', ['checkout', '--', abs], { cwd: workspaceCwd() }); }
    catch (e) { vscode.window.showWarningMessage('Omni AI: git not available'); return; }
    child.on('close', (code) => {
      if (code === 0) {
        const dec = this.loadDec(); dec[p] = 'reverted'; this.saveDec(dec);
        this.post({ type: 'decision', path: p, state: 'reverted' });
        vscode.window.showInformationMessage('Reverted ' + p);
      } else vscode.window.showWarningMessage('Omni AI: could not revert ' + p + ' (new or untracked file?)');
    });
  }

  // Hand the current conversation off to Claude Remote Control in a terminal (pairing URL shows there).
  remoteControl(name) {
    const os = require('os'), fs = require('fs'), path = require('path');
    const conv = this.loadConv();
    const tail = conv.slice(-16).map((m) =>
      (m.role === 'user' ? 'User: ' : m.role === 'assistant' ? 'Assistant: ' : '[' + (m.label || 'cmd') + '] ') + (m.content || '')
    ).join('\n\n');
    const ctx = 'Continue this conversation remotely. Context:\n\n' + tail + '\n\nContinue from here.';
    const f = path.join(os.tmpdir(), 'omni-remote-' + Date.now() + '.txt');
    let seeded = false;
    try { fs.writeFileSync(f, ctx); seeded = true; } catch (e) { /* no seed */ }
    const sess = name || ('omni-' + path.basename(workspaceCwd()));
    const term = vscode.window.createTerminal({ name: 'Omni · Remote Control', cwd: workspaceCwd() });
    term.show();
    const bin = config().get('claudeBin', 'claude');
    term.sendText(bin + ' --remote-control ' + sess + (seeded ? ' "$(cat \'' + f + '\')"' : ''));
  }

  // Open a file referenced in the chat (click). Tries the path, then searches the workspace by basename.
  async openFile(p) {
    if (!p) return;
    const path = require('path');
    const show = async (uri) => { await vscode.window.showTextDocument(await vscode.workspace.openTextDocument(uri), { preview: true }); };
    // 1) absolute or relative-to-cwd, as given
    const abs = path.isAbsolute(p) ? p : path.join(workspaceCwd(), p);
    try { await show(vscode.Uri.file(abs)); return; } catch (e) { /* fall through to search */ }
    // 2) search the workspace by the basename
    const base = p.split('/').pop();
    let hits = [];
    try { hits = await vscode.workspace.findFiles('**/' + base, '**/node_modules/**', 20); } catch (e) { hits = []; }
    // prefer a hit whose path ends with the referenced path
    const exact = hits.find((u) => u.fsPath.endsWith(p)) || (hits.length === 1 ? hits[0] : null);
    if (exact) { try { await show(exact); return; } catch (e) {} }
    if (hits.length > 1) {
      const pick = await vscode.window.showQuickPick(
        hits.map((u) => ({ label: path.basename(u.fsPath), description: vscode.workspace.asRelativePath(u), uri: u })),
        { placeHolder: 'Open which ' + base + '?' }
      );
      if (pick) { try { await show(pick.uri); return; } catch (e) {} }
      else return;
    }
    vscode.window.showWarningMessage('Omni AI: could not find ' + p + ' in this workspace');
  }

  // Run a CLI subcommand (e.g. --usage, --status, --doctor) and stream it into the chat as preformatted text.
  runCli(args, label) {
    if (this.child) return;
    if (!Array.isArray(args) || !args.length) { this.post({ type: 'done', code: 0 }); return; }
    const bin = config().get('binPath', 'ai');
    label = label || ('/' + args.join(' ').replace(/^--/, ''));
    let acc = '';
    this.post({ type: 'cliBegin', label });
    let child;
    try { child = spawn(bin, args, { cwd: workspaceCwd(), env: { ...process.env, NO_COLOR: '1' } }); }
    catch (e) { this.post({ type: 'out', stream: 'err', text: String(e) }); this.post({ type: 'done', code: 1 }); return; }
    this.child = child;
    child.stdout.on('data', (d) => { const t = d.toString(); acc += t; this.post({ type: 'out', stream: 'out', pre: true, text: t }); });
    child.stderr.on('data', (d) => this.post({ type: 'out', stream: 'err', text: d.toString() }));
    child.on('error', (e) => this.post({ type: 'out', stream: 'err', text: String(e.message || e) }));
    child.on('close', (code) => {
      this.child = null;
      if (acc.trim()) { const ts = Date.now(); this.pushHistory({ role: 'user', content: label, ts }, { role: 'cli', label, content: acc, ts }); }
      this.post({ type: 'done', code });
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
    // Prefer this extension's own saved conversation for the workspace.
    const saved = this.loadConv();
    if (saved && saved.length) { this.post({ type: 'history', messages: saved, decisions: this.loadDec() }); return; }
    // Fall back to the CLI's omni history.
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

  // Ask the CLI for its live tools/modes/langs/skills so the chat menus match the terminal.
  sendMenu() {
    const bin = config().get('binPath', 'ai');
    let out = '';
    let child;
    try { child = spawn(bin, ['--menu-json'], { cwd: workspaceCwd(), env: { ...process.env, NO_COLOR: '1' } }); }
    catch (e) { return; }
    child.stdout.on('data', (d) => { out += d.toString(); });
    child.on('error', () => {});
    child.on('close', () => {
      try { this.post({ type: 'menu', menu: JSON.parse(out.trim()) }); } catch (e) { /* ignore */ }
    });
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
    this._saving = true; this._acc = ''; this._user = text;
    this._changed = new Set();
    this._gitBefore = changedSnapshot(workspaceCwd());
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
    child.on('close', (code) => {
      if (buf.trim()) this._event(buf);
      this.child = null;
      if (this._saving) { if (this._acc.trim()) this.saveTurn(this._user, this._acc.trim()); this._saving = false; }
      const root = gitRoot(workspaceCwd());
      const norm = (p) => { let q = p; if (path.isAbsolute(q)) { const rel = path.relative(root, q); if (rel && !rel.startsWith('..')) q = rel; } return q; };
      let files;
      if (this._changed && this._changed.size) {
        // The tool reported exactly which files it edited — use only those.
        files = new Set(Array.from(this._changed).map(norm));
      } else {
        // Tool emitted no file events: fall back to git, limited to files touched this turn.
        files = new Set();
        const after = changedSnapshot(workspaceCwd());
        const before = this._gitBefore || {};
        Object.keys(after).forEach((p) => { if (!(p in before) || after[p] !== before[p]) files.add(norm(p)); });
      }
      if (files.size) { const arr = Array.from(files); this.pushHistory({ role: 'changes', files: arr, ts: Date.now() }); this.post({ type: 'changes', files: arr }); }
      this.post({ type: 'done', code });
    });
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
    const answer = (text) => { if (text) { this.post({ type: 'out', stream: 'out', text }); if (this._saving) this._acc += text; } };
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
      else if (it.type === 'command_execution') {
        if (!done) this.post({ type: 'tool', label: '$', cmd: it.command || '' });
        else this.post({ type: 'toolDone', output: it.aggregated_output || '', failed: it.exit_code != null && it.exit_code !== 0 });
      }
      else if (it.type === 'file_change' && done) { const ps = (it.changes || []).map((c) => c.path); ps.forEach((p) => this._changed && this._changed.add(p)); act('✎ edited ' + ps.join(', ')); }
      else if (it.type === 'mcp_tool_call') {
        if (!done) this.post({ type: 'tool', label: '⚙', cmd: (it.server || '') + ' · ' + (it.tool || '') });
        else this.post({ type: 'toolDone', output: JSON.stringify(it.result || it.error || '', null, 2), failed: it.status === 'failed' });
      }
      else if (it.type === 'web_search' && !done) act('🔎 ' + firstLine(it.query || ''));
    } else if (kind === 'assistant') {
      for (const b of ((ev.message || {}).content || [])) {
        if (b.type === 'text') answer(b.text || '');
        else if (b.type === 'thinking') this.post({ type: 'think', text: b.thinking || b.text || '' });
        else if (b.type === 'tool_use') {
          const inp = b.input || {};
          const cmd = inp.command || inp.cmd || inp.file_path || inp.path || inp.query || JSON.stringify(inp);
          if (/^(edit|write|multiedit|notebookedit|create)/i.test(b.name || '') && (inp.file_path || inp.path)) { if (this._changed) this._changed.add(inp.file_path || inp.path); }
          this.post({ type: 'tool', label: b.name === 'Bash' ? '$' : '⚙', cmd: (b.name && b.name !== 'Bash' ? b.name + ' ' : '') + String(cmd || '') });
        }
      }
    } else if (kind === 'user') {
      for (const b of ((ev.message || {}).content || [])) {
        if (b.type === 'tool_result') {
          let c = b.content;
          if (Array.isArray(c)) c = c.map((x) => (x && x.text) || '').join('\n');
          this.post({ type: 'toolDone', output: String(c || ''), failed: !!b.is_error });
        }
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
  .turn { margin: 16px 0; line-height: 1.55; display: flex; flex-direction: column; }
  .turn.user { align-items: flex-end; }
  .turn.assistant { align-items: flex-start; }
  .turn .who { display: inline-flex; align-items: center; gap: 6px; font-weight: 600; font-size: 12px; opacity: .85; margin-bottom: 4px; }
  .turn.user .who { color: var(--vscode-textLink-foreground); }
  .turn.user .body { background: rgba(124,92,255,.14); border: 1px solid rgba(124,92,255,.35); border-radius: 14px 14px 4px 14px; padding: 8px 12px; max-width: 78%; }
  .turn.assistant .body { max-width: 100%; }
  .turn .foot { font-size: 11px; opacity: .5; margin-top: 4px; }
  .steps { align-self: stretch; margin: 2px 0; }
  .steps summary { font-size: 12px; opacity: .7; cursor: pointer; font-style: italic; list-style: none; }
  .steps summary::-webkit-details-marker { display: none; }
  .steps .line { font-size: 12px; opacity: .6; margin: 2px 0 2px 6px; font-style: italic; }
  .steps.live summary::after { content: ' …'; animation: blink 1.2s infinite; }
  @keyframes blink { 50% { opacity: .3; } }
  .think { align-self: stretch; margin: 2px 0 6px; }
  .think summary { font-size: 12px; opacity: .7; cursor: pointer; }
  .think .r { font-size: 12.5px; opacity: .75; white-space: pre-wrap; border-left: 2px solid var(--brand2); padding-left: 10px; margin-top: 4px; }
  .tool { align-self: stretch; margin: 3px 0 3px 6px; }
  .tool > summary { font-size: 12px; opacity: .75; cursor: pointer; font-family: var(--vscode-editor-font-family, monospace); white-space: pre-wrap; list-style: none; }
  .tool > summary::-webkit-details-marker { display: none; }
  .tool > summary::before { content: '▸ '; opacity: .6; }
  .tool[open] > summary::before { content: '▾ '; }
  .tool .tout { font-size: 12px; opacity: .85; white-space: pre-wrap; background: var(--vscode-textCodeBlock-background, rgba(127,127,127,.12)); padding: 6px 8px; border-radius: 6px; margin-top: 4px; max-height: 320px; overflow: auto; font-family: var(--vscode-editor-font-family, monospace); }
  .tool.fail > summary { color: var(--vscode-errorForeground); }
  .mermaid { background: var(--vscode-textCodeBlock-background, rgba(127,127,127,.08)); border-radius: 8px; padding: 8px; margin: 6px 0; text-align: center; }
  .clicard { align-self: stretch; border: 1px solid var(--vscode-panel-border); border-radius: 10px; overflow: hidden; margin: 2px 0; }
  .clihead { font-size: 11px; padding: 4px 10px; background: rgba(124,92,255,.14); font-family: var(--vscode-editor-font-family, monospace); opacity: .85; border-bottom: 1px solid var(--vscode-panel-border); }
  .clicard .body { padding: 10px 12px; font-size: 12.5px; }
  .clicard .body pre { margin: 0; border-radius: 0; background: transparent; font-size: 12px; padding: 0; }
  .cl-h { font-weight: 600; margin-top: 10px; }
  .cl-h:first-child { margin-top: 0; }
  .cl-t { opacity: .85; margin: 1px 0; }
  .cl-sp { height: 6px; }
  .cl-r { display: flex; align-items: center; gap: 8px; margin: 2px 0; }
  .cl-lbl { min-width: 90px; opacity: .85; }
  .cl-bar { flex: 0 0 150px; height: 8px; border-radius: 4px; background: rgba(127,127,127,.25); overflow: hidden; }
  .cl-bar > span { display: block; height: 100%; background: linear-gradient(90deg, var(--brand1), var(--brand2)); }
  .cl-pct { width: 42px; text-align: right; opacity: .8; }
  .cl-aft { opacity: .6; }
  .review { align-self: stretch; border: 1px solid var(--brand2); border-radius: 10px; margin: 8px 0; overflow: hidden; }
  .rv-h { font-size: 12px; font-weight: 600; padding: 6px 10px; background: rgba(124,92,255,.14); }
  .rv-body { padding: 8px 10px; }
  .rv-file { font-family: var(--vscode-editor-font-family, monospace); font-size: 12px; opacity: .85; word-break: break-all; margin-bottom: 6px; }
  .rv-actions { display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; }
  .rv-btn { font: inherit; font-size: 12px; padding: 4px 12px; border-radius: 6px; border: 1px solid var(--vscode-panel-border); background: transparent; color: var(--vscode-foreground); cursor: pointer; }
  .rv-btn.ok { border-color: #4ec9b0; color: #4ec9b0; }
  .rv-btn.ok:hover { background: rgba(78,201,176,.12); }
  .rv-btn.no { border-color: var(--vscode-errorForeground); color: var(--vscode-errorForeground); }
  .rv-btn.no:hover { background: rgba(244,135,113,.12); }
  .rv-sum { display: flex; align-items: center; gap: 10px; padding: 3px 0; }
  .rv-sum-t { min-width: 90px; font-size: 12px; opacity: .8; }
  .rv-sum-t.ok { color: #4ec9b0; }
  .rv-sum-t.rej { color: var(--vscode-errorForeground); }
  .rv-sum-f { font-family: var(--vscode-editor-font-family, monospace); font-size: 12px; cursor: pointer; word-break: break-all; }
  .rv-sum-f:hover { color: var(--brand2); text-decoration: underline; }
  .diffblock { align-self: stretch; margin: 6px 0; border: 1px solid var(--vscode-panel-border); border-radius: 8px; overflow: hidden; }
  .diffblock > summary { font-size: 12px; padding: 6px 10px; cursor: pointer; opacity: .8; font-family: var(--vscode-editor-font-family, monospace); }
  .diffpre { margin: 0; padding: 8px 10px; font-size: 12px; overflow-x: auto; white-space: pre; font-family: var(--vscode-editor-font-family, monospace); }
  .diffpre .dl-add { display: block; color: #4ec9b0; background: rgba(78,201,176,.08); }
  .diffpre .dl-del { display: block; color: #f48771; background: rgba(244,135,113,.08); }
  .diffpre .dl-hunk { display: block; color: #569cd6; }
  .diffpre span { display: block; }
  .turn .body { word-break: break-word; }
  .turn .body p { margin: 6px 0; }
  .turn .body h1,.turn .body h2,.turn .body h3,.turn .body h4 { margin: 10px 0 4px; }
  .turn .body h4 { font-size: 1em; opacity: .9; }
  .turn .body hr { border: none; border-top: 1px solid var(--vscode-panel-border); margin: 10px 0; }
  .turn .body ul,.turn .body ol { margin: 4px 0; padding-left: 22px; }
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
  #bar { position: relative; display: flex; align-items: center; gap: 6px; border: 1px solid var(--vscode-input-border, #3a3a3a); border-radius: 12px; padding: 4px 6px 4px 10px; background: var(--vscode-input-background); }
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
  #plusmenu { position: absolute; left: 0; bottom: calc(100% + 6px); background: var(--vscode-editorWidget-background, #252526); border: 1px solid var(--vscode-panel-border); border-radius: 8px; overflow: hidden; min-width: 240px; z-index: 5; box-shadow: 0 4px 16px rgba(0,0,0,.35); }
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
    <div id="chips"></div>
    <div id="bar">
      <div id="plusmenu" style="display:none">
        <div class="mi" id="pm-attach">📎 Attach files as context</div>
        <div class="mi" id="pm-voice">🎙 Voice input (dictate)</div>
      </div>
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
<script src="https://cdn.jsdelivr.net/npm/mermaid@10.9.3/dist/mermaid.min.js"></script>
<script>
  var vs = acquireVsCodeApi();
  try { if (window.mermaid) window.mermaid.initialize({ startOnLoad: false, securityLevel: 'loose', theme: (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'default' }); } catch (e) {}
  var log = document.getElementById('log'), q = document.getElementById('q');
  var sendBtn = document.getElementById('send'), stopBtn = document.getElementById('stop'), attachBtn = document.getElementById('attach');
  var toolSel = document.getElementById('tool'), modeSel = document.getElementById('mode');
  var chips = document.getElementById('chips'), menu = document.getElementById('menu');
  var welcome = document.getElementById('welcome');
  var cur = null, menuIdx = 0, DECISIONS = {};
  // CLI-backed commands: run the CLI with these flags and show the output (synced with the terminal).
  var CLI_MAP = {
    '/usage': ['--usage'], '/limits': ['--usage'], '/status': ['--status'], '/doctor': ['--doctor'],
    '/model': ['--model'], '/account': ['--account'], '/key': ['--key'], '/keys': ['--keys'],
    '/sync': ['--sync'], '/update': ['--update'], '/reset': ['--reset'], '/setup': ['--setup'],
    '/versions': ['--version'], '/log': ['--log']
  };
  // Full command list, mirrored from the terminal chat. needsArg → inserted into the input to type the value.
  var COMMANDS = [
    { cmd: '/help', desc: 'List commands' },
    { cmd: '/clear', desc: 'Clear this chat' },
    { cmd: '/usage', desc: 'Usage and limits' },
    { cmd: '/limits', desc: 'Usage and limits' },
    { cmd: '/status', desc: 'Accounts and keys status' },
    { cmd: '/doctor', desc: 'Health check' },
    { cmd: '/model', desc: 'List or set models', needsArg: true },
    { cmd: '/account', desc: 'Manage accounts (list|add|login|sync|remove)', needsArg: true },
    { cmd: '/key', desc: 'Set an API key', needsArg: true },
    { cmd: '/keys', desc: 'List API keys' },
    { cmd: '/sync', desc: 'Sync MCPs and skills across tools' },
    { cmd: '/update', desc: 'Update the CLI' },
    { cmd: '/reset', desc: 'Reset cooldowns' },
    { cmd: '/setup', desc: 'First-run setup' },
    { cmd: '/versions', desc: 'Show version' },
    { cmd: '/log', desc: 'Show usage log' },
    { cmd: '/tool', desc: 'Set tool (auto|claude|codex|…)', needsArg: true },
    { cmd: '/mode', desc: 'Set mode (default|plan|edits|auto|yolo)', needsArg: true },
    { cmd: '/lang', desc: 'Interface language', needsArg: true },
    { cmd: '/remote-control', desc: 'Hand off to Claude Remote Control', needsArg: false },
    { cmd: '/history', desc: 'Open history' },
    { cmd: '/sessions', desc: 'Open sessions (history)' },
    { cmd: '/new', desc: 'Start a new conversation' },
    { cmd: '/resume', desc: 'Resume a conversation (history)' }
  ];
  var SKILLS = [];
  var F = String.fromCharCode(96);
  function isPath(t){ return /\\.(ts|tsx|js|jsx|py|go|rs|java|rb|json|md|ya?ml|sh|css|scss|html|sql|toml|txt)$/i.test(t) || (/\\//.test(t) && /\\.[a-z0-9]+$/i.test(t)); }
  function esc(t){ return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
  function md(s){
    var codes = [];
    s = s.replace(new RegExp(F+F+F+'([\\\\s\\\\S]*?)'+F+F+F,'g'), function(_,c){ codes.push(c); return '\\u0000'+(codes.length-1)+'\\u0000'; });
    s = esc(s);
    s = s.replace(/^#{4,6} (.*)$/gm,'<h4>$1</h4>').replace(/^### (.*)$/gm,'<h3>$1</h3>').replace(/^## (.*)$/gm,'<h2>$1</h2>').replace(/^# (.*)$/gm,'<h1>$1</h1>');
    s = s.replace(/^\\s*([-*_])\\1\\1[-*_ ]*$/gm,'<hr>');
    s = s.replace(/^\\s*[-*] (.*)$/gm,'<li>$1</li>');
    s = s.replace(/(<li>[\\s\\S]*?<\\/li>)/g, function(m){ return '<ul>'+m+'</ul>'; });
    s = s.replace(/^\\s*\\d+\\. (.*)$/gm,'<oli>$1</oli>');
    s = s.replace(/(<oli>[\\s\\S]*?<\\/oli>)/g, function(m){ return '<ol>'+m.replace(/<oli>/g,'<li>').replace(/<\\/oli>/g,'</li>')+'</ol>'; });
    s = s.replace(new RegExp(F+'([^'+F+']+)'+F,'g'), function(_, c){
      return isPath(c) ? '<code class="file" title="Open file" data-p="' + c.replace(/"/g,'&quot;') + '">' + c + '</code>' : '<code>' + c + '</code>';
    });
    s = s.replace(/\\*\\*([^*]+)\\*\\*/g,'<b>$1</b>').replace(/\\*([^*]+)\\*/g,'<i>$1</i>');
    s = s.replace(/\\[([^\\]]+)\\]\\((https?:[^)]+)\\)/g,'<span class="link">$1</span>');
    s = s.replace(/\\n/g,'<br>');
    s = s.replace(/\\u0000(\\d+)\\u0000/g, function(_, i){
      var raw = codes[i];
      var first = raw.match(/^([^\\n]*)\\n([\\s\\S]*)$/);
      var lang = '', body = raw;
      if (first && /^[a-zA-Z0-9_+#-]{1,15}$/.test(first[1].trim())){ lang = first[1].trim().toLowerCase(); body = first[2]; }
      if (lang === 'mermaid'){ return '<div class="mermaid" data-code="' + encodeURIComponent(body) + '"></div>'; }
      return '<pre><code>' + esc(lang ? body : raw) + '</code></pre>';
    });
    return s;
  }
  var hhmm = function(d){ return new Date(d).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); };
  var dur = function(ms){ return ms >= 60000 ? Math.floor(ms/60000)+'m'+Math.round(ms%60000/1000)+'s' : (ms/1000).toFixed(1)+'s'; };
  function clearWelcome(){ if (welcome && welcome.parentNode) welcome.remove(); }
  function colorDiff(t){
    return esc(t).split('\\n').map(function(l){
      var c = '';
      if (l.indexOf('+') === 0 && l.indexOf('+++') !== 0) c = 'dl-add';
      else if (l.indexOf('-') === 0 && l.indexOf('---') !== 0) c = 'dl-del';
      else if (l.indexOf('@@') === 0) c = 'dl-hunk';
      return '<span class="' + c + '">' + (l || ' ') + '</span>';
    }).join('');
  }
  function appendDiffBlock(pth, text){
    var d = document.createElement('details'); d.className = 'diffblock'; d.open = true;
    var s = document.createElement('summary'); s.textContent = 'diff · ' + pth;
    var pre = document.createElement('pre'); pre.className = 'diffpre'; pre.innerHTML = colorDiff(text || '');
    d.appendChild(s); d.appendChild(pre); thread.appendChild(d); scroll();
  }
  function renderMermaid(){
    if (!window.mermaid) return;
    var els = thread.querySelectorAll('.mermaid:not([data-done])');
    Array.prototype.forEach.call(els, function(el){
      el.setAttribute('data-done', '1');
      try { el.textContent = decodeURIComponent(el.getAttribute('data-code') || ''); } catch (e) { el.textContent = el.getAttribute('data-code') || ''; }
      try { window.mermaid.run({ nodes: [el] }); } catch (e) { /* leave as text on parse error */ }
    });
  }
  var thread = document.getElementById('thread');
  thread.addEventListener('click', function(e){
    var el = e.target;
    if (el && el.classList && el.classList.contains('file')) { vs.postMessage({ type: 'openFile', path: el.getAttribute('data-p') }); }
  });
  function scroll(){ log.scrollTop = log.scrollHeight; }
  // Sequential, file-by-file review: show one diff, approve/reject, auto-advance to the next.
  var activeReview = null;
  function renderChanges(files){
    if (!files || !files.length) return;
    files = files.filter(function(f, i){ return files.indexOf(f) === i; }); // dedup
    clearWelcome();
    var d = document.createElement('div'); d.className = 'review';
    var head = document.createElement('div'); head.className = 'rv-h';
    var body = document.createElement('div'); body.className = 'rv-body';
    d.appendChild(head); d.appendChild(body); thread.appendChild(d);
    var state = { files: files, idx: 0, head: head, body: body, diffBox: null, curFile: null };
    activeReview = state;
    reviewStep(state);
  }
  function reviewStep(state){
    while (state.idx < state.files.length && DECISIONS[state.files[state.idx]]) state.idx++;
    if (state.idx >= state.files.length){ renderReviewSummary(state); return; }
    var f = state.files[state.idx];
    state.curFile = f;
    state.head.textContent = 'Review ' + (state.idx + 1) + '/' + state.files.length;
    state.body.innerHTML = '';
    var fname = document.createElement('div'); fname.className = 'rv-file'; fname.textContent = f;
    var diffBox = document.createElement('pre'); diffBox.className = 'diffpre'; diffBox.textContent = 'loading diff…';
    var actions = document.createElement('div'); actions.className = 'rv-actions';
    function mk(txt, cls, fn){ var b = document.createElement('button'); b.className = 'rv-btn' + (cls ? ' ' + cls : ''); b.textContent = txt; b.onclick = fn; return b; }
    actions.appendChild(mk('✓ Approve', 'ok', function(){ DECISIONS[f] = 'kept'; vs.postMessage({ type: 'setDecision', path: f, state: 'kept' }); state.idx++; reviewStep(state); }));
    actions.appendChild(mk('✗ Reject (revert)', 'no', function(){ DECISIONS[f] = 'reverted'; vs.postMessage({ type: 'revert', path: f }); state.idx++; reviewStep(state); }));
    actions.appendChild(mk('Open', '', function(){ vs.postMessage({ type: 'diff', path: f }); }));
    actions.appendChild(mk('Skip', '', function(){ state.idx++; reviewStep(state); }));
    state.body.appendChild(fname); state.body.appendChild(diffBox); state.body.appendChild(actions);
    state.diffBox = diffBox;
    vs.postMessage({ type: 'diffInline', path: f });
    scroll();
  }
  function renderReviewSummary(state){
    state.head.textContent = 'Review complete';
    state.body.innerHTML = '';
    state.files.forEach(function(f){
      var dec = DECISIONS[f] || 'pending';
      var row = document.createElement('div'); row.className = 'rv-sum';
      var t = document.createElement('span'); t.className = 'rv-sum-t ' + (dec === 'reverted' ? 'rej' : dec === 'kept' ? 'ok' : '');
      t.textContent = dec === 'kept' ? '✓ kept' : dec === 'reverted' ? '↩ reverted' : '• pending';
      var s = document.createElement('span'); s.className = 'rv-sum-f'; s.textContent = f; s.onclick = function(){ vs.postMessage({ type: 'diffInline', path: f }); };
      row.appendChild(t); row.appendChild(s); state.body.appendChild(row);
    });
    if (activeReview === state) activeReview = null;
    scroll();
  }
  // A user message: right-aligned bubble with a time footer.
  function userTurn(text, time){
    clearWelcome();
    var d = document.createElement('div'); d.className = 'turn user';
    var who = document.createElement('div'); who.className = 'who'; who.textContent = 'You';
    var body = document.createElement('div'); body.className = 'body'; body._raw = text || ''; body.innerHTML = md(body._raw);
    var foot = document.createElement('div'); foot.className = 'foot'; foot.textContent = hhmm(time || Date.now());
    d.appendChild(who); d.appendChild(body); d.appendChild(foot); thread.appendChild(d); scroll();
  }
  // The assistant exchange: steps (tool activity) + thinking + answer + footer with timings.
  function startAssistant(){
    clearWelcome();
    var d = document.createElement('div'); d.className = 'turn assistant';
    var who = document.createElement('div'); who.className = 'who';
    var dot = document.createElement('span'); dot.className = 'dot'; who.appendChild(dot); who.appendChild(document.createTextNode('Omni'));
    var steps = document.createElement('details'); steps.className = 'steps live'; steps.open = true;
    var ss = document.createElement('summary'); ss.textContent = 'Working'; steps.appendChild(ss);
    var think = document.createElement('details'); think.className = 'think'; think.style.display = 'none';
    var ts = document.createElement('summary'); ts.textContent = 'Thinking'; var tr = document.createElement('div'); tr.className = 'r'; tr.textContent = '';
    think.appendChild(ts); think.appendChild(tr);
    var body = document.createElement('div'); body.className = 'body'; body._raw = '';
    var foot = document.createElement('div'); foot.className = 'foot'; foot.textContent = '';
    d.appendChild(who); d.appendChild(steps); d.appendChild(think); d.appendChild(body); d.appendChild(foot);
    thread.appendChild(d); scroll();
    cur = { el: d, steps: steps, think: think, thinkBody: tr, body: body, foot: foot, start: Date.now(), firstOut: 0 };
    return cur;
  }
  function ensureAssistant(){ if (!cur) startAssistant(); return cur; }
  // A CLI command result (e.g. /usage): a bordered card with a command header and monospace body.
  function startCliCard(label){
    clearWelcome();
    var d = document.createElement('div'); d.className = 'turn assistant';
    var who = document.createElement('div'); who.className = 'who'; var dot = document.createElement('span'); dot.className = 'dot'; who.appendChild(dot); who.appendChild(document.createTextNode('Omni'));
    var card = document.createElement('div'); card.className = 'clicard';
    var head = document.createElement('div'); head.className = 'clihead'; head.textContent = '⚙ ' + (label || 'command');
    var body = document.createElement('div'); body.className = 'body'; body._raw = '';
    card.appendChild(head); card.appendChild(body);
    var foot = document.createElement('div'); foot.className = 'foot'; foot.textContent = '';
    d.appendChild(who); d.appendChild(card); d.appendChild(foot);
    thread.appendChild(d); scroll();
    cur = { el: d, steps: null, think: null, thinkBody: null, body: body, foot: foot, start: Date.now(), firstOut: 0, pre: true, cli: true, label: label };
    return cur;
  }
  // Turn CLI text (usage/status/…) into a native card: section headers, HTML bars, rows — no terminal glyphs.
  function renderCli(label, text){
    var lines = String(text).replace(/\\r/g, '').split('\\n');
    var html = '';
    lines.forEach(function(line){
      if (!line.trim()){ html += '<div class="cl-sp"></div>'; return; }
      var indent = (line.match(/^\\s*/)[0] || '').length;
      var barm = line.match(/([█░▓▒#=.-]{4,})\\s*(\\d+)%/);
      if (barm){
        var pct = Math.max(0, Math.min(100, parseInt(barm[2], 10)));
        var bi = line.indexOf(barm[1]);
        var before = esc(line.slice(0, bi).trim());
        var after = esc(line.slice(line.indexOf(barm[2], bi) + barm[2].length + 1).trim());
        html += '<div class="cl-r" style="padding-left:' + (indent * 4) + 'px">'
          + '<span class="cl-lbl">' + before + '</span>'
          + '<span class="cl-bar"><span style="width:' + pct + '%"></span></span>'
          + '<span class="cl-pct">' + pct + '%</span>'
          + (after ? '<span class="cl-aft">' + after + '</span>' : '')
          + '</div>';
      } else {
        var cls = indent === 0 ? 'cl-h' : 'cl-t';
        html += '<div class="' + cls + '" style="padding-left:' + (indent * 4) + 'px">' + esc(line.trim()) + '</div>';
      }
    });
    return html;
  }
  function cliStatic(label, content, ts){
    clearWelcome();
    var d = document.createElement('div'); d.className = 'turn assistant';
    var who = document.createElement('div'); who.className = 'who'; var dot = document.createElement('span'); dot.className = 'dot'; who.appendChild(dot); who.appendChild(document.createTextNode('Omni'));
    var card = document.createElement('div'); card.className = 'clicard';
    var head = document.createElement('div'); head.className = 'clihead'; head.textContent = (label || 'command');
    var body = document.createElement('div'); body.className = 'body'; body.innerHTML = renderCli(label, content || '');
    card.appendChild(head); card.appendChild(body);
    d.appendChild(who); d.appendChild(card); if (ts) d.appendChild(footEl(ts)); thread.appendChild(d); scroll();
  }
  function stepLine(text){ var c = ensureAssistant(); var l = document.createElement('div'); l.className = 'line'; l.textContent = text; c.steps.appendChild(l); scroll(); }
  function toolStart(label, cmd){ var c = ensureAssistant(); var d = document.createElement('details'); d.className = 'tool'; var s = document.createElement('summary'); s.textContent = (label || '$') + ' ' + (cmd || ''); var pre = document.createElement('pre'); pre.className = 'tout'; pre.textContent = '…'; d.appendChild(s); d.appendChild(pre); c.steps.appendChild(d); c.tool = d; c.toolPre = pre; scroll(); }
  function toolDone(output, failed){ var c = cur; if (!c || !c.toolPre){ return; } c.toolPre.textContent = (output && output.trim()) ? output : '(no output)'; if (failed) c.tool.classList.add('fail'); c.tool = null; c.toolPre = null; scroll(); }
  function thinkAppend(text){ var c = ensureAssistant(); c.think.style.display = ''; c.thinkBody.textContent += text; scroll(); }
  function outAppend(text, pre){ var c = ensureAssistant(); if (!c.firstOut){ c.firstOut = Date.now(); if (!c.cli) c.pre = !!pre; } c.body._raw += text; c.body.innerHTML = c.cli ? renderCli(c.label, c.body._raw) : (c.pre ? ('<pre>' + esc(c.body._raw) + '</pre>') : md(c.body._raw)); scroll(); }
  function finishTurn(){
    if (!cur) return;
    if (cur.steps){ cur.steps.classList.remove('live'); cur.steps.querySelector('summary').textContent = 'Steps'; cur.steps.open = false; }
    if (cur.think){ if (!cur.think.querySelector('.r').textContent) cur.think.style.display = 'none'; else cur.think.open = false; }
    var end = Date.now();
    var parts = [];
    if (cur.firstOut) parts.push('thought ' + dur(cur.firstOut - cur.start));
    parts.push('done ' + hhmm(end)); parts.push(dur(end - cur.start));
    cur.foot.textContent = parts.join(' · ');
    cur = null;
    renderMermaid();
  }
  function autosize(){ q.style.height = 'auto'; q.style.height = Math.min(q.scrollHeight, 160) + 'px'; }
  function notify(text){ clearWelcome(); var d = document.createElement('div'); d.className = 'act'; d.textContent = text; thread.appendChild(d); scroll(); }
  function footEl(ts){ var f = document.createElement('div'); f.className = 'foot'; if (ts) f.textContent = hhmm(ts); return f; }
  function assistantStatic(text, ts){
    clearWelcome();
    var d = document.createElement('div'); d.className = 'turn assistant';
    var who = document.createElement('div'); who.className = 'who';
    var dot = document.createElement('span'); dot.className = 'dot'; who.appendChild(dot); who.appendChild(document.createTextNode('Omni'));
    var body = document.createElement('div'); body.className = 'body'; body.innerHTML = md(text || '');
    d.appendChild(who); d.appendChild(body); if (ts) d.appendChild(footEl(ts)); thread.appendChild(d); scroll(); renderMermaid();
  }
  function send(){
    var text = q.value.trim(); if (!text) return;
    if (text.charAt(0) === '/'){ runSlash(text); return; }
    userTurn(text, Date.now()); q.value = ''; autosize(); hideMenu();
    cur = null; sendBtn.disabled = true; stopBtn.disabled = false;
    vs.postMessage({ type: 'send', text: text, tool: toolSel.value, mode: modeSel.value });
  }
  function isSkill(cmd){ return SKILLS.some(function(s){ return s.cmd === cmd; }); }
  // Run any terminal-style slash command.
  function runSlash(line){
    hideMenu();
    var parts = line.trim().split(/\\s+/); var cmd = parts[0].toLowerCase(); var rest = parts.slice(1);
    q.value = ''; autosize();
    if (cmd === '/clear'){ thread.innerHTML = ''; cur = null; return; }
    if (cmd === '/help'){ assistantStatic(helpText()); return; }
    if (cmd === '/history' || cmd === '/sessions' || cmd === '/resume'){ vs.postMessage({ type: 'command', id: 'omni-ai.history.focus' }); return; }
    if (cmd === '/new'){ thread.innerHTML = ''; cur = null; vs.postMessage({ type: 'newConversation' }); notify('new conversation'); return; }
    if (cmd === '/tool'){ if (rest[0]){ toolSel.value = rest[0]; vs.postMessage({ type: 'setDefault', key: 'tool', value: rest[0] }); notify('tool: ' + rest[0]); } else notify('tools: ' + optsOf(toolSel)); return; }
    if (cmd === '/mode'){ if (rest[0]){ modeSel.value = rest[0]; vs.postMessage({ type: 'setDefault', key: 'mode', value: rest[0] }); notify('mode: ' + rest[0]); } else notify('modes: ' + optsOf(modeSel)); return; }
    if (cmd === '/lang'){ notify('Interface language follows the CLI; set it with the terminal chat.'); return; }
    if (cmd === '/update'){ runCli('/update', ['--update']); vs.postMessage({ type: 'command', id: 'workbench.extensions.action.checkForUpdates' }); notify('Checking for extension updates…'); return; }
    if (cmd === '/remote-control' || cmd === '/remote-mode' || cmd === '/remote'){ notify('Starting Claude Remote Control in a terminal (pairing URL appears there)…'); vs.postMessage({ type: 'remote', name: rest[0] || '' }); return; }
    if (CLI_MAP[cmd]){ runCli(cmd + (rest.length ? ' ' + rest.join(' ') : ''), CLI_MAP[cmd].concat(rest)); return; }
    if (isSkill(cmd)){ userTurn(line, Date.now()); cur = null; sendBtn.disabled = true; stopBtn.disabled = false; vs.postMessage({ type: 'send', text: line, tool: toolSel.value, mode: modeSel.value }); return; }
    notify('Unknown command: ' + cmd);
  }
  function optsOf(sel){ return Array.prototype.map.call(sel.options, function(o){ return o.value; }).join(', '); }
  function helpText(){
    var lines = ['## Commands'];
    COMMANDS.forEach(function(c){ lines.push('- ' + F + c.cmd + F + ' — ' + c.desc); });
    if (SKILLS.length){ lines.push('## Skills'); SKILLS.forEach(function(s){ lines.push('- ' + F + s.cmd + F + ' — ' + s.desc); }); }
    return lines.join('\\n');
  }
  function startVoice(){
    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR){ notify('Voice input is not available in this webview.'); return; }
    var r = new SR(); r.lang = navigator.language || 'en-US'; r.interimResults = true; r.continuous = false;
    var base = q.value;
    r.onresult = function(ev){ var t = ''; for (var i = 0; i < ev.results.length; i++) t += ev.results[i][0].transcript; q.value = (base ? base + ' ' : '') + t; autosize(); };
    r.onerror = function(){ notify('Voice input error (check mic permission).'); };
    try { r.start(); notify('🎙 listening…'); } catch (e) { notify('Voice input could not start.'); }
  }
  function runCli(label, args){
    userTurn(label, Date.now());
    cur = null; sendBtn.disabled = true; stopBtn.disabled = false;
    vs.postMessage({ type: 'cli', args: args });
  }
  function matches(){ var v = q.value.trim().toLowerCase().split(/\\s+/)[0]; return COMMANDS.concat(SKILLS).filter(function(c){ return c.cmd.indexOf(v) === 0; }); }
  function menuPick(c){ if (c.needsArg){ q.value = c.cmd + ' '; hideMenu(); q.focus(); autosize(); } else { runSlash(c.cmd); } }
  function showMenu(){
    var list = matches(); if (q.value.charAt(0) !== '/' || !list.length){ hideMenu(); return; }
    menuIdx = Math.min(menuIdx, list.length - 1);
    menu.innerHTML = '';
    list.slice(0, 12).forEach(function(c, i){
      var el = document.createElement('div'); el.className = 'mi' + (i === menuIdx ? ' sel' : '');
      el.innerHTML = '<b>' + c.cmd + '</b><span style="opacity:.6">' + c.desc + '</span>';
      el.onclick = function(){ menuPick(c); };
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
      if (e.key === 'Enter'){ e.preventDefault(); if (list[menuIdx]) menuPick(list[menuIdx]); return; }
      if (e.key === 'Escape'){ hideMenu(); return; }
    }
    if (e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); send(); }
  });
  window.addEventListener('message', function(e){
    var m = e.data;
    if (m.type === 'history'){
      DECISIONS = m.decisions || {};
      (m.messages || []).forEach(function(msg){ if (msg.role === 'changes') renderChanges(msg.files); else if (msg.role === 'cli') cliStatic(msg.label, msg.content, msg.ts); else if (msg.role === 'assistant') assistantStatic(msg.content, msg.ts); else userTurn(msg.content, msg.ts); });
      cur = null;
    } else if (m.type === 'defaults'){
      if (m.tool) toolSel.value = m.tool; if (m.mode) modeSel.value = m.mode;
    } else if (m.type === 'menu'){
      var mn = m.menu || {};
      if (mn.tools && mn.tools.length){ var tv = toolSel.value; toolSel.innerHTML = ''; mn.tools.forEach(function(t){ var o = document.createElement('option'); o.value = t; o.textContent = t === 'agy' ? 'antigravity' : t; toolSel.appendChild(o); }); if (mn.tools.indexOf(tv) >= 0) toolSel.value = tv; }
      if (mn.modes && mn.modes.length){ var mv = modeSel.value; modeSel.innerHTML = ''; mn.modes.forEach(function(x){ var o = document.createElement('option'); o.value = x; o.textContent = x; modeSel.appendChild(o); }); if (mn.modes.indexOf(mv) >= 0) modeSel.value = mv; }
      SKILLS = (mn.skills || []).map(function(s){ return { cmd: '/' + s, desc: 'Project skill' }; });
    } else if (m.type === 'attached'){
      chips.innerHTML = ''; (m.names || []).forEach(function(n){ var c = document.createElement('span'); c.className = 'chip'; c.textContent = '📎 ' + n; chips.appendChild(c); });
    } else if (m.type === 'seed'){
      q.value = m.text; autosize(); q.focus();
    } else if (m.type === 'start'){
      cur = null;
    } else if (m.type === 'cliBegin'){
      startCliCard(m.label);
    } else if (m.type === 'act'){
      stepLine(m.text);
    } else if (m.type === 'tool'){
      toolStart(m.label, m.cmd);
    } else if (m.type === 'toolDone'){
      toolDone(m.output, m.failed);
    } else if (m.type === 'think'){
      thinkAppend(m.text);
    } else if (m.type === 'out'){
      if (m.stream === 'err'){ stepLine('✗ ' + m.text); }
      else { outAppend(m.text, m.pre); }
    } else if (m.type === 'changes'){
      renderChanges(m.files);
    } else if (m.type === 'diffText'){
      if (activeReview && activeReview.diffBox && m.path === activeReview.curFile){ activeReview.diffBox.innerHTML = colorDiff(m.text || '(no diff)'); scroll(); }
      else appendDiffBlock(m.path, m.text);
    } else if (m.type === 'decision'){
      DECISIONS[m.path] = m.state;
    } else if (m.type === 'done'){
      sendBtn.disabled = false; stopBtn.disabled = true;
      finishTurn();
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
