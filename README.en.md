# Omni AI CLI

![npm](https://img.shields.io/badge/npm-coming%20soon-1A1714)
[![License: MIT](https://img.shields.io/badge/license-MIT-C4552F.svg)](LICENSE)
![Platforms](https://img.shields.io/badge/macOS%20·%20Linux%20·%20Windows-1A1714)
![Languages](https://img.shields.io/badge/pt--BR%20·%20en%20·%20es%20·%20fr-C4552F)

> [Português](README.md) · [Español](README.es.md) · [Français](README.fr.md)

A chat terminal that brings **Claude Code, Codex, Antigravity and OpenRouter** together in
one place. You type your question, `ai` decides who answers, and if a tool fails or
runs out of quota, it moves on to the next one without you doing anything.

Simple questions go to the cheaper tools. Heavy tasks go to Claude, with automatic
switching between several accounts when one hits its limit. History is saved, the
interface speaks Portuguese, English, Spanish and French, and models and accounts are
configured from inside the chat itself.

---

## Contents

- [How it works](#how-it-works)
- [Installation](#installation)
- [How to use](#how-to-use)
- [Chat commands](#chat-commands)
- [Models](#models)
- [Claude accounts](#claude-accounts)
- [API keys](#api-keys)
- [Modes](#modes)
- [Skills and MCP](#skills-and-mcp)
- [Direct use, no chat](#direct-use-no-chat)
- [Where files live](#where-files-live)
- [Project structure](#project-structure)
- [Tests](#tests)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)
- [Privacy](#privacy)
- [License](#license)

---

## How it works

From the typed line to the saved answer. The chat (`terminal.py`) does not call any AI
directly: it launches the router (`smart_ai.py`) as a subprocess and reads the events it
emits, line by line, to show who is answering and what they are doing.

```mermaid
flowchart TD
    A["You type and press Enter"] --> B{"Starts with / ?"}
    B -- yes --> C["Internal command<br/>/model · /account · /key · /lang · /resume"]
    C --> C1["config_tool.py writes to<br/>models.json, profiles.json or keys/"]
    B -- no --> D["Builds the context: latest messages<br/>of the session up to 48 thousand characters + language"]

    subgraph CHAT ["terminal.py — chat"]
        D --> E["Thread worker launches the router<br/>in its own process group"]
        E -. "JSON events line by line" .-> F["Shows provider, commands and answer"]
        X["Ctrl-C"] -. "SIGTERM to the group;<br/>after 5 s, SIGKILL" .-> E
    end

    subgraph ROUTER ["smart_ai.py — router"]
        E --> G["Loads models:<br/>env > models.json > default"]
        G --> H{"Tool pinned<br/>with /tool?"}
        H -- yes --> H1["Only the chosen tool"]
        H -- auto --> I{"Classifies: keywords<br/>and length up to 200 characters"}
        I -- simple --> J["OpenRouter: free, or paid<br/>if authorized · personal data masked"]
        J -- "no key or failed" --> K["Codex: subscription,<br/>then API key"]
        K -- failed --> L["Antigravity: subscription,<br/>then API key"]
        L -- failed --> M
        I -- complex --> M["Claude: next active account<br/>by priority"]
        M --> N{"Rate limit?"}
        N -- yes --> O["Account on 60 min cooldown"]
        O --> P{"Another account<br/>available?"}
        P -- yes --> M
        P -- no --> PK{"Anthropic<br/>API key?"}
        PK -- yes --> PK1["Claude with ANTHROPIC_API_KEY"]
        PK1 -- ok --> R
        PK1 -- failed --> Q
        PK -- no --> Q["Last resort:<br/>Codex, then Antigravity"]
    end

    N -- no --> R["Answer"]
    J -- ok --> R
    K -- ok --> R
    L -- ok --> R
    Q --> R
    H1 --> R
    R --> F
    F --> S[("Session saved to disk<br/>with atomic write")]
    S --> A
```

Three details the diagram makes explicit:

- **Each Claude account has its own login folder.** Extra accounts run with
  `CLAUDE_CONFIG_DIR` pointing to `~/.claude-accounts/<nome>`. The main account is
  never logged out, and no credential is copied from one place to another. Copying
  `.credentials.json` does not work reliably on macOS, where the login lives in the
  Keychain.
- **Ctrl-C kills the whole tree.** The router runs in its own process group, so
  canceling also ends the `claude`, `codex` or `agy` running underneath, instead of
  leaving orphan processes burning quota.
- **Only the answer goes into the history.** Status and diagnostic lines appear on
  screen, but are not sent as context with the next question. The models' internal
  reasoning is also discarded.

---

## Installation

One command installs everything and, run again, updates it. Your accounts, keys and models live in `~/.config/smart-ai` and are left untouched.

**macOS and Linux**:

```bash
curl -fsSL https://raw.githubusercontent.com/samuelrms/omni-ai-cli/main/get.sh | bash
```

**Windows** (PowerShell):

```powershell
irm https://raw.githubusercontent.com/samuelrms/omni-ai-cli/main/get.ps1 | iex
```

**Coming soon to npm, yarn, pnpm and bun.** The `omni-ai-cli` package is ready and ships as soon as publishing is unlocked:

```bash
npm install -g omni-ai-cli
yarn global add omni-ai-cli
pnpm add -g omni-ai-cli
bun add -g omni-ai-cli
```

For now, use the macOS/Linux or Windows command above.

To update later: `ai --update` (or `/update` in the chat).

From source, for development:

```bash
git clone https://github.com/samuelrms/omni-ai-cli ~/dev/omni-ai-cli
cd ~/dev/omni-ai-cli && ./install.sh        # Windows: powershell -File install.ps1
```

The installer:

1. installs **Claude Code** through the official installer if it is not on the machine yet;
2. opens the main account login if there isn't one yet;
3. creates the `ai` command in `~/.local/bin`;
4. shows the configured Claude accounts.

Requirements: nothing beyond `curl` (macOS/Linux) or PowerShell (Windows). If Python 3.11+ is missing, the installer downloads a Python 3.12 just for Omni AI, without sudo and without touching the system Python.
Codex (`codex`) and Antigravity (`agy`) are optional and join the fallback queue when they are installed.

If `~/.local/bin` is not in your `PATH`, add this to `~/.zshrc`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

On Windows, the installer creates `ai.cmd` in `%USERPROFILE%\.local\bin` and adds that folder to `PATH`; open a new terminal afterwards.

---

## How to use

```bash
ai
```

The first time, it asks for the language. After that, just type and press Enter.

| Key | Action |
| --- | --- |
| Enter | Sends |
| ↑ / ↓ | Previous lines |
| Ctrl-C | During an answer: cancels. At the prompt: saves and exits |
| Ctrl-D | Saves and exits |

On exit, it shows the command to return to the same conversation:

```text
Sessão salva. Retome com: ai --resume 1f3a9c2b7d40
```

---

## Chat commands

| Command | What it does |
| --- | --- |
| `/tool auto\|claude\|codex\|agy\|openrouter` | Pins a tool (`auto` decides on its own) |
| `/model` | Lists the models of each tool |
| `/model codex gpt-6-astra` | Changes a model |
| `/model codex default` | Goes back to the default |
| `/account` | Lists the Claude accounts and whether they are logged in |
| `/account add pessoal` | Creates an account and opens the Claude login right there |
| `/account login NOME` | Redoes the login of an account |
| `/account sync NAME` | Re-shares skills, plugins and MCPs from the main account |
| `/account disable\|enable\|remove NOME` | Disables, re-enables or removes |
| `/key` | Lists the configured API keys (masked) |
| `/key claude\|codex\|agy\|openrouter` | Adds a key, without showing what you type |
| `/key remove NOME` | Removes the key |
| `/status` · `/reset` | Account status · clears cooldowns |
| `/lang pt\|en\|es\|fr` | Changes the language |
| `/mode default\|plan\|edits\|auto\|yolo` | Permission mode, the same on all three tools |
| `/sync` | Shares MCPs and skills across Claude, Codex and Antigravity |
| `/skills` | Lists project and user skills and commands from Claude, Codex and Antigravity |
| `/name args` | Runs the skill or command (e.g. `/pr-review 237`). Tab completes |
| `/history` · `/sent` · `/trace` | Conversation · what was sent · run events |
| `/sessions` · `/resume ID` · `/new` | Saved sessions |
| `/parallel codex: tarefa \| codex: tarefa` | 2 to 4 analyses in parallel, read-only |
| `/help` · `/exit` | Help · exit |

The configuration commands also work outside the chat: `ai --model`,
`ai --account add pessoal`, `ai --key claude`, `ai --status`, `ai --reset`.

---

## Models

| Key | Default | Accepted values |
| --- | --- | --- |
| `claude` | `opus` (the most capable) | `opus`, `sonnet`, `haiku` or full ID |
| `codex` | `gpt-6-astra` | Codex model ID |
| `codex_effort` | `high` | `minimal`, `low`, `medium`, `high`, `xhigh` |
| `agy` | Antigravity default | Antigravity model ID |
| `openrouter` | automatic (free) | model ID; paid only with `openrouter_paid on` |
| `openrouter_paid` | `off` | `on` authorizes paid OpenRouter models |

The priority order is: environment variable, then what was saved with `/model` (in
`~/.config/smart-ai/models.json`), then the default. The variables are
`SMART_AI_CLAUDE_MODEL`, `SMART_AI_CODEX_MODEL`, `SMART_AI_CODEX_EFFORT`,
`SMART_AI_AGY_MODEL` and `SMART_AI_OPENROUTER_MODEL`. When one of them is set,
`/model` warns that it takes precedence.

---

## Claude accounts

```text
ai [auto] › /account add trabalho
Abrindo login do Claude para a conta "trabalho" (pasta isolada ~/.claude-accounts/trabalho).
Sua conta principal não é afetada.
✓ Conta "trabalho" adicionada com prioridade 3.
```

Accounts are tried in priority order. The one that hits its limit goes on cooldown
(60 minutes by default, adjustable in `profiles.json`) and the next one takes over. The
`primary` account uses the normal Claude login (`claude auth login`).

Skills, plugins, agents, `CLAUDE.md`, `settings.json` and the main account's MCP servers
are shared with every extra account at login. Only the login stays separate. If you
install a new skill or MCP later, run `/account sync NAME`.


---

## API keys

Besides subscriptions, each tool accepts an API key. **The subscription comes
first**; the key only comes in when the subscription fails or hits its limit, because a
key is billed per use. OpenRouter works with a key only.

| Tool | Command | Variable the CLI receives | Where to create |
| --- | --- | --- | --- |
| Claude | `/key claude` | `ANTHROPIC_API_KEY` | console.anthropic.com/settings/keys |
| Codex | `/key codex` | `CODEX_API_KEY` | platform.openai.com/api-keys |
| Antigravity | `/key agy` | `GEMINI_API_KEY` | aistudio.google.com/apikey |
| OpenRouter | `/key openrouter` | `OPENROUTER_API_KEY` | openrouter.ai/keys |

```text
ai [auto] › /key claude
Chave de claude (console.anthropic.com/settings/keys). Cole e tecle Enter; ela não aparece na tela.
>
✓ Chave de claude salva em ~/.config/smart-ai/keys/claude (permissão 600).
```

Keys are stored in `~/.config/smart-ai/keys/`, one per file, with permission 600, and
are passed only to the tool's process, never to the history or the log. If the variable
is already set in the environment, it takes precedence. `/key` lists what is
configured, showing only the start and end of each key.

On Claude, the key works as an extra account after all the subscriptions: if it also
hits its limit, it goes on cooldown like the others.

> **Never paste a key as a shell command.** It gets recorded in the zsh history.
> If that happens, revoke the key on the tool's website and delete the line from
> `~/.zsh_history`.

On OpenRouter the default is free models only: the client picks up to three `:free`
variants from the catalog and uses `openrouter/free` as a last alternative. Paid models
stay blocked until you authorize them:

```text
/model openrouter_paid on            # authorizes; warns it will be billed to your account
/model openrouter anthropic/claude-opus-5.5
/model openrouter_paid off           # blocks again and goes back to free
```

With authorization on, the chosen model goes first and free models stay as backup. With
it off, a zero price cap is sent on every call, so no paid model answers.

---

## Modes

The mode applies to Claude, Codex and Antigravity at once and shows in the prompt
(`ai [auto · plan] ›`). Each session keeps its own.

| Mode | What it does | Claude | Codex | Antigravity |
| --- | --- | --- | --- | --- |
| `default` | Each tool's default | — | — | — |
| `plan` | Reads and plans only, changes nothing | `plan` | `--sandbox read-only` | `--mode plan` |
| `edits` | Accepts file edits | `acceptEdits` | `--sandbox workspace-write` | `--mode accept-edits` |
| `auto` | Acts on its own, with the tool's safeguards | `auto` | `workspace-write`, no approval | `--mode auto` |
| `yolo` | No permissions or sandbox | `bypassPermissions` | `--dangerously-bypass-approvals-and-sandbox` | `--dangerously-skip-permissions` |

`/mode yolo` asks for confirmation, because the AI can then delete files and run any
command. Outside the chat: `ai --mode plan "question"`. `/parallel` always runs in `plan`.

---

## Skills and MCP

`ai` calls the CLIs themselves, so each one uses the skills and MCPs it already has.
`/sync` gathers everything and installs in each tool what only exists in the others:

```text
ai [auto] › /sync
Ferramentas encontradas: claude, codex, agy
  MCP   github                   codex → claude
  MCP   obsidian                 claude → codex
  skill humanizer                claude → codex  (~/.agents/skills/humanizer)
Aplicar? [s/N]
```

- MCPs are added through each CLI's own `mcp add` command, not by editing files by hand.
- The same server under different names is recognized by package or URL and not duplicated.
- MCPs that ship inside an app (like Codex.app's) stay there.
- Skills are shared by link: `~/.claude/skills`, `~/.agents/skills` (Codex) and
  `~/.gemini/antigravity/skills`. Editing one applies to all three.
- Extra Claude accounts are refreshed too.
- MCP environment variables, which may hold tokens, are copied into the other tool's
  config. `/sync` warns before doing it.
- `/sync --dry-run` only shows what it would do.

**Project skills and commands:** the chat reads `.claude/commands`, `.claude/skills`, `.agents/skills`
and `.agent/workflows` from the open project, plus the user folders. `/pr-review 237` goes to
Claude exactly as typed, and Claude runs the command natively. A skill that only exists in
Codex or Antigravity goes to that tool, with the file's instructions in the request.

Since the chat doesn't forward approval prompts, an MCP tool only runs if it is already
allowed in the CLI's permissions or the mode is `auto` or `yolo`. OpenRouter has no skills
or MCP.

---

## Direct use, no chat

```bash
ai "o que é um design pattern?"        # automatic routing
ai --claude "refatorar o módulo X"     # forces Claude
ai --codex "explicar REST"             # forces Codex
ai --agy "pesquisar React hooks"       # forces Antigravity
ai --openrouter "explicar Docker"      # forces OpenRouter
ai --classify "prompt"                 # shows how it would be classified
ai --log                               # latest calls
```

---

## Where files live

| Path | Contents |
| --- | --- |
| `~/.config/smart-ai/profiles.json` | Claude accounts and routing rules |
| `~/.config/smart-ai/models.json` | Models chosen with `/model` |
| `~/.config/smart-ai/lang` | Chat language |
| `~/.config/smart-ai/keys/<ferramenta>` | API keys, one per file (permission 600) |
| `~/.claude-accounts/<nome>/` | Isolated login of each extra Claude account |
| `~/.local/share/smart-ai/sessions/` | Chat sessions |
| `~/.cache/smart-ai/state.json` | Active cooldowns |
| `~/.cache/smart-ai/usage.log` | Usage log, metadata only |

---

## Project structure

```text
omni-ai-cli/
├── bin/ai.js                 # npm launcher: finds or installs Python and runs smart_ai.py
├── package.json              # omni-ai-cli npm package
├── smart_ai.py               # ai command: classifies, routes and runs each tool
├── platform_utils.py         # differences between macOS, Linux and Windows
├── smart-ai.sh / ai.cmd      # compatibility launchers (Unix / Windows)
├── terminal.py               # chat: sessions, events, Ctrl-C, languages
├── config_tool.py            # models, keys and Claude accounts (/model, /key, /account)
├── skills.py                 # project and user skills and commands (/skills, /name)
├── sync_tool.py              # /sync: MCPs and skills across the three tools
├── openrouter_client.py      # OpenRouter client: free, paid only if authorized
├── pii_masker.py             # masks and restores personal data
├── profiles.json             # starting template; yours lives in ~/.config/smart-ai
├── get.sh / get.ps1          # one-command install (downloads and runs install)
├── install.sh / install.ps1  # installs Claude Code and the ai command (Unix / Windows)
├── README.md / .en / .es / .fr
├── postman/                  # HTTP simulation of OpenRouter
└── tests/                    # fake executables, no quota used
```

The router knows nothing about chat: it only runs a question and, with
`SMART_AI_EVENTS=1`, emits JSON events. The chat knows nothing about credentials:
accounts and models go through `config_tool.py`.

---

## Tests

```bash
python3 -m unittest discover -s tests -v
bash -n install.sh
```

**27 tests**: session persistence and resume, context limit, file permissions, session
open in two terminals, event reading, process tree cancellation, real parallelism,
read-only sandbox, personal data masking, OpenRouter → Codex fallback, model
validation, key storage and the account lifecycle. All the
executables are fake: **no test authenticates or uses up a subscription.**

To test OpenRouter's HTTP against the Postman simulation:

```bash
postman simulation run postman/simulations/openrouter.sim.yaml --port 4901 --no-history --output ndjson
SMART_AI_SIM_URL=http://127.0.0.1:4901/openrouter python3 -m unittest discover -s tests -v
```

---

## Troubleshooting

| Problem | Solution |
| --- | --- |
| `OPENROUTER_API_KEY não configurada` | `/key openrouter` in the chat or `ai --key openrouter`. |
| `Arquivo de credenciais não encontrado` or `Conta 'X' sem login` | `/account login X`. |
| `You've hit your session limit` | Account quota. `ai` moves to the next one; add another with `/account add` or a key with `/key claude`. |
| `Individual quota reached` (Antigravity) | Antigravity quota; it is skipped until it renews. |
| `Not inside a trusted directory` (Codex) | Old install. Run the installer again. |
| `Claude Code não instalado` | `./install.sh` (or `install.ps1` on Windows) installs it with the official installer. |
| Windows: skill links fail | Turn on Developer Mode or run as administrator; otherwise `ai` uses folder junctions. |
| `/model` changes nothing | A `SMART_AI_*_MODEL` variable is set and takes precedence. |
| `/key` changes nothing | The tool's variable (`ANTHROPIC_API_KEY` etc.) is set and takes precedence. |
| `Sessão já aberta em outro terminal` | Close the other terminal or use `/new`. |
| `ai: command not found` | Add `~/.local/bin` to the `PATH`. |

---

## Known limitations

- The chat uses the CLIs' non-interactive modes. Native approval prompts (for
  example, "can I edit this file?") do not appear; in those cases, use the native CLI.
- `/parallel` only analyzes: Codex in `--sandbox read-only` or OpenRouter, which does not
  access the files. The agents do not edit the project at the same time.
- The simple/complex classification is by keywords and length, not by a model.
- Switching models does not renew the account's quota.
- OpenRouter has no streaming in this version: the answer appears whole at the end.
- On Windows the code uses the native equivalents (process groups and `taskkill`, `msvcrt`, folder junctions), but it has only been tested for real on macOS.

---

## Privacy

- Sessions, API keys and logs stay only on your machine, with permissions restricted to your user.
- The usage log records tool, account and status, **never the text of the question**.
- Before going to OpenRouter, emails, phone numbers, documents and other personal data
  are swapped for placeholders and restored in the answer. Masking is pattern-based and
  does not guarantee full anonymization.
- Claude, Codex and Antigravity receive the original text, under each service's terms.

---

## License

[MIT](LICENSE).
