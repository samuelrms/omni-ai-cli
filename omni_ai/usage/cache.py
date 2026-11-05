"""Usage reading cache and thresholds: freshness, peak, alerts."""
import json
import os
import time
from pathlib import Path

from .. import config as config_tool
from .format import window_label, resets_in

CACHE_FILE = config_tool.CONFIG_DIR / 'usage-cache.json'
FRESH = {'claude': 120, 'codex': 300}  # seconds a reading stays good for the router

def _f(value, default=0.0):
    """A float from a maybe-string/maybe-bad API value, never raising."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def thresholds():
    """routing.usage_skip_percent: the router skips an account at or above it (default 100).
    routing.usage_alert_percent: warn once per window when an account passes it (default 80)."""
    routing = config_tool.profiles().get('routing', {})
    return _f(routing.get('usage_skip_percent', 100), 100.0), _f(routing.get('usage_alert_percent', 80), 80.0)


def load_cache():
    return config_tool.read_json(CACHE_FILE, {})


def store(key, reading):
    def _mutate(data):
        data.setdefault('readings', {})[key] = dict(reading, at=int(time.time()))
    config_tool.update_json(CACHE_FILE, _mutate)


def peak(windows):
    """Highest % among windows that have not reset yet."""
    now = time.time()
    return max((w['used'] for w in windows or [] if not w.get('resets') or w['resets'] > now), default=0.0)


def read(key, fetch, max_age=None):
    """Reading for an account: from the cache while fresh, otherwise fetched (and cached). When the tool
    refuses the check (too many in a row) or fails, the last reading comes back marked stale, and no new
    check is made until the wait the tool asked for is over."""
    tool = key.split(':')[0]
    data = load_cache()
    cached = data.get('readings', {}).get(key)
    now = time.time()
    if cached and now - cached['at'] < (FRESH.get(tool, 120) if max_age is None else max_age):
        return cached
    stale = dict(cached, stale=True) if cached else None
    if data.get('backoff', {}).get(key, 0) > now:
        return stale
    reading = fetch()
    if reading and 'error' not in reading:
        store(key, reading)
        return reading
    if reading and reading.get('retry'):
        config_tool.update_json(CACHE_FILE,
                                lambda data: data.setdefault('backoff', {}).__setitem__(key, int(now + reading['retry'])))
    return stale


def alerts(key, reading, label):
    """Warning lines for windows past the alert threshold, each said once per reset."""
    _, alert = thresholds()
    windows = (reading or {}).get('windows', [])
    if not windows:
        return []
    lines = []

    def _mutate(data):
        said = data.setdefault('alerted', {})
        for window in windows:
            mark = f"{key}:{window['minutes']}:{window.get('label') or ''}:{int(window.get('resets') or 0)}"
            if window['used'] >= alert and mark not in said:
                said[mark] = int(time.time())
                reset = f", reseta em {resets_in(window['resets'])}" if window.get('resets') else ''
                lines.append(f"{label}: {window['used']:.0f}% da janela "
                             f"{window_label(window['minutes'], window.get('label'))} usada{reset}")

    config_tool.update_json(CACHE_FILE, _mutate)
    return lines
