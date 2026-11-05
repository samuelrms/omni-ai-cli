"""Prompt routing classifier: free LLM backends (Groq/Cerebras/OpenRouter), paid Grok, keyword rules."""
import json
import os
import re

from .. import config as config_tool
from .base import config, models

# Groq retires models often; the second one covers the first going away
GROQ_CLASSIFIER_MODELS = [('qwen/qwen3.8-27b', 'none'), ('openai/gpt-oss-20b', 'low')]
GROK_CLASSIFIER_MODEL = 'grok-4-fast-non-reasoning'
# Cerebras: free tier and very fast; the second free classifier when Groq is down or out of quota
CEREBRAS_CLASSIFIER_MODELS = ['llama3.1-8b', 'gpt-oss-120b']
CLASSIFIER_MODELS = ['nvidia/nemotron-3.5-lightning:free', 'google/gemma-4-26b-a4b-it:free', 'openrouter/free']
CLASSIFIER_SYSTEM = (
    'You are a router, not an assistant: never answer or carry out the prompt, only label it.\n'
    'The CLI runs inside the user\'s project directory and can read files, run shell and use tools.\n'
    'complex: anything that touches code, files, the repository, shell or GitHub, or that reasons, '
    'plans, debugs, designs, compares trade-offs, or needs more than one step or any real work — '
    'even when short and even when no file is named.\n'
    'simple: ONLY a one-shot general-knowledge chat a plain model answers off the top of its head '
    'with no project context — a definition, a fact, a translation, a quick rewrite of given text.\n'
    'The two tiers cost very differently, so misrouting complex as simple is the expensive mistake: '
    'when the prompt is borderline, mixed, or you are unsure, answer complex.\n'
    'Examples:\n'
    'what is a REST API -> simple\n'
    'translate "good morning" to french -> simple\n'
    'how do I reverse a list in python -> simple\n'
    'why is my build failing -> complex\n'
    'write a function to parse this log -> complex\n'
    'explain how auth works in this app -> complex\n'
    'add retries to the http client -> complex\n'
    'Reply with exactly one word: complex or simple.')


def ask_text(url, key, data, timeout=6):
    """One request to an OpenAI-compatible endpoint; the reply text, or None on any failure."""
    import urllib.request
    request = urllib.request.Request(url, data=json.dumps(data).encode(), method='POST', headers={
        'Content-Type': 'application/json', 'Authorization': f'Bearer {key}', 'User-Agent': 'omni-ai-cli'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read().decode())['choices'][0]['message']['content'] or ''
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None


def ask_classifier(url, key, data, labels=('complex', 'simple')):
    """One labelling request; the first label found in the reply, None on any failure."""
    answer = (ask_text(url, key, data) or '').lower()
    return next((label for label in labels if label in answer), None)


def free_classifiers():
    """(provider, url, request extras) of the free labelling backends, in the order they are tried."""
    groq = 'https://api.groq.com/openai/v1/chat/completions'
    return [('groq', groq, {'model': model, 'reasoning_effort': effort}) for model, effort in GROQ_CLASSIFIER_MODELS] + [
        ('cerebras', 'https://api.cerebras.ai/v1/chat/completions', {'model': model}) for model in CEREBRAS_CLASSIFIER_MODELS] + [
        # Free OpenRouter models fail now and then (rate limit, empty reply); the second try covers most of it
        ('openrouter', 'https://openrouter.ai/api/v1/chat/completions',
         {'models': CLASSIFIER_MODELS, 'reasoning': {'enabled': False},
          'provider': {'max_price': {'prompt': 0, 'completion': 0}}}),
    ] * 2


def classify_llm(prompt):
    """Groq first (large free quota), then Cerebras, then free OpenRouter, then paid Grok; None when none answers."""
    from ..pii import PIIMasker
    masked, _ = PIIMasker().mask(prompt)
    messages = [{'role': 'system', 'content': CLASSIFIER_SYSTEM},
                {'role': 'user', 'content': f'<prompt>\n{masked[:4000]}\n</prompt>\nLabel (complex or simple):'}]
    backends = free_classifiers() + [
        # Paid: only reached when every free service fails
        ('grok', 'https://api.x.ai/v1/chat/completions', {'model': models()['grok'] or GROK_CLASSIFIER_MODEL}),
    ]
    for provider, url, extra in backends:
        for _, key, _ in config_tool.stored_keys(provider):
            # Room for a short reasoning pass on models that can't switch it off
            label = ask_classifier(url, key, dict(extra, messages=messages, temperature=0, max_tokens=300))
            if label:
                CLASSIFIED_BY[0] = provider
                return label
    return None


CLASSIFIED_BY = ['rules']  # who labelled the last prompt: a classifier service, or the keyword rules


def classify(prompt):
    prompt = os.environ.get('SMART_AI_CLASSIFY_PROMPT') or prompt
    CLASSIFIED_BY[0] = 'rules'
    return classify_llm(prompt) or classify_rules(prompt)


def classify_rules(prompt):
    lower = prompt.lower()
    routing = config().get('routing', {})
    if any(k in lower for k in routing.get('complex_keywords', [])):
        return 'complex'
    # Anything that acts on the workspace needs an agent with file access; OpenRouter only chats
    if re.search(r'\b(projeto|project|repo|reposit[oó]rio|codebase|c[oó]digo|code|arquivos?|files?|pastas?|folder|'
                 r'github|git|commit|push|pull request|pr\b|readme|about|branch|analis|analy[sz]|atuali[sz]|update|'
                 r'edit|alter|mud[ae]|ajust|adicion|add|remov|delet|instal|rod[ae]|run|execut|test)', lower):
        return 'complex'
    if re.search(r'\.(py|js|ts|go|rs|java|kt|dart|swift|rb|sh|yml|yaml|json|toml|sql)', prompt):
        return 'complex'
    if re.search(r'(/|\\|~/|\./)[a-zA-Z]', prompt):
        return 'complex'
    if any(k in lower for k in routing.get('simple_keywords', [])):
        return 'simple'
    # Only a genuinely short general question falls through to simple; anything longer, or that
    # mentions code machinery, routes to Claude. Borderline stays complex (the cheap OpenRouter path
    # is for day-to-day chat only).
    if len(prompt) <= int(routing.get('max_simple_length', 120)) and \
            not re.search(r'function|class|module|package|dependency|error|stack|trace|test|spec|'
                          r'debug|deploy|build|config|script|api|server|database|query', lower):
        return 'simple'
    return 'complex'
