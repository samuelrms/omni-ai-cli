"""Rate-limit detection and memory: parse the tool's own words, remember them, and expire them."""
import json
import os
import re
import time

from .. import config as config_tool
from .base import state, STATE_FILE, LIMIT_TEXT

LIMIT_MESSAGE = re.compile(r"[^\n]*(?:hit your (?:usage|session) limit|quota reached|rate limit|try again at|resets? (?:at|in))[^\n]*", re.I)

LIMIT_IN = re.compile(r'(?:resets?|try again)\s+in\s+((?:\d+\s*[dhms]\s*)+)', re.I)
LIMIT_AT = re.compile(r'(?:resets?(?:\s+at)?|try again at)\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)?\b', re.I)
LIMIT_FALLBACK = 24 * 3600  # no reset time in the message: stop showing it after a day


def limit_until(text, at):
    """When the tool said the limit ends, as epoch seconds; None when the message gives no time."""
    found = LIMIT_IN.search(text)
    if found:
        units = {'d': 86400, 'h': 3600, 'm': 60, 's': 1}
        return at + sum(int(n) * units[u.lower()] for n, u in re.findall(r'(\d+)\s*([dhms])', found.group(1), re.I))
    found = LIMIT_AT.search(text)
    if not found:
        return None
    hour, minute, half = int(found.group(1)), int(found.group(2) or 0), (found.group(3) or '').lower()
    if half == 'pm' and hour < 12:
        hour += 12
    elif half == 'am' and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    seen = time.localtime(at)
    until = time.mktime((seen.tm_year, seen.tm_mon, seen.tm_mday, hour, minute, 0, 0, 0, -1))
    return int(until if until > at else until + 86400)


def remember_limit(key, output):
    """Keep the tool's own words about its limit (e.g. 'resets 7:40pm') for /usage."""
    match = LIMIT_MESSAGE.search(re.sub(r'\\u2019', "'", output))
    data = state()
    limits = data.setdefault('limits', {})
    if match:
        text = match.group(0)
        inner = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', text)  # JSON error: keep only its message
        if inner:
            text = inner.group(1)
        text = ' '.join(text.split())[:220]
        now = int(time.time())
        limits[key] = {'text': text, 'at': now, 'until': limit_until(text, now)}
    else:
        limits.pop(key, None)
    config_tool.write_json(STATE_FILE, data)


def active_limit(item, now=None):
    """The stored limit if it still holds; expired ones (reset time passed) count as gone."""
    if not item:
        return None
    now = time.time() if now is None else now
    until = item.get('until') or limit_until(item['text'], item['at']) or item['at'] + LIMIT_FALLBACK
    return dict(item, until=until) if now < until else None


def hit_limit(output, code):
    """Only Claude's own verdict counts: the final error result, a rejected rate-limit event, or its
    plain error lines. Tool output (files, PR text, command logs) is ignored, so a review that mentions
    "rate limit" can't put an account in cooldown."""
    verdicts = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            verdicts.append(line)  # Claude's stderr/plain error lines
            continue
        if not isinstance(event, dict):
            continue
        if event.get('type') == 'result' and event.get('is_error'):
            verdicts.append(str(event.get('result', '')))
        elif event.get('type') == 'rate_limit_event' and \
                (event.get('rate_limit_info') or {}).get('status') in ('rejected', 'exceeded'):
            return True
    if os.environ.get('SMART_AI_EVENTS') != '1':
        # Text mode: only the tail of a failed run is Claude talking about itself
        verdicts = output.splitlines()[-5:] if code != 0 else []
    return any(LIMIT_TEXT.search(v) for v in verdicts)


def tail_hit_limit(output):
    """Rate-limit check for the non-Claude tools: look only at the last few non-empty lines
    of a failed run (the tool's own error), so tool output mentioning 'rate limit' earlier
    (a log, a diff, a review) can't put the account in cooldown. Mirrors hit_limit's text mode."""
    tail = [line for line in output.splitlines() if line.strip()][-5:]
    return any(LIMIT_TEXT.search(line) for line in tail)
