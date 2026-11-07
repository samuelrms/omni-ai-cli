"""Administration API: /admin/*. Off unless --enable-admin, with its own keys. Secret values only ever come in."""
import ipaddress
import os
import re
import time

from .. import config as config_tool
from ..cli.base import cooldown_left, installed
from ..cli.health import ai_version
from ..tools import CLIS
from . import auth
from .protocol import RequestError

PROTECTED_ENDINGS = {'mode', 'bin', 'path', 'dir', 'file', 'sandbox'}
ROUTING_FIELDS = ('usage_skip_percent', 'usage_alert_percent')
MAX_KEY_LENGTH = 512
KEY_VALUE = re.compile(r'[\x21-\x7e]+')
LOGIN_ROUTE = re.compile(r'/admin/accounts/([a-z0-9][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)?)/login(/complete)?')
KEY_ID = re.compile(r'([a-z]+)(?:\.([a-z0-9][a-z0-9_-]{0,14}))?')


def _protected(field):
    words = field.lower().split('_')
    return words[-1] in PROTECTED_ENDINGS or any(word.startswith('permission') for word in words)


def _forbid_or_reject(field):
    if _protected(field):
        raise RequestError(403, f"The field '{field}' cannot be changed through the API.", 'permission_error', param=field)
    raise RequestError(400, f"Unknown field '{field}'.", param=field)


def _checked_object(value, where):
    if not isinstance(value, dict):
        raise RequestError(400, f"'{where}' must be an object.", param=where)
    return value


def require_https(handler):
    """Only prevents accidents. The header comes from the client: whoever reaches this port directly can forge it,
    so the port must be closed to anything but the proxy, which has to overwrite the header."""
    if handler.server.admin_allow_http:
        return
    try:
        local = ipaddress.ip_address(handler.client_address[0].removeprefix('::ffff:')).is_loopback
    except ValueError:
        local = False
    if not local and handler.headers.get('X-Forwarded-Proto', '').lower() != 'https':
        raise RequestError(403, 'The administration API requires HTTPS through a reverse proxy.', 'permission_error')


def _accounts_data():
    return config_tool.profiles()


def read_config():
    env_set = [key for key, (_, env, _) in config_tool.MODELS.items() if os.environ.get(env)]
    data = _accounts_data()
    routing = data.get('routing', {})
    return {'models': config_tool.model_values(),
            'models_set_by_environment': env_set,
            'routing': {name: routing[name] for name in ROUTING_FIELDS if name in routing},
            'fallback_order': [{'name': name, 'priority': conf.get('priority', 99), 'enabled': conf.get('enabled', True)}
                               for name, conf in config_tool.ordered(data)]}


def _check_models(models):
    # The paid switch goes first, because a paid model is only valid once it is on
    current = config_tool.model_values()
    for key in sorted(models, key=lambda k: k != 'openrouter_paid'):
        value = models[key]
        if key not in config_tool.MODELS:
            _forbid_or_reject(key)
        if not isinstance(value, str):
            raise RequestError(400, f"'{key}' must be a string.", param=key)
        paid = models.get('openrouter_paid', current['openrouter_paid'])
        if key == 'openrouter' and paid == 'on':
            if not re.fullmatch(r'[A-Za-z0-9._:/@\[\]-]*', value) or value.startswith('-'):
                raise RequestError(400, f"Invalid value for '{key}'.", param=key)
            continue
        try:
            config_tool.validate_model(key, value)
        except SystemExit as error:
            raise RequestError(400, str(error), param=key) from None


def _check_order(order, known):
    for name, change in order.items():
        if name not in known:
            raise RequestError(400, f"Unknown account '{name}'.", param=name)
        for field, value in _checked_object(change, name).items():
            if field == 'priority' and type(value) is int and 0 <= value <= 1000:
                continue
            if field == 'enabled' and isinstance(value, bool):
                continue
            if field in ('priority', 'enabled'):
                raise RequestError(400, f"Invalid value for '{field}'.", param=field)
            _forbid_or_reject(field)


def _check_routing(routing):
    for field, value in routing.items():
        if field not in ROUTING_FIELDS:
            _forbid_or_reject(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
            raise RequestError(400, f"'{field}' must be between 0 and 100.", param=field)


def write_config(body):
    """All or nothing: everything is checked before the first write."""
    for field in _checked_object(body, 'body'):
        if field not in ('models', 'fallback_order', 'routing'):
            _forbid_or_reject(field)
    models = _checked_object(body.get('models', {}), 'models')
    order = _checked_object(body.get('fallback_order', {}), 'fallback_order')
    routing = _checked_object(body.get('routing', {}), 'routing')
    _check_models(models)
    _check_order(order, _accounts_data()['profiles'])
    _check_routing(routing)
    if models:
        config_tool.update_json(config_tool.MODELS_FILE, lambda data: data.update(models))
    if order or routing:
        def change(data):
            for name, values in order.items():
                data['profiles'][name].update(values)
            if routing:
                data.setdefault('routing', {}).update(routing)
        config_tool.update_json(config_tool.profiles_path(), change)
    return read_config()


def _ending(value):
    return value[-4:] if len(value) >= 12 else ''


def list_keys():
    return {'data': [{'id': provider if name in ('principal', 'env') else f'{provider}.{name}', 'provider': provider,
                      'name': name, 'origin': origin, 'ending': _ending(value)}
                     for provider in config_tool.KEYS for name, value, origin in config_tool.stored_keys(provider)]}


def add_key(body):
    _checked_object(body, 'body')
    for field in body:
        if field not in ('provider', 'name', 'value'):
            _forbid_or_reject(field)
    provider, name, value = body.get('provider'), body.get('name'), body.get('value')
    if provider not in config_tool.KEYS:
        raise RequestError(400, 'Unknown provider.', param='provider')
    if not isinstance(value, str) or len(value) > MAX_KEY_LENGTH or not KEY_VALUE.fullmatch(value):
        raise RequestError(400, 'Invalid key value.', param='value')
    prefix = config_tool.KEYS[provider][1]
    if prefix and not value.startswith(prefix):
        raise RequestError(400, f'This provider expects a key starting with {prefix}', param='value')
    if name is None or name == 'principal':
        path, name = config_tool.key_path(provider), 'principal'
        taken = any(n == 'principal' for n, _, _ in config_tool.stored_keys(provider))
    else:
        if not isinstance(name, str) or not KEY_ID.fullmatch(f'{provider}.{name}') or name == 'env':
            raise RequestError(400, 'Invalid key name.', param='name')
        path = config_tool.extra_key_path(provider, name)
        taken = path.exists()
    if taken:
        raise RequestError(409, 'This key already exists. Delete it first to replace it.', code='key_exists')
    config_tool.save_key(path, value)
    return {'id': provider if name == 'principal' else f'{provider}.{name}', 'provider': provider, 'name': name,
            'origin': 'salva', 'ending': _ending(value)}


def delete_key(key_id):
    match = KEY_ID.fullmatch(key_id)
    provider = match.group(1) if match else None
    if provider not in config_tool.KEYS:
        raise RequestError(404, 'Key not found.')
    if match.group(2):
        paths = [config_tool.extra_key_path(provider, match.group(2))]
    else:
        paths = [config_tool.key_path(provider)] + ([config_tool.LEGACY_OPENROUTER] if provider == 'openrouter' else [])
    existing = [path for path in paths if path.is_file()]
    if not existing:
        raise RequestError(404, 'Key not found. A key set by an environment variable cannot be deleted here.')
    for path in existing:
        path.unlink()


def accounts():
    claude = [{'tool': 'claude', 'name': name, 'enabled': conf.get('enabled', True),
               'logged_in': bool(config_tool.logged_in(conf)), 'id': name, 'cooldown_seconds': cooldown_left(name)}
              for name, conf in config_tool.ordered(_accounts_data())]
    # The main login of the other CLIs: not a profile, but what /admin/accounts/{tool}/login acts on
    from ..onboarding import logged_in
    mains = [{'tool': tool, 'name': 'primary', 'id': tool, 'enabled': True, 'logged_in': bool(logged_in(tool)), 'cooldown_seconds': cooldown_left(tool)}
             for tool in CLIS if tool in config_tool.MAIN_LOGINS and tool != 'claude' and installed(tool)]
    others = [{'tool': tool, 'name': name, 'id': f'{tool}.{name}', 'enabled': conf.get('enabled', True),
               'logged_in': bool(config_tool.tool_logged_in(tool, conf)), 'cooldown_seconds': cooldown_left(f'{tool}:{name}')}
              for tool in CLIS if tool != 'claude' for name, conf in config_tool.tool_accounts(tool)]
    return {'data': claude + mains + others}


def list_client_keys():
    return {'data': [{'name': name, 'created': item.get('created')} for name, item in sorted(auth.stored().items())]}


def add_client_key(body):
    _checked_object(body, 'body')
    for field in body:
        if field != 'name':
            _forbid_or_reject(field)
    name = body.get('name')
    if not isinstance(name, str):
        raise RequestError(400, "'name' is required.", param='name')
    try:
        return {'name': name, 'key': auth.add(name)}
    except ValueError as error:
        status = 409 if 'existe' in str(error) else 400
        raise RequestError(status, str(error), param='name') from None


def health(server):
    with server.running_lock:
        running = len(server.running)
    return {'status': 'ok', 'version': ai_version(), 'uptime_seconds': int(time.monotonic() - server.started),
            'mode': server.mode, 'running_requests': running, 'tools': {tool: installed(tool) for tool in CLIS}}


def handle(handler, method, path):
    """Authentication, then one route. Returns nothing: it answers or raises RequestError."""
    require_https(handler)
    scheme, _, value = (handler.headers.get('Authorization') or '').partition(' ')
    if scheme.lower() != 'bearer' or not auth.verify_admin(value.strip()):
        raise RequestError(401, 'Invalid or missing administration key.', 'authentication_error', code='invalid_api_key')
    if path == '/admin/config' and method == 'GET':
        handler._json(200, read_config())
    elif path == '/admin/config' and method == 'PUT':
        handler._json(200, write_config(handler._read_body()))
    elif path == '/admin/keys' and method == 'GET':
        handler._json(200, list_keys())
    elif path == '/admin/keys' and method == 'POST':
        handler._json(201, add_key(handler._read_body()))
    elif path.startswith('/admin/keys/') and method == 'DELETE':
        delete_key(path[len('/admin/keys/'):])
        handler._json(200, {'deleted': True})
    elif path == '/admin/accounts' and method == 'GET':
        handler._json(200, accounts())
    elif (match := LOGIN_ROUTE.fullmatch(path)) and method == 'POST':
        logins = handler.server.logins
        if match.group(2):
            status, body = logins.complete(match.group(1), handler._read_body())
            handler._json(status, body)
        else:
            handler._json(201, logins.start(match.group(1)))
    elif (match := LOGIN_ROUTE.fullmatch(path)) and method == 'DELETE' and not match.group(2):
        logins = handler.server.logins
        logins.cancel(match.group(1), handler.headers.get('X-Omni-Login-Attempt'))
        handler._json(200, {'cancelled': True})
    elif path == '/admin/api-keys' and method == 'GET':
        handler._json(200, list_client_keys())
    elif path == '/admin/api-keys' and method == 'POST':
        handler._json(201, add_client_key(handler._read_body()))
    elif path.startswith('/admin/api-keys/') and method == 'DELETE':
        if not auth.remove(path[len('/admin/api-keys/'):]):
            raise RequestError(404, 'Key not found.')
        handler._json(200, {'deleted': True})
    elif path == '/admin/health' and method == 'GET':
        handler._json(200, health(handler.server))
    else:
        raise RequestError(404, 'Unknown route.')
