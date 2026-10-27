"""Every tool Omni can route to, in one place.

Adding a tool: one entry here plus its run_<name> function in cli.py. Lists in the chat, help, /model, /key,
/status, /usage, onboarding and the colors on screen come from this table.
"""


class Tool:
    def __init__(self, name, label, rgb, flag, bin=None, install=None, install_windows=None, login=None,
                 model=('', ''), key=None, parallel=False, price='paid', in_usage=True):
        self.name, self.label, self.rgb, self.flag = name, label, rgb, flag
        self.bin = bin                      # executable, for tools that are a CLI; None = API only
        self.install, self.install_windows, self.login = install, install_windows, login
        self.model_default, self.model_help = model  # /model <name>; env override SMART_AI_<NAME>_MODEL
        self.key = key                      # (env var the tool reads, expected prefix or None, where to create one)
        self.parallel = parallel            # can take a read-only /parallel subtask
        self.price = price                  # 'free' or 'paid', as shown in onboarding
        self.in_usage = in_usage            # gets its own block in /usage (Claude has its own layout)

    def install_command(self, windows=False):
        """The command that installs it here, or None when it has to be done by hand (a download page)."""
        command = self.install_windows if windows and self.install_windows else self.install
        if not command or command.startswith('http') or (windows and command.startswith('curl ')):
            return None
        return command

    @property
    def model_env(self):
        return f'SMART_AI_{self.name.upper()}_MODEL'


TOOLS = {tool.name: tool for tool in (
    Tool('claude', 'Claude Code', (217, 119, 87), 'c', bin='claude',
         install='curl -fsSL https://claude.ai/install.sh | bash', install_windows='irm https://claude.ai/install.ps1 | iex',
         login='claude auth login', model=('opus', 'Claude Code: opus, sonnet, haiku ou ID completo'),
         key=('ANTHROPIC_API_KEY', 'sk-ant-', 'console.anthropic.com/settings/keys'), in_usage=False),
    Tool('codex', 'Codex CLI', (16, 185, 129), 'x', bin='codex', install='npm install -g @openai/codex', login='codex login',
         model=('gpt-6-astra', 'Codex CLI: ID do modelo'), key=('CODEX_API_KEY', 'sk-', 'platform.openai.com/api-keys'),
         parallel=True),
    Tool('cursor', 'Cursor CLI', (200, 200, 200), 'u', bin='cursor-agent', install='curl https://cursor.com/install -fsS | bash',
         login='cursor-agent login', model=('', 'Cursor CLI: ID do modelo; vazio = padrão da conta'),
         key=('CURSOR_API_KEY', None, 'cursor.com/dashboard → Integrations → API Keys')),
    Tool('agy', 'Antigravity', (99, 102, 241), 'a', bin='agy', install='https://antigravity.google  (CLI: agy)',
         login='agy   (Google sign-in on first run)', model=('', 'Antigravity: vazio = padrão do agy'),
         key=('GEMINI_API_KEY', None, 'aistudio.google.com/apikey'), price='free'),
    Tool('openrouter', 'OpenRouter', (234, 179, 8), 'o',
         model=('', 'OpenRouter: ID do modelo; pago exige openrouter_paid on'),
         key=('OPENROUTER_API_KEY', 'sk-or-', 'openrouter.ai/keys'), parallel=True, price='free', in_usage=False),
    Tool('grok', 'Grok Build', (59, 130, 246), 'g', bin='grok', install='curl -fsSL https://x.ai/cli/install.sh | bash',
         install_windows='irm https://x.ai/cli/install.ps1 | iex', login='grok login',
         model=('', 'Grok Build/xAI: ID do modelo; vazio = padrão do grok'), key=('XAI_API_KEY', 'xai-', 'console.x.ai'),
         parallel=True),
    Tool('deepseek', 'DeepSeek', (77, 107, 254), 'd',
         model=('', 'DeepSeek: deepseek-chat ou deepseek-reasoner; vazio = deepseek-chat'),
         key=('DEEPSEEK_API_KEY', 'sk-', 'platform.deepseek.com/api_keys'), parallel=True),
)}

# Keys that never answer a request: they only label prompts (routing) and check the queue
CLASSIFIER_KEYS = {
    'groq': ('GROQ_API_KEY', 'gsk_', 'console.groq.com/keys'),
    'cerebras': ('CEREBRAS_API_KEY', 'csk-', 'cloud.cerebras.ai'),
}

CLIS = [name for name, tool in TOOLS.items() if tool.bin]
PARALLEL = [name for name, tool in TOOLS.items() if tool.parallel]
KEY_NAMES = list(TOOLS) + list(CLASSIFIER_KEYS)


def keys():
    """provider: (env var, prefix, url) for every API key /key accepts."""
    return {**{name: tool.key for name, tool in TOOLS.items() if tool.key}, **CLASSIFIER_KEYS}


def joined(names, separator='|'):
    return separator.join(names)
