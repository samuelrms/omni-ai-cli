"""On-screen chrome: spinner, job output View, replay, titles, orbit mark, splash and welcome boxes."""
import os
import re
import sys
import threading
import time
import shutil

from ..tools import TOOLS
from . import color
from . import text as txt

LOCK = threading.RLock()


class Spinner:
    FRAMES = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'

    def __init__(self):
        self.started = None
        self.label = ''
        self.stop_event = threading.Event()
        self.thread = None
        self.visible = False
        self.embedded = False  # the input box draws it while requests run in the background

    def start(self, label):
        self.label, self.started = label, time.time()
        if self.embedded or not sys.stdout.isatty():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        i = 0
        while not self.stop_event.wait(0.1):
            with LOCK:
                sys.stdout.write('\r\033[K' + color.rgb(color._mix(color.BRAND, (i % 20) / 19), self.FRAMES[i % len(self.FRAMES)]) + ' '
                                 + color.shimmer(self.label, i) + color.dim(f" {elapsed(self.started)} · {txt.t('cancel')}"))
                sys.stdout.flush()
                self.visible = True
            i += 1

    def clear(self):
        if self.visible:
            sys.stdout.write('\r\033[K')
            self.visible = False

    def frame(self):
        return self.FRAMES[int(time.time() * 10) % len(self.FRAMES)]

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join()
            self.thread = None
        with LOCK:
            self.clear()


def elapsed(started):
    seconds = int(time.time() - started)
    return f'{seconds // 60}m {seconds % 60:02d}s' if seconds >= 60 else f'{seconds}s'


SPINNER = Spinner()


def out(text=''):
    with LOCK:
        SPINNER.clear()
        print(text, flush=True)


class View:
    """One running job's output; prefix is only used when several agents run at once."""

    def __init__(self, prefix=''):
        self.prefix = prefix
        self.pending = None  # last action, so its result can be attached under it
        self._streamed = False  # whether a streaming answer has started printing

    def _p(self, text):
        out('\n'.join(self.prefix + line for line in text.split('\n')))

    def stream_line(self, line):
        """One completed line of a streaming answer, printed as it arrives (line-granular keeps the
        chat's input box intact, unlike raw partial writes). The first line carries the answer mark."""
        if not self._streamed:
            self._p('\n' + color.gradient(color.MARK) + ' ' + line)
            self._streamed = True
        else:
            self._p('  ' + line)

    def provider(self, tool, detail):
        self._p(color.dim('  ') + color.rgb(color.TOOL_RGB.get(tool, (6, 182, 212)), '● ' + tool) + (color.dim(' · ' + detail) if detail else ''))

    def action(self, verb, target):
        self._p('\n' + color.green('●') + ' ' + color.bold(verb) + (' ' + txt.short(target, txt.width() - len(verb) - 6) if target else ''))

    def result(self, text, failed=False):
        lines = [l for l in str(text).splitlines() if l.strip()]
        if not lines:
            self._p(color.dim('  └ ' + txt.t('no_output')))
            return
        first = txt.short(lines[0], txt.width() - 8)
        self._p(color.dim('  └ ') + (color.red(first) if failed else color.dim(first)))
        if len(lines) > 1:
            self._p(color.dim('    ' + txt.t('more', n=len(lines) - 1)))

    def answer(self, text):
        body = txt.markdown(text.strip())
        lines = body.split('\n')
        self._p('\n' + color.gradient(color.MARK) + ' ' + lines[0] + ''.join('\n  ' + l for l in lines[1:]))

    def note(self, text):
        if color.LIMIT_WORDS.search(text) or text.lstrip().startswith(('⚠', '✗', '❌')):
            self._p('  ' + color.badge(txt.t('limit') if color.LIMIT_WORDS.search(text) else txt.t('warn'), (234, 179, 8)) + ' '
                    + color.yellow(text.lstrip('⚠✗❌ ')))
        else:
            self._p(color.dim('  ' + text))

    def error(self, text):
        line = txt.short(text, txt.width() - 6)
        if line == getattr(self, 'last_error', None):
            return  # tools often report the same failure twice (item + turn)
        self.last_error = line
        if color.LIMIT_WORDS.search(line):
            self._p('  ' + color.badge(txt.t('limit'), (234, 179, 8)) + ' ' + color.yellow(line))
        else:
            self._p('  ' + color.badge(txt.t('error'), (220, 38, 38)) + ' ' + color.red(line))


def replay(messages, limit=40):
    """Show a resumed conversation the way it looked: your lines with ›, answers with ●."""
    if len(messages) > limit:
        out(color.dim('  ' + txt.t('older', n=len(messages) - limit)))
    for message in messages[-limit:]:
        content = str(message.get('content') or '')  # a saved message may carry None or a non-str
        if message.get('role') == 'user':
            lines = content.split('\n')
            out('\n' + color.rgb((232, 96, 76), '❯') + ' ' + lines[0] + ''.join('\n  ' + l for l in lines[1:]))
        else:
            view = View()
            view.answer(content)
            if message.get('provider'):
                view.note(message['provider'])
    out('')


def attention(title):
    """Bell (dock bounce / tab badge in most terminals) plus a tab title saying it's done."""
    if sys.stdout.isatty():
        with LOCK:
            sys.stdout.write('\a')
            sys.stdout.flush()
        set_title(title)


def set_title(text):
    """Terminal tab/window title (OSC 0); empty text hands the title back to the terminal."""
    if sys.stdout.isatty():
        clean = re.sub(r'[\x00-\x1f\x7f]', ' ', text)[:80]
        with LOCK:
            sys.stdout.write(f'\033]0;{clean}\007')
            sys.stdout.flush()


def clear_screen():
    """Start on a clean screen, like Claude Code and Codex (keeps the scrollback above)."""
    if sys.stdout.isatty():
        with LOCK:
            sys.stdout.write('\033[2J\033[H')
            sys.stdout.flush()


ORBIT_SLOTS = ['top', 'right', 'bottom', 'left']
ORBIT_TOOLS = ['claude', 'codex', 'openrouter', 'agy']


def orbit(step=0):
    """The Omni mark: four tools orbiting the star; 'step' turns them a quarter each time."""
    at = {ORBIT_SLOTS[(i + step) % 4]: color.rgb(color.TOOL_RGB[t], '●') for i, t in enumerate(ORBIT_TOOLS)}
    d = color.dim('·')
    star = color.rgb(color.hue(step / 8), color.MARK) if step else color.gradient(color.MARK)
    return [f' {d}  {at["top"]}  {d} ', f'{at["left"]}   {star}   {at["right"]}', f' {d}  {at["bottom"]}  {d} ']


BIG_TITLE = [
    ' ██████  ███    ███ ███    ██ ██      █████  ██',
    '██    ██ ████  ████ ████   ██ ██     ██   ██ ██',
    '██    ██ ██ ████ ██ ██ ██  ██ ██     ███████ ██',
    '██    ██ ██  ██  ██ ██  ██ ██ ██     ██   ██ ██',
    ' ██████  ██      ██ ██   ████ ██     ██   ██ ██',
]


def splash(seconds=1.5):
    """Full-screen intro on the alternate screen (like vim/less), so nothing is left in the scrollback."""
    if not (color.COLOR and sys.stdout.isatty()) or os.environ.get('SMART_AI_NO_SPLASH') == '1':
        return False
    cols, rows = shutil.get_terminal_size((100, 30))
    big = cols >= len(BIG_TITLE[0]) + 4 and rows >= 16
    title_w = len(BIG_TITLE[0]) if big else len('O M N I   A I')
    tools = color.dim(' · ').join(color.rgb(color.TOOL_RGB[t], t) for t in TOOLS)
    tools_w = len(' · '.join(TOOLS))
    bar_w = min(44, cols - 8)
    height = 3 + 1 + (len(BIG_TITLE) if big else 1) + 1 + 1 + 1 + 1
    top = max(0, (rows - height) // 2)

    def center(text, visible):
        return ' ' * max(0, (cols - visible) // 2) + text

    frames = max(1, int(seconds / 0.06))
    sys.stdout.write('\033[?1049h\033[?25l')  # alternate screen, hide cursor
    try:
        for f in range(frames + 1):
            progress = f / frames
            phase = progress * 1.5
            logo = orbit(f // 3)
            lines = [center(l, 11) for l in logo] + ['']
            if big:
                for r, row in enumerate(BIG_TITLE):
                    lines.append(center(''.join(color.rgb(color.hue(phase + c / 70 + r / 40), ch) if ch != ' ' else ' '
                                                for c, ch in enumerate(row)), title_w))
            else:
                lines.append(center(color.bold(color.rainbow('O M N I   A I', phase)), title_w))
            filled = round(bar_w * progress)
            bar = ''.join(color.rgb(color.hue(phase + i / bar_w / 2), '━') for i in range(filled)) + color.dim('─' * (bar_w - filled))
            lines += ['', center(tools, tools_w), '', center(bar, bar_w)]
            with LOCK:
                sys.stdout.write('\033[H\033[2J' + '\n' * top + '\n'.join(lines))
                sys.stdout.flush()
            time.sleep(seconds / frames)
    except KeyboardInterrupt:
        pass  # Ctrl-C just skips the intro
    finally:
        sys.stdout.write('\033[?25h\033[?1049l')  # cursor back, normal screen
        sys.stdout.flush()
    return True


def welcome(session_id, cwd, engine, model, mode, lang_hint, animate=True):
    w = min(txt.width() - 2, 78)
    tools = color.dim(' · ').join(color.rgb(color.TOOL_RGB[t], t) for t in TOOLS)
    where = txt.home_path(cwd) + color.dim(' · ') + f'{engine} · {model}' + ('' if mode == 'default' else color.dim(' · ') + color.rgb((239, 68, 68) if mode == 'yolo' else (59, 130, 246), mode))
    strip = re.compile(r'\033\[[0-9;]*m')
    border = lambda t: color.rgb((88, 70, 140), t)

    def frame(step, phase):
        logo = orbit(step)
        title = color.bold(color.rainbow('Omni AI', phase)) if phase is not None else color.bold(color.gradient('Omni AI'))
        rows = [f'{logo[0]}     {title}', f'{logo[1]}     {tools}', f'{logo[2]}     {where}', '',
                color.dim(txt.t('session') + ' ') + session_id + color.dim('  ·  ') + color.dim(lang_hint)]
        lines = [border('╭' + '─' * w + '╮')]
        for i, row in enumerate(rows):
            if i >= 3 and len(strip.sub('', row)) > w - 4:
                row = color.dim(txt.short(strip.sub('', row), w - 4))
            lines.append(border('│') + '  ' + row + ' ' * max(0, w - 2 - len(strip.sub('', row))) + border('│'))
        lines.append(border('╰' + '─' * w + '╯'))
        return lines

    lines = frame(0, None)
    out('\n'.join(lines))
    if not (animate and color.COLOR and sys.stdout.isatty()):
        return
    # Short intro: the tools circle the star while the title runs through the colors, then it settles
    frames = 18
    try:
        for f in range(1, frames + 1):
            time.sleep(0.07)
            with LOCK:
                sys.stdout.write(f'\033[{len(lines)}F' + '\n'.join('\033[2K' + l for l in frame(f // 2, f / frames)) + '\n')
                sys.stdout.flush()
        with LOCK:
            sys.stdout.write(f'\033[{len(lines)}F' + '\n'.join('\033[2K' + l for l in frame(0, None)) + '\n')
            sys.stdout.flush()
    except KeyboardInterrupt:
        pass
