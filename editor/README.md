# Omni AI CLI — VS Code & Cursor

One chat for **Claude Code, Codex, Cursor, Antigravity, Grok, DeepSeek and OpenRouter**, inside your editor. You ask, the [Omni AI CLI](https://github.com/samuelrms/omni-ai-cli) picks the tool, and if one fails or runs out of quota it moves to the next.

The extension runs on top of the `ai` command, so it uses the same logins, accounts, keys and models as your terminal. On first run it installs `ai` if missing and opens a setup guide. You can also install it yourself with `npm i -g omni-ai-cli` or `brew install samuelrms/tap/omni-ai-cli`.

## Chat

Open the **Omni AI** icon in the Activity Bar, or use the chat as an editor tab.

- Terminal slash commands in the chat (`/usage`, `/doctor`, `/update`, `/print`, `/export` and more), including your accounts, tools, models and skills.
- Thinking display, collapsible command blocks and Mermaid diagrams.
- File-by-file review of changes, with colored diffs to approve or reject.
- Attachments and voice input from the `+` menu.
- Conversations kept when you close and reopen the panel.

## Commands

Open the Command Palette and type **Omni AI**:

| Command | What it does |
| --- | --- |
| Ask | Ask a question; the router picks the tool |
| Explain selection | Explain the selected code or the whole file |
| Run prompt on selection | Apply your prompt to the selection |
| Open chat | Open the chat in the sidebar |
| Open chat in editor | Open the chat as an editor tab |
| Open chat in terminal | Open the interactive chat in a terminal |
| Doctor (health check) | Check the install, logins and keys |

Explain and Run also appear in the right-click menu when there is a selection.

## Settings

- `omni-ai.binPath`: path to `ai` (default `ai`).
- `omni-ai.tool`: `auto` (the router decides) or a specific tool.
- `omni-ai.mode`: permission mode (`default`, `plan`, `edits`, `auto`, `yolo`).

## Also in the CLI

The `ai` command can also run as an [OpenAI-compatible server](https://github.com/samuelrms/omni-ai-cli#modo-servidor), including [with Docker](https://github.com/samuelrms/omni-ai-cli#docker).

## Notes

The terminal commands pass the selection through a temporary file, which assumes a POSIX shell (macOS or Linux). The sidebar chat runs `ai` directly, so it works across platforms.

See what changed in the **Changes** tab or in [GitHub Releases](https://github.com/samuelrms/omni-ai-cli/releases).
