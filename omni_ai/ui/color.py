"""Terminal color: ANSI toggles, RGB/truecolor, brand gradient, rainbow, badges, shimmer."""
import os
import re
import sys

from ..tools import TOOLS

COLOR = sys.stdout.isatty() and not os.environ.get('NO_COLOR')


def _c(code):
    return (lambda text: f'\033[{code}m{text}\033[0m') if COLOR else (lambda text: text)


bold, dim, italic = _c('1'), _c('2'), _c('3')
TRUECOLOR = os.environ.get('COLORTERM', '').lower() in ('truecolor', '24bit')
BRAND = [(245, 165, 36), (232, 96, 76), (139, 92, 246)]  # amber → coral → violet
MODE_COLORS = {'plan': '#3b82f6', 'edits': '#06b6d4', 'auto': '#22c55e', 'yolo': '#ef4444'}


def rgb(color, text, background=False):
    if not COLOR:
        return text
    r, g, b = color
    if TRUECOLOR:
        code = f'{48 if background else 38};2;{r};{g};{b}'
    else:  # 256-color cube approximation
        code = f'{48 if background else 38};5;{16 + 36 * round(r / 51) + 6 * round(g / 51) + round(b / 51)}'
    return f'\033[{code}m{text}\033[0m'


def _mix(stops, t):
    t = max(0.0, min(1.0, t)) * (len(stops) - 1)
    i = min(int(t), len(stops) - 2)
    f = t - i
    return tuple(round(a + (b - a) * f) for a, b in zip(stops[i], stops[i + 1]))


def hue(position):
    """Bright rainbow color for a position on the color wheel (0..1)."""
    import colorsys
    r, g, b = colorsys.hsv_to_rgb(position % 1.0, 0.62, 1.0)
    return round(r * 255), round(g * 255), round(b * 255)


def rainbow(text, phase):
    """Text whose colors slide along the wheel as 'phase' grows: the RGB-cycling title."""
    n = max(1, len(text))
    return ''.join(rgb(hue(phase + i / (n * 2.5)), ch) for i, ch in enumerate(text))


def gradient(text, stops=BRAND):
    """Brand gradient across the characters of a short text."""
    if not COLOR:
        return text
    n = max(1, len(text) - 1)
    return ''.join(rgb(_mix(stops, i / n), ch) for i, ch in enumerate(text))


def badge(label, color):
    """Small solid label that draws the eye: LIMITE, ERRO."""
    if not COLOR:
        return f'[{label}]'
    return '\033[1;30m' + rgb(color, f' {label} ', background=True)


LIMIT_WORDS = re.compile(r'limit|quota|cota|cooldown|rate.?limit|resets?|try again', re.I)
red, green, yellow, blue, magenta, cyan = _c('31'), _c('32'), _c('33'), _c('34'), _c('35'), _c('36')
TOOL_RGB = {name: tool.rgb for name, tool in TOOLS.items()}
MARK = '✦'  # Omni's mark: the star at the center of the orbit


def shimmer(text, tick):
    """A bright band sliding across dim text, like a light passing over it."""
    if not COLOR:
        return text
    center = tick % (len(text) + 8) - 4
    out_ = []
    for i, ch in enumerate(text):
        glow = max(0.0, 1 - abs(i - center) / 3)
        base = 110 + round(145 * glow)
        out_.append(rgb(_mix(BRAND, i / max(1, len(text) - 1)), ch) if glow > 0.6 else rgb((base, base, base), ch))
    return ''.join(out_)
