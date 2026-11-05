"""Small formatting helpers for the usage screen: window labels, bars, reset times."""
import time
from datetime import datetime

def window_label(minutes, label=None):
    if label:
        return label
    if minutes and minutes % 10080 == 0:
        return 'semana'
    if minutes and minutes % 1440 == 0:
        return f'{minutes // 1440} dias'
    return f'{minutes // 60}h' if minutes else 'crédito'


def bar(percent, width=20):
    filled = round(max(0.0, min(100.0, percent)) / 100 * width)
    return '█' * filled + '░' * (width - filled)


def resets_in(epoch, now=None):
    seconds = int(epoch - (time.time() if now is None else now))
    if seconds <= 0:
        return 'agora'
    days, rest = divmod(seconds, 86400)
    return f'{days}d{rest // 3600:02d}h' if days else f'{rest // 3600}h{rest % 3600 // 60:02d}m' if rest >= 3600 else f'{rest // 60}m'


def clock(epoch):
    """Reset time in local time: '14:00' today/tomorrow, 'seg 14:00' within a week."""
    moment = time.localtime(epoch)
    if epoch - time.time() < 86400:
        return time.strftime('%H:%M', moment)
    return ('seg', 'ter', 'qua', 'qui', 'sex', 'sáb', 'dom')[moment.tm_wday] + time.strftime(' %H:%M', moment)
