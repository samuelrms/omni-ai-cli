// Omni AI CLI — VS Code / Cursor extension.
// Thin wrapper around the `ai` CLI: it never reimplements routing, it just drives the command
// in an integrated terminal so logins, streaming and the chat work exactly as in a real terminal.
const vscode = require('vscode');
const os = require('os');
const fs = require('fs');
const path = require('path');

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

function activate(context) {
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
