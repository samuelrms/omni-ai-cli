"""Function calling through provider APIs. The client's tools are only described to the model and never run here."""
import json
import os
import threading
import urllib.error
import urllib.request
import uuid

from .. import config as config_tool
from .. import deepseek, grok
from ..chat.spend import record_spend, spend
from ..cli.base import cooldown_left, mark_rate_limited
from ..cli.executors import key_account
from ..openrouter.client import DEFAULT_MODEL, OPENROUTER_BASE
from ..pii import PIIMasker
from .execution import FAILED, OK, RATE_LIMITED

CALL_TIMEOUT = 300
NATIVE = ('openrouter', 'deepseek', 'grok')  # in the order omni-auto tries them: the free option first
BASES = {'openrouter': OPENROUTER_BASE, 'deepseek': deepseek.DEEPSEEK_BASE, 'grok': grok.XAI_BASE}
TYPES = {'object': dict, 'array': list, 'string': str, 'boolean': bool, 'null': type(None)}


def fits_schema(value, schema):
    """The common core of JSON Schema: a model's arguments must not reach the client looking valid when they are not.
    What is not checked here (patterns, $ref, oneOf...) is left to the client."""
    if not isinstance(schema, dict):
        return True
    kinds = schema.get('type')
    if kinds is not None and not any(_is_type(value, kind) for kind in (kinds if isinstance(kinds, list) else [kinds])):
        return False
    if 'enum' in schema and value not in schema['enum']:
        return False
    if isinstance(value, dict):
        properties = schema.get('properties') or {}
        if any(name not in value for name in schema.get('required') or []):
            return False
        if schema.get('additionalProperties') is False and any(name not in properties for name in value):
            return False
        return all(fits_schema(item, properties[name]) for name, item in value.items() if name in properties)
    if isinstance(value, list):
        return all(fits_schema(item, schema.get('items')) for item in value)
    return True


def _is_type(value, kind):
    if kind == 'number':
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == 'integer':
        return (isinstance(value, int) and not isinstance(value, bool)) or (isinstance(value, float) and value.is_integer())
    return isinstance(value, TYPES[kind]) if kind in TYPES else True


def valid_calls(calls, request):
    """False for a call the client could not act on: unknown function, bad arguments, or against tool_choice."""
    schemas = {tool['function']['name']: tool['function'].get('parameters') for tool in request['tools']}
    choice = request['tool_choice']
    if choice == 'none' and calls:
        return False
    if (choice == 'required' or isinstance(choice, dict)) and not calls:
        return False
    for call in calls:
        function = call.get('function') if isinstance(call, dict) else None
        if not isinstance(function, dict) or function.get('name') not in schemas:
            return False
        if isinstance(choice, dict) and function['name'] != choice['function']['name']:
            return False
        try:
            arguments = json.loads(function.get('arguments') or '{}')
        except (TypeError, ValueError):
            return False
        if not isinstance(arguments, dict) or not fits_schema(arguments, schemas[function['name']]):
            return False
    return True


class Masking:
    """Same protection as the plain path. Every text that leaves goes masked, and what comes back is restored."""

    def __init__(self):
        self.masker, self.mapping = PIIMasker(), {}
        self.count = 0

    def _mask(self, text):
        self.count += 1
        masked, found = self.masker.mask(text, namespace=f'M{self.count}_')
        self.mapping.update(found)
        return masked

    def message(self, message):
        out = dict(message)
        content = out.get('content')
        if isinstance(content, str):
            out['content'] = self._mask(content)
        elif isinstance(content, list):
            out['content'] = [dict(part, text=self._mask(part['text'])) for part in content]
        if out.get('tool_calls'):
            out['tool_calls'] = [dict(call, function=dict(call['function'], arguments=self._mask(call['function']['arguments'])))
                                 for call in out['tool_calls']]
        return out

    def restore(self, text):
        return self.masker.unmask(text, self.mapping) if self.mapping else text


class Native:
    """Same surface as Execution, so the handler treats both alike."""

    def __init__(self, tool, request):
        self.tool, self.request = tool, request
        self.child = None
        self.stop_reason = None
        self.stopped = threading.Event()
        self.provider, self.model, self.tokens = None, None, None
        self.answer, self.tool_calls, self.finish_reason = '', [], None
        self.job = {'engine': tool}

    def stop(self, reason):
        if not self.stop_reason:
            self.stop_reason = reason
        self.stopped.set()

    def _candidates(self):
        values = config_tool.model_values()
        for provider in (NATIVE if self.tool == 'auto' else (self.tool,)):
            model = values[provider] if provider in values else ''
            if provider == 'openrouter':
                model = model or DEFAULT_MODEL
                if values['openrouter_paid'] != 'on' and model != DEFAULT_MODEL and not model.endswith(':free'):
                    continue
            for name, key, _ in config_tool.stored_keys(provider):
                account = key_account(provider, name)
                if not cooldown_left(account):
                    yield provider, model, key, account

    def _payload(self, provider, model, key, masking):
        if provider == 'grok' and not model:
            model = grok.pick_model(key)
        elif provider == 'deepseek' and not model:
            model = deepseek.DEFAULT_MODEL
        data = {'model': model, 'stream': False, 'tools': self.request['tools'],
                'messages': [masking.message(message) for message in self.request['messages']]}
        for field in ('tool_choice', 'parallel_tool_calls', 'temperature', 'max_tokens'):
            if self.request.get(field) is not None:
                data[field] = self.request[field]
        if provider == 'openrouter':
            data['provider'] = {'require_parameters': True}  # only providers that really take tools
        return data

    def _post(self, provider, key, data, results):
        request = urllib.request.Request(BASES[provider] + '/chat/completions', json.dumps(data).encode('utf-8'), {
            'Authorization': f'Bearer {key}', 'Content-Type': 'application/json', 'User-Agent': 'omni-ai-cli',
            'HTTP-Referer': 'https://github.com/samuelrms/omni-ai-cli', 'X-Title': 'Omni AI CLI'})
        try:
            with urllib.request.urlopen(request, timeout=CALL_TIMEOUT) as response:
                results.append((response.status, json.load(response)))
        except urllib.error.HTTPError as error:
            results.append((error.code, None))
        except (OSError, ValueError):
            results.append((0, None))

    def _call(self, provider, key, data):
        """Waits for the provider or for a stop, whichever comes first: a blocked read cannot be interrupted."""
        results = []
        thread = threading.Thread(target=self._post, args=(provider, key, data, results), daemon=True)
        thread.start()
        while thread.is_alive() and not self.stopped.is_set():
            thread.join(0.1)
        return results[0] if results else (0, None)

    def _accept(self, body, provider, model, masking):
        try:
            choice = body['choices'][0]
            message = choice['message']
        except (KeyError, IndexError, TypeError):
            return False
        calls = []
        for call in message.get('tool_calls') or []:
            if not isinstance(call, dict) or not isinstance(call.get('function'), dict):
                return False
            function = call['function']
            arguments = function.get('arguments')
            calls.append({'id': call.get('id') or 'call_' + uuid.uuid4().hex, 'type': 'function',
                          'function': {'name': function.get('name'), 'arguments': masking.restore(arguments) if isinstance(arguments, str) else ''}})
        content = message.get('content')
        if (not calls and not (isinstance(content, str) and content.strip())) or not valid_calls(calls, self.request):
            return False
        self.answer = masking.restore(content) if isinstance(content, str) else ''
        self.tool_calls, self.provider, self.model = calls, provider, body.get('model', model)
        self.finish_reason = 'tool_calls' if calls else choice.get('finish_reason')
        usage = body.get('usage')
        if isinstance(usage, dict):
            spend(self.job, usage)
            if usage.get('prompt_tokens') or usage.get('completion_tokens'):
                self.tokens = (int(usage.get('prompt_tokens') or 0), int(usage.get('completion_tokens') or 0))
        return True

    def run(self, on_delta=None):
        limited = tried = 0
        try:
            for provider, model, key, account in self._candidates():
                masking = Masking()
                try:
                    data = self._payload(provider, model, key, masking)
                except (OSError, ValueError, RuntimeError):
                    continue
                tried += 1
                status, body = self._call(provider, key, data)
                if self.stopped.is_set():
                    return self.stop_reason
                if status == 200 and self._accept(body, provider, model, masking):
                    return OK
                if status == 429:
                    limited += 1
                    mark_rate_limited(account)
            return RATE_LIMITED if tried and limited == tried else FAILED
        finally:
            self.job['provider'] = self.provider or self.tool
            record_spend(self.job)


def capable(tool):
    """Whether a request with tools has somewhere to go. Agent CLIs have no way to hand a call back to the client."""
    names = NATIVE if tool == 'auto' else (tool,) if tool in NATIVE else ()
    return any(config_tool.stored_keys(name) for name in names)
