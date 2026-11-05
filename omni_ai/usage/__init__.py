"""Usage and limits for every tool/account.

Split into submodules: format, cache, providers. Public names are re-exported
here so omni_ai.usage.<name> keeps working.
"""
import json
import os
import select
import subprocess
import sys
import urllib.request

from .. import config as config_tool

from .format import window_label, bar, resets_in, clock
from .cache import CACHE_FILE, FRESH, _f, thresholds, load_cache, store, peak, read, alerts
from .providers import (
    PLANS, plan_name, claude_account, codex_status, openrouter_credits, codex_accounts,
    CLAUDE_USAGE_URL, CLAUDE_WINDOWS, claude_token, claude_limits, claude_windows,
    claude_reading, codex_reading,
)

def snapshot(claude_profiles, codex_accounts, openrouter_keys):
    """Everything /usage shows, fetched at once: one thread per account."""
    from concurrent.futures import ThreadPoolExecutor
    jobs = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for name, conf in claude_profiles:
            jobs[('claude-who', name)] = pool.submit(claude_account, conf)
            jobs[('claude', name)] = pool.submit(read, f'claude:{name}', lambda conf=conf: claude_reading(conf), 60)
        for name, env in codex_accounts:
            jobs[('codex', name)] = pool.submit(read, f'codex:{name}', lambda env=env: codex_reading(env), 30)
        for name, key, _ in openrouter_keys or []:
            jobs[('openrouter', name)] = pool.submit(openrouter_credits, key)

        def _result(future):
            try:
                return future.result()
            except Exception:
                return None  # one bad account must not blank the whole /usage screen
        return {k: _result(f) for k, f in jobs.items()}
