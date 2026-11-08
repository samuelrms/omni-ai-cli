#!/usr/bin/env bash
# Prints the Dockerfile hash lines for the Grok and Cursor versions set there (or given as arguments):
#   scripts/docker-hashes.sh [GROK_VERSION] [CURSOR_VERSION]
# Downloads about 400 MB. Check the versions in the vendors' release notes before trusting new hashes.
set -euo pipefail
cd "$(dirname "$0")/.."
arg() { sed -n "s/^ARG $1=//p" Dockerfile; }
grok="${1:-$(arg GROK_VERSION)}"
cursor="${2:-$(arg CURSOR_VERSION)}"
hasher() { if command -v sha256sum >/dev/null; then sha256sum; else shasum -a 256; fi; }
sum() { curl --proto '=https' --tlsv1.2 -fsSL "$1" | hasher | cut -d' ' -f1; }
echo "ARG GROK_SHA256_AMD64=$(sum "https://x.ai/cli/grok-${grok}-linux-x86_64")"
echo "ARG GROK_SHA256_ARM64=$(sum "https://x.ai/cli/grok-${grok}-linux-aarch64")"
echo "ARG CURSOR_SHA256_AMD64=$(sum "https://downloads.cursor.com/lab/${cursor}/linux/x64/agent-cli-package.tar.gz")"
echo "ARG CURSOR_SHA256_ARM64=$(sum "https://downloads.cursor.com/lab/${cursor}/linux/arm64/agent-cli-package.tar.gz")"
