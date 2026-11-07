"""Provider API keys: where they live, the ordered lookup (env > main > extras), and the key command."""
import getpass
import os
import re
import tempfile

from ..osutils import tighten_file
from ..tools import keys as tool_keys
from .store import CONFIG_DIR, NAME

# provider: (env var the CLI reads, expected prefix or None, where to create one)
KEYS = tool_keys()
KEYS_DIR = CONFIG_DIR / 'keys'
LEGACY_OPENROUTER = CONFIG_DIR / 'openrouter_key'


def key_path(provider):
    return KEYS_DIR / provider


def extra_key_path(provider, name):
    return KEYS_DIR / f'{provider}.{name}'


def stored_keys(provider):
    """Every key of a tool in the order they are tried: the env variable, the main saved key, then the
    extra ones (/key add TOOL NAME) by name. Each item: (name, value, origin)."""
    found = []
    env = os.environ.get(KEYS[provider][0])
    if env:
        found.append(('env', env, 'env'))
    def _read(path):
        tighten_file(path)
        try:
            return path.read_text(encoding='utf-8').strip()
        except (OSError, ValueError):
            return ''  # an unreadable/garbled key file must not break every lookup
    for path in (key_path(provider), LEGACY_OPENROUTER if provider == 'openrouter' else None):
        if path and path.is_file() and _read(path):
            found.append(('principal', _read(path), 'salva'))
            break
    extras = sorted(KEYS_DIR.glob(provider + '.*'), key=lambda p: (len(p.name), p.name)) if KEYS_DIR.is_dir() else []
    for path in extras:
        value = _read(path)
        if value:
            found.append((path.name[len(provider) + 1:], value, 'salva'))
    return found


def stored_key(provider):
    keys = stored_keys(provider)
    if keys:
        return keys[0][1], keys[0][2]
    return '', ''


def ask_key(provider, inline):
    env, prefix, url = KEYS[provider]
    if inline:
        value = inline.strip()  # typed inline: /key openrouter sk-or-...
    else:
        print(f'Chave de {provider} ({url}). Cole e tecle Enter; ela não aparece na tela.')
        try:
            value = getpass.getpass('> ').strip()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit('\nCancelado.')
    if not value or not re.fullmatch(r'[A-Za-z0-9._-]{16,}', value):
        raise SystemExit('Chave vazia ou com formato inválido.')
    if prefix and not value.startswith(prefix):
        raise SystemExit(f'Chave de {provider} deve começar com {prefix}')
    return value


def save_key(path, value):
    KEYS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(KEYS_DIR, 0o700)
    # Write to a fresh 0600 temp file and swap it in, so overwriting an existing key never
    # leaves the secret in a file that kept an older, world-readable mode.
    fd, tmp = tempfile.mkstemp(dir=KEYS_DIR, prefix='.tmp-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(value)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def next_key_name(provider):
    taken = {name for name, _, _ in stored_keys(provider)}
    return next(f'chave{n}' for n in range(2, 1000) if f'chave{n}' not in taken)


def cmd_key(args):
    """/key list · /key TOOL [value] (main key) · /key add TOOL [NAME] [value] (one more key)
    · /key remove TOOL [NAME]. Keys of a tool are tried in order when one fails or hits its limit."""
    action = args[0] if args else 'list'
    if action == 'list':
        for provider, (env, _, url) in KEYS.items():
            keys = stored_keys(provider)
            if not keys:
                print(f'  {provider:<11} {"—":<34} {env:<19} {url}')
            for i, (name, value, origin) in enumerate(keys):
                shown = f'{name}: {value[:7]}…{value[-4:]} [{origin}]'
                print(f'  {provider if i == 0 else "":<11} {shown:<34} {env if i == 0 else "":<19} {url if i == 0 else ""}')
        print('\nPrincipal: /key claude · mais uma: /key add claude [NOME] · remover: /key remove claude [NOME]')
        print('Assinatura vem primeiro; as chaves são usadas em ordem quando ela falha ou bate no limite.')
        print('OpenRouter funciona só com chave.')
        return
    if action in KEYS:
        action = 'set'
    else:
        args = args[1:]
    if not args or args[0] not in KEYS:
        raise SystemExit('Informe a ferramenta: ' + ', '.join(KEYS))
    provider = args[0]
    env = KEYS[provider][0]
    if action == 'get':
        print(stored_key(provider)[0])
    elif action == 'set':
        save_key(key_path(provider), ask_key(provider, args[1] if len(args) > 1 else ''))
        print(f'✓ Chave principal de {provider} salva em {key_path(provider)} (permissão 600).')
        if os.environ.get(env):
            print(f'  Aviso: a variável {env} está definida e é tentada primeiro.')
    elif action == 'add':
        rest = args[1:]
        name = rest.pop(0) if rest and NAME.fullmatch(rest[0]) and len(rest[0]) < 16 else next_key_name(provider)
        if name in ('env', 'principal') or extra_key_path(provider, name).exists():
            raise SystemExit(f'Já existe uma chave {name} para {provider}.')
        if not key_path(provider).is_file() and not (provider == 'openrouter' and LEGACY_OPENROUTER.is_file()):
            path, name = key_path(provider), 'principal'
        else:
            path = extra_key_path(provider, name)
        save_key(path, ask_key(provider, rest[0] if rest else ''))
        print(f'✓ Chave {name} de {provider} salva ({len(stored_keys(provider))} no total, usadas em ordem).')
    elif action == 'remove':
        name = args[1] if len(args) > 1 else 'principal'
        paths = [key_path(provider), LEGACY_OPENROUTER if provider == 'openrouter' else None] if name == 'principal' \
            else [extra_key_path(provider, name) if NAME.fullmatch(name) else None]
        removed = False
        for path in paths:
            if path and path.exists():
                path.unlink()
                removed = True
        print(f'✓ Chave {name} de {provider} removida.' if removed else f'Nenhuma chave {name} salva para {provider}.')
    else:
        raise SystemExit('Ação inválida: list, set, add, remove')
