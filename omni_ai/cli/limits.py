"""Rate-limit detection and memory: parse the tool's own words, remember them, and expire them."""
from datetime import datetime, timedelta
import json
import os
import re
import time
from zoneinfo import ZoneInfo

from .. import config as config_tool
from .base import STATE_FILE, LIMIT_TEXT

LIMIT_MESSAGE = re.compile(r"[^\n]*(?:hit your (?:usage|session) limit|quota reached|rate limit|try again at|resets? (?:at|in))[^\n]*", re.I)

_DURATION = r"\d+\s*(?:months?|mo|weeks?|wks?|w|days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?![a-z])"
LIMIT_IN = re.compile(r"(?:resets?|try again)\s+in\s+(" + _DURATION + r"(?:\s*(?:,|and)?\s*" + _DURATION + r"){0,4})", re.I)
LIMIT_AT = re.compile(r'(?:resets?(?:\s+at)?|try again at)\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)?\b', re.I)
LIMIT_ON = re.compile(r"(?:resets?|try again)(?:\s+on)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b"
                      r"(?:,?\s*(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)?\b)?", re.I)
LIMIT_ZONE = re.compile(r"\b([A-Za-z_]+/[A-Za-z_]+(?:/[A-Za-z_]+)?|UTC|GMT)\b")
MONTHS = ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec')
LIMIT_FALLBACK = 24 * 3600  # no reset time in the message: stop showing it after a day


def _seconds(amount, unit):
    unit = unit.lower()
    if unit.startswith('mo'):
        return amount * 30 * 86400
    if unit.startswith('w'):
        return amount * 7 * 86400
    return amount * {'d': 86400, 'h': 3600, 'm': 60, 's': 1}[unit[0]]


def _zone(text):
    """The time zone the message names; None (local time) when it names none or an unknown one."""
    found = LIMIT_ZONE.search(text)
    try:
        return ZoneInfo(found.group(1)) if found else None
    except (KeyError, ValueError, OSError):
        return None


def _clock(hour, minute, half):
    hour, minute, half = int(hour or 0), int(minute or 0), (half or '').lower()
    if half == 'pm' and hour < 12:
        hour += 12
    elif half == 'am' and hour == 12:
        hour = 0
    return (hour, minute) if hour <= 23 and minute <= 59 else None


def limit_until(text, at):
    """When the tool said the limit ends, as epoch seconds; None when the message gives no time."""
    found = LIMIT_IN.search(text)
    if found:
        return at + sum(_seconds(int(n), unit) for n, unit in re.findall(r'(\d+)\s*([a-z]+)', found.group(1), re.I))
    seen = datetime.fromtimestamp(at, _zone(text))
    found = LIMIT_ON.search(text)
    if found:
        clock = _clock(found.group(3), found.group(4), found.group(5))
        if not clock:
            return None
        try:
            until = seen.replace(month=MONTHS.index(found.group(1).lower()) + 1, day=int(found.group(2)),
                                 hour=clock[0], minute=clock[1], second=0, microsecond=0)
            if until <= seen:
                until = until.replace(year=until.year + 1)
        except ValueError:
            return None
        return int(until.timestamp())
    found = LIMIT_AT.search(text)
    if not found:
        return None
    clock = _clock(found.group(1), found.group(2), found.group(3))
    if not clock:
        return None
    until = seen.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
    return int((until if until > seen else until + timedelta(days=1)).timestamp())


def _deciding_lines(output):
    """Where a tool talks about itself: its error events and the last plain lines. What it read or wrote
    while working (files, logs, a diff that mentions "rate limit") is not the reason it stopped."""
    lines = []
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            lines.append(line)
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get('type')
        info = event.get('rate_limit_info')
        if kind == 'result' and event.get('is_error'):
            lines.append(str(event.get('result', '')))
        elif kind == 'rate_limit_event' and isinstance(info, dict) and info.get('status') in ('rejected', 'exceeded'):
            lines.append(str(info.get('message') or 'rate limit'))
        elif kind in ('error', 'turn.failed'):
            lines.append(line)
    return lines[-5:]


def remember_limit(key, output):
    """Keep the tool's own words about its limit (e.g. 'resets 7:40pm') for /usage."""
    match = LIMIT_MESSAGE.search(re.sub(r'\\u2019', "'", '\n'.join(_deciding_lines(output))))
    entry = None
    if match:
        text = match.group(0)
        inner = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', text)  # JSON error: keep only its message
        if inner:
            text = inner.group(1)
        text = ' '.join(text.split())[:220]
        now = int(time.time())
        entry = {'text': text, 'at': now, 'until': limit_until(text, now)}

    def store(data):
        limits = data.setdefault('limits', {})
        if entry:
            limits[key] = entry
        else:
            limits.pop(key, None)

    config_tool.update_json(STATE_FILE, store)


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
        if event.get('type') == 'result' and not event.get('is_error') and code == 0:
            return False  # the answer is finished: a warning about usage on the way there is not a limit
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
