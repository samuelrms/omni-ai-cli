#!/usr/bin/env bash
#
# Script de instalação do Omni AI CLI
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

echo -e "${BOLD}${CYAN}"
echo "  ╔═══════════════════════════════════════╗"
echo "  ║     🧠 Omni AI CLI - Instalação      ║"
echo "  ╚═══════════════════════════════════════╝"
echo -e "${NC}"

# 1. Verificar dependências
echo -e "${BOLD}Verificando dependências...${NC}"

check_dep() {
    local name="$1"
    local cmd="$2"
    if command -v "$cmd" &>/dev/null; then
        local ver
        ver=$($cmd --version 2>/dev/null | head -1)
        echo -e "  ${GREEN}✓${NC} $name: $ver"
        return 0
    else
        echo -e "  ${RED}✗${NC} $name: não encontrado"
        return 1
    fi
}

# Python 3.11+: use the system one, or fetch a private 3.12 with uv (no sudo)
PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null && "$candidate" -c "import sys; sys.exit(sys.version_info < (3, 11))" 2>/dev/null; then
        PYTHON="$(command -v "$candidate")"; break
    fi
done
if [[ -z "$PYTHON" ]]; then
    echo -e "  ${YELLOW}⚠${NC} Python 3.11+ não encontrado. Instalando Python 3.12 só para o Omni AI..."
    DATA="$HOME/.local/share/smart-ai"
    export UV_INSTALL_DIR="$DATA/uv" UV_NO_MODIFY_PATH=1 UV_PYTHON_INSTALL_DIR="$DATA/python"
    [[ -x "$UV_INSTALL_DIR/uv" ]] || curl -LsSf https://astral.sh/uv/install.sh | sh
    "$UV_INSTALL_DIR/uv" python install --no-bin 3.12 || "$UV_INSTALL_DIR/uv" python install 3.12
    PYTHON="$("$UV_INSTALL_DIR/uv" python find 3.12)"
fi
if [[ -z "$PYTHON" ]] || ! "$PYTHON" -c "import sys; sys.exit(sys.version_info < (3, 11))"; then
    echo -e "  ${RED}✗ Não consegui um Python 3.11+.${NC}"; exit 1
fi
echo -e "  ${GREEN}✓${NC} Python: $("$PYTHON" --version) ($PYTHON)"
mkdir -p "$HOME/.config/smart-ai"
printf '%s' "$PYTHON" > "$HOME/.config/smart-ai/python-path"
install_claude() {
    echo -e "    ${CYAN}→ Instalando Claude Code (instalador oficial)...${NC}"
    if ! command -v curl &>/dev/null; then
        echo -e "    ${RED}✗ curl não encontrado. Instale manualmente: https://docs.claude.com/claude-code${NC}"
        return 1
    fi
    curl -fsSL https://claude.ai/install.sh | bash || return 1
    export PATH="$HOME/.local/bin:$PATH"
    check_dep "Claude Code" "claude"
}

if [[ "${SMART_AI_SKIP_CLAUDE_INSTALL:-0}" != "1" ]]; then
    check_dep "Claude Code" "claude" || install_claude || {
        echo -e "    ${RED}✗ Claude Code é necessário e não pôde ser instalado.${NC}"
        exit 1
    }
fi
check_dep "Codex CLI" "codex" || echo -e "    ${YELLOW}ℹ Codex é opcional (roteamento inteligente)${NC}"
check_dep "Antigravity" "agy" || echo -e "    ${YELLOW}ℹ AGY é opcional (roteamento inteligente)${NC}"

echo ""

# 2. Criar diretórios
echo -e "${BOLD}Criando diretórios...${NC}"
mkdir -p "$HOME/.cache/smart-ai"
mkdir -p "$HOME/.local/bin"
echo -e "  ${GREEN}✓${NC} ~/.cache/smart-ai/"
echo -e "  ${GREEN}✓${NC} ~/.local/bin/"

# 3. Inicializar state file
if [[ ! -f "$HOME/.cache/smart-ai/state.json" ]]; then
    echo '{}' > "$HOME/.cache/smart-ai/state.json"
    echo -e "  ${GREEN}✓${NC} State file criado"
fi

# 4. Criar symlink
echo ""
echo -e "${BOLD}Instalando comando 'ai'...${NC}"

LINK_TARGET="$HOME/.local/bin/ai"

if [[ -L "$LINK_TARGET" || -f "$LINK_TARGET" ]]; then
    echo -e "  ${YELLOW}⚠${NC} Removendo link anterior..."
    rm -f "$LINK_TARGET"
fi

# Small launcher instead of a symlink, so it always uses the Python chosen above
cat > "$LINK_TARGET" <<LAUNCHER
#!/usr/bin/env sh
exec "$PYTHON" "$SCRIPT_DIR/smart_ai.py" "\$@"
LAUNCHER
chmod +x "$LINK_TARGET"
echo -e "  ${GREEN}✓${NC} Comando criado: $LINK_TARGET → $SCRIPT_DIR/smart_ai.py"

# 5. Verificar PATH
if echo "$PATH" | grep -q "$HOME/.local/bin"; then
    echo -e "  ${GREEN}✓${NC} ~/.local/bin já está no PATH"
else
    echo -e "  ${YELLOW}⚠${NC} Adicione ao seu ~/.zshrc:"
    echo -e "     ${CYAN}export PATH=\"\$HOME/.local/bin:\$PATH\"${NC}"
fi

# 6. Login e contas Claude
echo ""
echo -e "${BOLD}Verificando contas Claude...${NC}"
if claude auth status &>/dev/null; then
    echo -e "  ${GREEN}✓${NC} Conta principal logada"
elif [[ -t 0 ]]; then
    echo -e "  ${YELLOW}⚠${NC} Conta principal sem login. Abrindo login..."
    claude auth login || echo -e "  ${YELLOW}⚠${NC} Login pendente: rode ${CYAN}claude auth login${NC}"
else
    echo -e "  ${YELLOW}⚠${NC} Conta principal sem login: rode ${CYAN}claude auth login${NC}"
fi
"$PYTHON" "$SCRIPT_DIR/config_tool.py" account list | sed 's/^/  /'

echo ""
echo -e "${GREEN}${BOLD}✓ Instalação concluída!${NC}"
echo ""
echo -e "  Comece com:"
echo -e "    ${CYAN}ai${NC}                    Abrir o chat (/help dentro dele)"
echo -e "    ${CYAN}ai --status${NC}           Ver status"
echo -e "    ${CYAN}ai --help${NC}             Ver todas as opções"
echo ""
