"""Config paths and the JSON core: atomic, locked read-modify-write, corrupt-file guard."""
import json
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get('SMART_AI_HOME', '~/.config/smart-ai')).expanduser()
MODELS_FILE = CONFIG_DIR / 'models.json'
ACCOUNTS_DIR = Path(os.environ.get('SMART_AI_ACCOUNTS', '~/.claude-accounts')).expanduser()
TEMPLATE_PROFILES = ROOT / 'profiles.json'
NAME = re.compile(r'[a-z0-9][a-z0-9_-]{0,31}')


def profiles_path():
    """User's accounts and routing live outside the install folder, so updates never overwrite them."""
    if os.environ.get('SMART_AI_CONFIG'):
        return Path(os.environ['SMART_AI_CONFIG'])
    user = CONFIG_DIR / 'profiles.json'
    if not user.exists() and TEMPLATE_PROFILES.exists():
        user.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(TEMPLATE_PROFILES, user)
    return user


def read_json(path, default):
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (FileNotFoundError, NotADirectoryError):
        return default
    except (ValueError, UnicodeDecodeError) as error:
        # A corrupt file must not look empty: returning the default here would make the next
        # write overwrite it and silently erase whatever was still in it.
        raise SystemExit(f'Arquivo de configuração corrompido: {path} ({error}). '
                         f'Corrija ou remova o arquivo e tente de novo.')
    except OSError as error:  # it exists but cannot be read: the same risk of being overwritten
        raise SystemExit(f'Não foi possível ler {path}: {error}. Corrija as permissões e tente de novo.') from None
    if isinstance(default, dict) and not isinstance(data, dict):
        raise SystemExit(f'Arquivo de configuração corrompido: {path} (esperado um objeto). Corrija ou remova o arquivo e tente de novo.')
    return data


_FILE_LOCKS = {}
_FILE_LOCKS_GUARD = threading.Lock()


def _file_lock(path):
    key = str(path)
    with _FILE_LOCKS_GUARD:
        return _FILE_LOCKS.setdefault(key, threading.Lock())


def update_json(path, mutate, default=None):
    """Read-modify-write a JSON file atomically: serialized across threads (a per-path lock) and,
    best effort, across processes (an advisory lock on a sidecar file). `mutate(data)` edits in
    place (or returns the new data). Prevents concurrent writers from clobbering each other."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(path):
        handle = None
        try:
            try:
                handle = open(path.with_name(path.name + '.lock'), 'w', encoding='utf-8')
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass  # no cross-process lock available; the thread lock still holds
            data = read_json(path, {} if default is None else default)
            result = mutate(data)
            data = data if result is None else result
            write_json(path, data)
            return data
        finally:
            if handle is not None:
                handle.close()


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.tmp-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(name, path)
    except BaseException:
        try:
            os.unlink(name)  # don't leave a .tmp-* behind when the write fails
        except OSError:
            pass
        raise
