"""Experimental function calling on agent CLIs: the model writes the call as JSON, and it is checked and converted here.
Only CLIs whose own tools can really be switched off are allowed, so a request never makes the machine read or run anything."""
import json
import re
import uuid

from .execution import Execution, FAILED, OK, RATE_LIMITED
from .native import valid_calls
from .protocol import MAX_PROMPT_BYTES, ROLE_LABELS, RequestError, _text

EMULATED = ('claude', 'grok')
FENCE = re.compile(r'^```[a-zA-Z]*\s*(.*?)\s*```$', re.S)
INSTRUCTIONS = """You are answering through an API, and you cannot run anything yourself. The caller runs the functions below.

Functions (JSON):
{functions}

{choice}

Reply with one JSON object and nothing else, in one of these two forms:
{{"tool_calls": [{{"name": "<function>", "arguments": {{...}}}}]}}
{{"content": "<your answer>"}}

Conversation:
"""


def target(tool, installed):
    """The CLI that will emulate this model, or None."""
    if tool == 'auto':
        return next((name for name in EMULATED if installed(name)), None)
    return tool if tool in EMULATED and installed(tool) else None


def _choice_text(choice):
    if choice == 'none':
        return 'Do not call any function now: use the "content" form.'
    if choice == 'required':
        return 'You must call at least one function.'
    if isinstance(choice, dict):
        return f'You must call the function {choice["function"]["name"]}.'
    return 'Call a function only if it is needed.'


def build_prompt(request, correction=None):
    turns = []
    for index, message in enumerate(request['messages']):
        role, content = message['role'], message.get('content')
        text = _text(content, index) if content is not None else ''
        if role == 'tool':
            turns.append(f'Function result ({message["tool_call_id"]}):\n{text}')
            continue
        for call in message.get('tool_calls') or []:
            text += f'\n[called {call["function"].get("name")} with {call["function"]["arguments"]}]'
        turns.append(f'{ROLE_LABELS[role]}:\n{text.strip()}')
    functions = json.dumps([tool['function'] for tool in request['tools']], ensure_ascii=False)
    prompt = INSTRUCTIONS.format(functions=functions, choice=_choice_text(request['tool_choice'])) + '\n\n'.join(turns)
    if correction:
        prompt += f'\n\nYour previous reply was rejected: {correction} Reply again with only the JSON object.'
    if len(prompt.encode('utf-8')) > MAX_PROMPT_BYTES:
        raise RequestError(400, f'The conversation exceeds the limit of {MAX_PROMPT_BYTES} bytes.', param='messages',
                           code='context_length_exceeded')
    return prompt


def parse_reply(text):
    """(content, calls) from the model's reply. ValueError says what to correct."""
    text = text.strip()
    fenced = FENCE.match(text)
    try:
        data = json.loads(fenced.group(1) if fenced else text)
    except ValueError:
        if 'tool_calls' in text:
            raise ValueError('it was not valid JSON.') from None
        return text, []  # a plain answer is still an answer; tool_choice is checked afterwards
    if not isinstance(data, dict) or not ('tool_calls' in data or isinstance(data.get('content'), str)):
        raise ValueError('it did not have the expected form.')
    calls = []
    entries = data.get('tool_calls') or []
    if not isinstance(entries, list):
        raise ValueError('tool_calls must be a list.')
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get('arguments', {}), dict):
            raise ValueError('each call needs a name and an arguments object.')
        calls.append({'id': 'call_' + uuid.uuid4().hex, 'type': 'function',
                      'function': {'name': entry.get('name'), 'arguments': json.dumps(entry.get('arguments', {}), ensure_ascii=False)}})
    return data.get('content') if isinstance(data.get('content'), str) else '', calls


class Emulated:
    """Same surface as Execution. Always read-only, in a temporary folder, whatever mode the server runs in."""

    MAX_TRIES = 2

    def __init__(self, tool, request):
        self.tool, self.request = tool, request
        self.inner = None
        self.stop_reason = None
        self.provider, self.tokens = None, None
        self.answer, self.tool_calls = '', []
        build_prompt(request)  # an oversized request fails now, as a normal error

    @property
    def child(self):
        return self.inner.child if self.inner else None

    def stop(self, reason):
        self.stop_reason = self.stop_reason or reason
        if self.inner:
            self.inner.stop(reason)

    def run(self, on_delta=None):
        correction = None
        for _ in range(self.MAX_TRIES):
            prompt = build_prompt(self.request, correction)
            self.inner = Execution(self.tool, prompt, prompt, no_tools=True)
            outcome = self.inner.run(None)
            self.provider, self.tokens = self.inner.provider, self.inner.tokens
            if self.stop_reason:
                return self.stop_reason
            if outcome != OK:
                return outcome if outcome == RATE_LIMITED else FAILED
            if self.provider not in EMULATED:
                return FAILED  # a fallback reached a tool whose own tools stay on
            try:
                content, calls = parse_reply(self.inner.answer)
            except ValueError as error:
                correction = str(error)
                continue
            if valid_calls(calls, self.request):
                self.answer, self.tool_calls = content, calls
                return OK
            correction = 'the call did not match the function, its parameters, or the required choice.'
        return FAILED
