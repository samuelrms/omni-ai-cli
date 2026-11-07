#!/usr/bin/env bash
# Omni AI CLI - one-command install / update for macOS and Linux:
#   curl -fsSL https://raw.githubusercontent.com/samuelrms/omni-ai-cli/main/get.sh | bash
set -euo pipefail

REPO="${SMART_AI_REPO:-https://github.com/samuelrms/omni-ai-cli}"
BRANCH="${SMART_AI_BRANCH:-main}"
DEST="${SMART_AI_DIR:-$HOME/.local/share/omni-ai-cli}"

echo "Omni AI CLI → $DEST"
# The folder is replaced below: never one that holds something else
if [[ -e "$DEST" && ! -f "$DEST/omni_ai/__init__.py" && -n "$(ls -A "$DEST" 2>/dev/null)" ]]; then
    echo "✗ $DEST já existe e não é uma instalação do Omni AI. Nada foi apagado." >&2
    echo "  Escolha outra pasta com SMART_AI_DIR=... ou remova essa você mesmo." >&2
    exit 1
fi
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
    curl --proto '=https' --tlsv1.2 -fsSL "$REPO/archive/refs/heads/$BRANCH.tar.gz" | tar -xz -C "$tmp"
    rm -rf "$DEST"
    mkdir -p "$(dirname "$DEST")"
    mv "$tmp"/omni-ai-cli-"$BRANCH" "$DEST"
    rm -rf "$tmp"
fi

# Reattach stdin to the terminal so the installer can open the Claude login
if [[ "${SMART_AI_NONINTERACTIVE:-0}" != "1" && -r /dev/tty ]]; then
    bash "$DEST/scripts/install.sh" </dev/tty
else
    bash "$DEST/scripts/install.sh"
fi
