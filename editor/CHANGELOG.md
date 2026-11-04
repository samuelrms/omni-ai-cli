# Changelog

All notable changes to the Omni AI CLI editor extension are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/). The
extension shares its version with the `omni-ai-cli` package.

## 1.11.0 — 2026-11-04

### Added
- Full chat experience in the sidebar and as an editor tab, matching the terminal CLI.
- All terminal slash commands in chat (`/usage`, `/doctor`, `/update`, and the rest), synced from the CLI, including user-created accounts, tools, models and skills.
- Reasoning/thinking display and collapsible Bash/command blocks.
- Renderable Mermaid diagrams for flows.
- Dedicated cards for CLI command results, distinct from a terminal look.
- File-by-file change review: sequential approve/reject with inline colored diffs, restricted to files the tools touched, with decisions persisted.
- `+` menu for attachments and voice input.
- `/remote-control` handoff to Claude Code Cloud.
- Conversation and decision history persisted across panel close/reopen.
- Input history navigation with the up/down arrows.
- First run installs the `ai` CLI if missing and opens a setup walkthrough.

### Changed
- Chat layout: user messages on the right, responses on the left, with timestamp and duration at the bottom of each message.
- Markdown rendering for headings (h1–h6), horizontal rules, ordered/unordered lists, code and clickable file links.

### Fixed
- `/doctor` runs in chat instead of hijacking a reused terminal.
- Timestamps no longer disappear when reopening the panel.
- `+` menu alignment.

## 1.10.0 — 2026-11-04

### Added
- Chat visual design overhaul with attachments and command menu.
- Auto-install of the `ai` CLI when missing, plus a first-run walkthrough.

## 1.9.0 — 2026-11-04

### Added
- Activity and thinking indicators, timestamps, and mode and tool pickers in chat.

## 1.8.0 — 2026-11-04

### Added
- Claude-style chat, open as an editor tab, keyboard shortcuts, and resume-last-conversation.

## 1.7.0 — 2026-11-03

### Added
- History sidebar view with continue.
- Reads Cursor conversations into the unified history.

## 1.6.0 — 2026-11-03

### Added
- `ai history` — unified conversations across tools (surfaced in the extension).

## 1.5.0 — 2026-11-03

### Added
- Sidebar chat view.
- Publish workflow for the extension (VS Code Marketplace and Open VSX).

## 1.4.0 — 2026-11-03

### Added
- First release of the VS Code and Cursor extension (MVP).

## 1.3.0 — 2026-11-03

### Added
- CLI: `--version` and shell completions.

## 1.2.0 — 2026-11-03

### Added
- `/doctor` runs the health check inside the chat.
- OpenRouter answers stream as they are generated.

## 1.1.0 — 2026-11-03

### Added
- `ai doctor` health check.

### Fixed
- `update` pulls the current branch from origin explicitly.

## 1.0.0 — 2026-11-03

### Added
- Omni AI CLI: route chat to Claude, Codex, Cursor, Antigravity, Grok, DeepSeek, Cerebras and OpenRouter.
- Automatic tool routing with a reason shown for each pick.
- Several API keys per tool, Claude included.
- Real usage and cost per account and the day's total in `/usage`; `/clear` and `/compact`.
- `/versions` in the chat's language, with a notice when a new version is out.
- Queue requests while one runs, and resume the queue after closing the chat.
- Offer to install missing tools on first launch.
