"""Per-call cost and token accounting written next to the usage log."""
import json
import os
import time
from pathlib import Path

from ..osutils import private_file

def costs_file():
    return Path(os.environ.get('SMART_AI_LOG', '~/.cache/smart-ai/usage.log')).expanduser().with_name('costs.jsonl')


def spend(job, usage):
    """Tokens and, when the provider says it, the cost in US$ of one call (OpenRouter: cost, xAI: ticks of 1e-10)."""
    tokens = sum(int(usage.get(k) or 0) for k in ('prompt_tokens', 'completion_tokens', 'input_tokens', 'output_tokens'))
    if tokens:
        job['tokens'] = job.get('tokens', 0) + tokens
    cost = usage.get('cost')
    if cost is None and usage.get('cost_in_usd_ticks') is not None:
        cost = usage['cost_in_usd_ticks'] / 1e10
    if isinstance(cost, (int, float)) and cost > 0:
        job['cost'] = (job.get('cost') or 0) + cost


def spend_label(job):
    parts = [f"US$ {job['cost']:.4f}"] if job.get('cost') else []
    if job.get('tokens'):
        tokens = job['tokens']
        parts.append(f'{tokens / 1000:.1f}k tokens' if tokens >= 1000 else f'{tokens} tokens')
    return ''.join(' · ' + part for part in parts)


def record_spend(job):
    """One line per finished call, for the day's total in /usage; never the prompt or the answer."""
    if not (job.get('cost') or job.get('tokens')):
        return
    path = costs_file()
    try:
        with private_file(path, 'a', tighten=True) as stream:
            stream.write(json.dumps({'at': time.strftime('%Y-%m-%d %H:%M:%S'), 'tool': job.get('provider', job['engine']),
                                     'cost': job.get('cost') or 0, 'tokens': job.get('tokens', 0)}) + '\n')
    except OSError:
        pass
