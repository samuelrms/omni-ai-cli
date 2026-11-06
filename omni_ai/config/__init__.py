"""Model and Claude account settings for Omni AI.

  python -m omni_ai.config env                     shell assignments (SMART_AI_* model variables)
  python -m omni_ai.config model [KEY [VALUE|default]]
  python -m omni_ai.config account list|add NAME|login NAME|remove NAME|enable NAME|disable NAME
  python -m omni_ai.config account list|add|login|remove|enable|disable codex|agy|grok NAME
  python -m omni_ai.config key [list|set PROVIDER|remove PROVIDER|get PROVIDER]

Split into submodules: store (paths + JSON core), models, profiles, accounts, keys.
Names are re-exported here so ``omni_ai.config.<name>`` keeps working.
"""
from . import store, models, accounts, keys

from .store import (
    ROOT, CONFIG_DIR, MODELS_FILE, ACCOUNTS_DIR, TEMPLATE_PROFILES, NAME,
    profiles_path, read_json, update_json, write_json, _file_lock,
)
from .models import MODELS, EFFORTS, model_values, validate_model, cmd_model, cmd_env
from .profiles import profiles, ordered, enabled_profiles, cmd_profiles
from .accounts import (
    logged_in, SHARED, share_claude_setup, login, cmd_account, MAIN_LOGINS, share_tool_setup,
    TOOL_HOMES, TOOL_ACCOUNTS_DIR, tool_accounts, tool_env, tool_logged_in,
    share_tool_setup, tool_login, cmd_tool_account,
)
from .keys import (
    KEYS, KEYS_DIR, LEGACY_OPENROUTER, key_path, extra_key_path,
    stored_keys, stored_key, ask_key, save_key, next_key_name, cmd_key,
)


def main(argv):
    command, args = (argv[0], argv[1:]) if argv else ('model', [])
    if command == 'env':
        cmd_env()
    elif command == 'model':
        cmd_model(args)
    elif command == 'account':
        cmd_account(args)
    elif command == 'key':
        cmd_key(args)
    elif command == 'profiles':
        cmd_profiles()
    else:
        raise SystemExit(__doc__)
