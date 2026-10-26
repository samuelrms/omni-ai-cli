#!/usr/bin/env bash
# Smart AI CLI - one-command install / update for macOS and Linux:
#   curl -fsSL https://raw.githubusercontent.com/samuelrms/smart-ai-cli/main/get.sh | bash
set -euo pipefail

REPO="${SMART_AI_REPO:-https://github.com/samuelrms/smart-ai-cli}"
BRANCH="${SMART_AI_BRANCH:-main}"
DEST="${SMART_AI_DIR:-$HOME/.local/share/smart-ai-cli}"

echo "Smart AI CLI → $DEST"
if command -v git >/dev/null 2>&1; then
    if [[ -d "$DEST/.git" ]]; then
        echo "Atualizando..."
        git -C "$DEST" fetch --depth 1 origin "$BRANCH" -q
        git -C "$DEST" reset --hard "origin/$BRANCH" -q
    else
        rm -rf "$DEST"
        git clone --depth 1 --branch "$BRANCH" -q "$REPO" "$DEST"
    fi
else
    # No git: download the branch tarball instead
    tmp=$(mktemp -d)
    curl -fsSL "$REPO/archive/refs/heads/$BRANCH.tar.gz" | tar -xz -C "$tmp"
    rm -rf "$DEST"
    mkdir -p "$(dirname "$DEST")"
    mv "$tmp"/smart-ai-cli-"$BRANCH" "$DEST"
    rm -rf "$tmp"
fi

# Reattach stdin to the terminal so the installer can open the Claude login
if [[ "${SMART_AI_NONINTERACTIVE:-0}" != "1" && -r /dev/tty ]]; then
    bash "$DEST/install.sh" </dev/tty
else
    bash "$DEST/install.sh"
fi
