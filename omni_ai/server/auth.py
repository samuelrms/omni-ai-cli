"""Server API keys: only their hashes are stored; the value is shown once, when it is created."""
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import time

from .. import config as config_tool

ENV_KEY = 'SMART_AI_SERVER_KEY'
ENV_KEY_FILE = 'SMART_AI_SERVER_KEY_FILE'  # for Docker secrets
ENV_ADMIN_KEY = 'SMART_AI_ADMIN_KEY'
ENV_ADMIN_KEY_FILE = 'SMART_AI_ADMIN_KEY_FILE'
SECRET_VARS = (ENV_KEY, ENV_KEY_FILE, ENV_ADMIN_KEY, ENV_ADMIN_KEY_FILE)  # tools have no business reading these


def keys_path():
    return config_tool.CONFIG_DIR / 'server_keys.json'


def admin_keys_path():
    return config_tool.CONFIG_DIR / 'admin_keys.json'


def _hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def stored(path=None):
    return config_tool.read_json(path or keys_path(), {})


def add(name, path=None, prefix='omni-'):
    """Create a key and return its value. A repeated name raises ValueError instead of replacing it."""
    if not config_tool.NAME.fullmatch(name):
        raise ValueError('Nome inválido: use letras minúsculas, números, - e _')
    value = prefix + secrets.token_urlsafe(32)

    def insert(data):
        if name in data:
            raise ValueError(f"A chave '{name}' já existe")
        data[name] = {'hash': _hash(value), 'created': int(time.time())}

    config_tool.update_json(path or keys_path(), insert)
    return value


def remove(name, path=None):
    found = []

    def drop(data):
        found.append(data.pop(name, None) is not None)

    config_tool.update_json(path or keys_path(), drop)
    return found[0]


def external_key(env_key=ENV_KEY, env_file=ENV_KEY_FILE):
    """The key given by the environment. A key file that was named but cannot be read stops the server."""
    path = os.environ.get(env_file)
    if path:
        try:
            value = Path(path).read_text(encoding='utf-8').strip()
        except (OSError, ValueError) as error:
            raise SystemExit(f'Não foi possível ler {env_file}: {error}') from None
        if not value:
            raise SystemExit(f'{env_file} aponta para um arquivo vazio.')
        return value
    return os.environ.get(env_key) or None


def configured():
    return bool(external_key()) or bool(stored())


def external_admin_key():
    return external_key(ENV_ADMIN_KEY, ENV_ADMIN_KEY_FILE)


def admin_configured():
    return bool(external_admin_key()) or bool(stored(admin_keys_path()))


def admin_key_shared():
    """A /v1 key must never open /admin, so one value cannot be both."""
    admin, client = external_admin_key(), external_key()
    return bool(admin) and verify(admin) or bool(client) and verify_admin(client)


def verify_admin(value):
    return _matches(value, stored(admin_keys_path()), external_admin_key())


def verify(value):
    return _matches(value, stored(), external_key())


def _matches(value, keys, env):
    """Compares against every key in constant time. Read on each request, so a removal takes effect at once."""
    if not value:
        return False
    digest = _hash(value)
    valid = False
    for item in keys.values():
        valid |= hmac.compare_digest(digest, str(item.get('hash', '')))
    if env:
        valid |= hmac.compare_digest(digest, _hash(env))
    return valid
