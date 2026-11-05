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


def keys_path():
    return config_tool.CONFIG_DIR / 'server_keys.json'


def _hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def stored():
    return config_tool.read_json(keys_path(), {})


def add(name):
    """Create a key and return its value. A repeated name raises ValueError instead of replacing it."""
    if not config_tool.NAME.fullmatch(name):
        raise ValueError('Nome inválido: use letras minúsculas, números, - e _')
    value = 'omni-' + secrets.token_urlsafe(32)

    def insert(data):
        if name in data:
            raise ValueError(f"A chave '{name}' já existe")
        data[name] = {'hash': _hash(value), 'created': int(time.time())}

    config_tool.update_json(keys_path(), insert)
    return value


def remove(name):
    found = []

    def drop(data):
        found.append(data.pop(name, None) is not None)

    config_tool.update_json(keys_path(), drop)
    return found[0]


def external_key():
    """The key given by the environment. A key file that was named but cannot be read stops the server."""
    path = os.environ.get(ENV_KEY_FILE)
    if path:
        try:
            value = Path(path).read_text(encoding='utf-8').strip()
        except (OSError, ValueError) as error:
            raise SystemExit(f'Não foi possível ler {ENV_KEY_FILE}: {error}') from None
        if not value:
            raise SystemExit(f'{ENV_KEY_FILE} aponta para um arquivo vazio.')
        return value
    return os.environ.get(ENV_KEY) or None


def configured():
    return bool(external_key()) or bool(stored())


def verify(value):
    """Compares against every key in constant time. Read on each request, so a removal takes effect at once."""
    if not value:
        return False
    digest = _hash(value)
    valid = False
    for item in stored().values():
        valid |= hmac.compare_digest(digest, str(item.get('hash', '')))
    env = external_key()
    if env:
        valid |= hmac.compare_digest(digest, _hash(env))
    return valid
