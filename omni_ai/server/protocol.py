"""OpenAI wire format: request validation, conversation to prompt, responses and errors."""
import math
import time

ROLE_LABELS = {'system': 'System', 'developer': 'System', 'user': 'User', 'assistant': 'Assistant'}
MAX_PROMPT_BYTES = 100_000  # the prompt reaches the tools as one command-line argument (Linux caps it at 128 KiB)
UNSUPPORTED_PARAMS = ('top_p', 'stop', 'presence_penalty', 'frequency_penalty', 'logit_bias', 'seed',
                      'response_format', 'logprobs', 'top_logprobs')
AUTO_MODEL = 'omni-auto'
TOOL_PREFIX = 'omni/'


class RequestError(Exception):
    def __init__(self, status, message, kind='invalid_request_error', param=None, code=None, retry_after=None):
        super().__init__(message)
        self.status, self.message, self.kind, self.param, self.code = status, message, kind, param, code
        self.retry_after = retry_after


def error_body(message, kind='invalid_request_error', param=None, code=None):
    return {'error': {'message': message, 'type': kind, 'param': param, 'code': code}}


def _text(content, index):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if not isinstance(part, dict) or part.get('type') != 'text' or not isinstance(part.get('text'), str):
                raise RequestError(400, 'Only text content is supported.', param=f'messages[{index}].content')
            parts.append(part['text'])
        return '\n'.join(parts)
    raise RequestError(400, 'Message content must be text.', param=f'messages[{index}].content')


def build_prompt(messages):
    """Tools take one prompt, so the conversation becomes labelled text. A lone user message stays untouched.
    The labels organize the text; they are not a security boundary, and a message can imitate them."""
    if not isinstance(messages, list) or not messages:
        raise RequestError(400, "'messages' must be a non-empty list.", param='messages')
    turns = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get('role') not in ROLE_LABELS:
            raise RequestError(400, 'Supported roles: system, developer, user and assistant.',
                               param=f'messages[{index}].role')
        turns.append((message['role'], _text(message.get('content'), index)))
    if turns[-1][0] != 'user' or not turns[-1][1].strip():
        raise RequestError(400, 'The last message must be a non-empty user message.', param='messages')
    if len(turns) == 1:
        prompt = turns[0][1]
    else:
        prompt = '\n\n'.join(f'{ROLE_LABELS[role]}:\n{text}' for role, text in turns) + \
            '\n\nReply only to the last User message.'
    if len(prompt.encode('utf-8')) > MAX_PROMPT_BYTES:
        raise RequestError(400, f'The conversation exceeds the limit of {MAX_PROMPT_BYTES} bytes.',
                           param='messages', code='context_length_exceeded')
    return prompt, turns[-1][1]


def parse_request(body):
    """Validated request fields. `tools` is refused rather than ignored: a client would wait for a call that never comes."""
    if not isinstance(body, dict):
        raise RequestError(400, 'The request body must be a JSON object.')
    model = body.get('model')
    if not isinstance(model, str) or not model:
        raise RequestError(400, "'model' is required.", param='model')
    prompt, last_user = build_prompt(body.get('messages'))
    if body.get('tools') or body.get('functions'):
        raise RequestError(400, 'Tools and function calling are not supported yet.', param='tools')
    if body.get('n') not in (None, 1):
        raise RequestError(400, "Only 'n' = 1 is supported.", param='n')
    temperature = body.get('temperature')
    if temperature is not None and (isinstance(temperature, bool) or not isinstance(temperature, (int, float))
                                    or not math.isfinite(temperature) or not 0 <= temperature <= 2):
        raise RequestError(400, "'temperature' must be between 0 and 2.", param='temperature')
    max_tokens = body.get('max_completion_tokens', body.get('max_tokens'))
    if max_tokens is not None and (isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1):
        raise RequestError(400, "'max_tokens' must be a positive integer.", param='max_tokens')
    stream = body.get('stream', False)
    if not isinstance(stream, bool):
        raise RequestError(400, "'stream' must be a boolean.", param='stream')
    options = body.get('stream_options')
    return {
        'model': model, 'prompt': prompt, 'last_user': last_user, 'temperature': temperature,
        'max_tokens': max_tokens, 'stream': stream,
        'include_usage': isinstance(options, dict) and options.get('include_usage') is True,
        'ignored': [name for name in UNSUPPORTED_PARAMS if body.get(name) not in (None, False)],
    }


def estimate_tokens(text):
    return max(1, len(text) // 4) if text else 0


def usage_body(reported, prompt, answer):
    """Tokens from the provider when it said them; otherwise an estimate. Returns (usage, 'reported' | 'estimated')."""
    if reported:
        return {'prompt_tokens': reported[0], 'completion_tokens': reported[1],
                'total_tokens': reported[0] + reported[1]}, 'reported'
    prompt_tokens, completion_tokens = estimate_tokens(prompt), estimate_tokens(answer)
    return {'prompt_tokens': prompt_tokens, 'completion_tokens': completion_tokens,
            'total_tokens': prompt_tokens + completion_tokens}, 'estimated'


def completion_body(request_id, created, model, answer, finish_reason, usage):
    return {'id': request_id, 'object': 'chat.completion', 'created': created, 'model': model,
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': answer},
                         'logprobs': None, 'finish_reason': finish_reason}],
            'usage': usage}


def chunk_body(request_id, created, model, delta, finish_reason=None):
    return {'id': request_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model,
            'choices': [{'index': 0, 'delta': delta, 'logprobs': None, 'finish_reason': finish_reason}]}


def model_body(model_id):
    return {'id': model_id, 'object': 'model', 'created': int(time.time()), 'owned_by': 'omni'}
