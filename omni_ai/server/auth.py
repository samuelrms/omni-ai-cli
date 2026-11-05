"""Server API keys: only their hashes are stored; the value is shown once, when it is created."""
import hashlib
import hmac
import os
import secrets
import time

from .. import config as config_tool

ENV_KEY = 'SMART_AI_SERVER_KEY'


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


def configured():
    return bool(os.environ.get(ENV_KEY)) or bool(stored())


def verify(value):
    """Compares against every key in constant time. Read on each request, so a removal takes effect at once."""
    if not value:
        return False
    digest = _hash(value)
    valid = False
    for item in stored().values():
        valid |= hmac.compare_digest(digest, str(item.get('hash', '')))
    env = os.environ.get(ENV_KEY)
    if env:
        valid |= hmac.compare_digest(digest, _hash(env))
    return valid
