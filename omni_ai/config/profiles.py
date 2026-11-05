"""Claude routing profiles: load, order by priority, list enabled ones for the fallback loop."""
from .store import profiles_path, read_json


def profiles():
    data = read_json(profiles_path(), None)
    if data is None:
        raise SystemExit(f'Não consegui ler {profiles_path()}')
    data.setdefault('profiles', {})
    return data


def ordered(data):
    return sorted(data['profiles'].items(), key=lambda item: item[1].get('priority', 99))


def enabled_profiles():
    """Names of enabled profiles by priority, for the fallback loop."""
    return [name for name, profile in ordered(profiles()) if profile.get('enabled', True)]


def cmd_profiles():
    for name in enabled_profiles():
        print(name)
