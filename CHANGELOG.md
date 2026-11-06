# Changelog

What changed for people using Omni AI CLI, newest first.

This file covers up to 1.23.0. For the newest and older versions, see [GitHub Releases](https://github.com/samuelrms/omni-ai-cli/releases). `ai --version` shows your version.

## [1.23.0](https://github.com/samuelrms/omni-ai-cli/releases/tag/v1.23.0) — 2026-11-06

- Ready-made Docker image at `ghcr.io/samuelrms/omni-ai-cli`, for amd64 and arm64. Use a version tag or `latest`, with `OMNI_IMAGE` in the Compose file. Without it, Compose still builds locally.
- The image includes third-party CLIs, subject to their own terms.

## [1.22.0](https://github.com/samuelrms/omni-ai-cli/releases/tag/v1.22.0) — 2026-11-06

- New `ai serve work` command to list and remove the branches and copies left by write requests: `list`, `remove ID` and `prune --older-than N` or `--merged`.
- Nothing is deleted without `--yes`. It only reaches branches and copies created by Omni, and keeps items that look in use.

## [1.21.1](https://github.com/samuelrms/omni-ai-cli/releases/tag/v1.21.1) — 2026-11-06

- When the server's mode denies an action and the tool exits with an error, the answer is now `403 action_not_allowed`.
- The administration API lists the main logins of installed tools, with the `id` used in the login routes.
- Inside a container, the network warning now says that the published port decides who has access.

## [1.21.0](https://github.com/samuelrms/omni-ai-cli/releases/tag/v1.21.0) — 2026-11-06

- Experimental `ai serve --tool-emulation` (`SMART_AI_TOOL_EMULATION=1` in Docker): OpenAI `tools` on Claude Code and Grok CLI, with their own tools turned off. It only applies when no API provider is available, and uses more quota.

## [1.20.0](https://github.com/samuelrms/omni-ai-cli/releases/tag/v1.20.0) — 2026-11-06

- In write modes, server requests now run in parallel without touching the original folder: a branch per request with git, a copy of up to 1 GB without it.
- The location comes back in `X-Omni-Branch` or `X-Omni-Copy`. Send it again to continue the work. Merging is up to you.
- Above the limit, requests wait in a queue before getting a 429.

## [1.19.0](https://github.com/samuelrms/omni-ai-cli/releases/tag/v1.19.0) — 2026-11-06

- The server accepts OpenAI `tools` on API providers (OpenRouter, DeepSeek and Grok with a key). Calls return to your code; the server never runs them. Other models return 400.

## [1.18.0](https://github.com/samuelrms/omni-ai-cli/releases/tag/v1.18.0) — 2026-11-06

- The administration API can start and finish subscription logins for Claude Code, Codex, Cursor and Grok, with no shell access.

## [1.17.0](https://github.com/samuelrms/omni-ai-cli/releases/tag/v1.17.0) — 2026-11-06

- Optional administration API (`ai serve --enable-admin`) to change models, fallback order, provider keys and client keys on a running server. It uses its own keys (`ai serve admin-keys`).

## [1.16.0](https://github.com/samuelrms/omni-ai-cli/releases/tag/v1.16.0) — 2026-11-06

- `ai serve --install-service` runs the server as a user service, with launchd on macOS and systemd on Linux. It starts by itself and restarts after failures. `ai --update` restarts it.

## [1.15.1](https://github.com/samuelrms/omni-ai-cli/releases/tag/v1.15.1) — 2026-11-05

- The test suite can no longer write to your real home folder or use your real keys. This affects people running the tests, not everyday use.

## [1.15.0](https://github.com/samuelrms/omni-ai-cli/releases/tag/v1.15.0) — 2026-11-05

- Docker image and Compose file to run `ai serve` on your own server, with Claude Code, Codex, Cursor and Grok.
- New write modes for the server: `ai serve --mode edits|auto|yolo --workdir PATH`. The default stays read-only `plan`.

## [1.14.0](https://github.com/samuelrms/omni-ai-cli/releases/tag/v1.14.0) — 2026-11-05

- Subscription login without a browser, for SSH, containers and servers: `ai --account login` shows a link to open on another device.
