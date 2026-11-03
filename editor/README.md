# Omni AI CLI — VS Code & Cursor

Run the [Omni AI CLI](https://github.com/samuelrms/omni-ai-cli) from your editor. The extension
is a thin wrapper around the `ai` command: it drives it in an integrated terminal, so logins,
streaming and the chat behave exactly as in a real terminal.

> Requires the `ai` CLI installed and on your PATH (`npm i -g omni-ai-cli`,
> `brew install samuelrms/tap/omni-ai-cli`, or the install script). Works in VS Code and Cursor.

## Commands

Open the Command Palette and type **Omni AI**:

| Command | What it does |
| --- | --- |
| Omni AI: Ask | Ask a question; the router picks the tool |
| Omni AI: Explain selection | Explain the selected code (or the whole file) |
| Omni AI: Run prompt on selection | Your prompt applied to the selection |
| Omni AI: Open chat | Open the interactive chat |
| Omni AI: Doctor | Health check of the install |

Explain / Run also appear in the editor right-click menu when there is a selection.

## Sidebar chat

The **Omni AI** icon in the Activity Bar opens a chat panel. Type a question and the extension
runs `ai` for you, streaming the answer into the panel (Enter sends, Shift+Enter adds a line,
Stop cancels). It uses the same router, tool and mode settings as the commands.

## Settings

- `omni-ai.binPath` — path to `ai` (default `ai`).
- `omni-ai.tool` — `auto` (router decides) or a specific tool.
- `omni-ai.mode` — permission mode (`default`, `plan`, `edits`, `auto`, `yolo`).

## Notes

The terminal commands pass the selection through a temporary file, which assumes a POSIX shell
(macOS/Linux). The sidebar chat spawns `ai` directly (no shell), so it is cross-platform.
