"""How the chat looks: welcome box, input box with status bar, compact tool actions, markdown answers, spinner."""
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'vendor'))

COLOR = sys.stdout.isatty() and not os.environ.get('NO_COLOR')


def _c(code):
    return (lambda text: f'\033[{code}m{text}\033[0m') if COLOR else (lambda text: text)


bold, dim, italic = _c('1'), _c('2'), _c('3')
red, green, yellow, blue, magenta, cyan = _c('31'), _c('32'), _c('33'), _c('34'), _c('35'), _c('36')
TOOL_COLOR = {'claude': _c('38;5;173'), 'codex': green, 'agy': magenta, 'openrouter': yellow}
LOCK = threading.RLock()


def width():
    return max(40, shutil.get_terminal_size((100, 30)).columns)


def short(text, limit):
    text = ' '.join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def home_path(path):
    path = str(path)
    home = str(Path.home())
    return '~' + path[len(home):] if path.startswith(home) else path


def markdown(text):
    """Small terminal markdown: headings, bold, inline code, bullets, fenced code."""
    out, fence = [], False
    for line in text.splitlines():
        if line.strip().startswith('```'):
            fence = not fence
            continue
        if fence:
            out.append(dim('  ' + line))
            continue
        heading = re.match(r'^(#{1,6})\s+(.*)', line)
        if heading:
            out.append(bold(heading.group(2)))
            continue
        line = re.sub(r'^(\s*)[-*]\s+', lambda m: m.group(1) + '• ', line)
        line = re.sub(r'\*\*(.+?)\*\*', lambda m: bold(m.group(1)), line)
        line = re.sub(r'`([^`]+)`', lambda m: cyan(m.group(1)), line)
        out.append(line)
    return '\n'.join(out)


class Spinner:
    FRAMES = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'

    def __init__(self):
        self.started = None
        self.label = ''
        self.stop_event = threading.Event()
        self.thread = None
        self.visible = False

    def start(self, label):
        self.label, self.started = label, time.time()
        if not sys.stdout.isatty():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        i = 0
        while not self.stop_event.wait(0.1):
            with LOCK:
                sys.stdout.write('\r\033[K' + cyan(self.FRAMES[i % len(self.FRAMES)]) + ' '
                                 + dim(f'{self.label} {elapsed(self.started)} · Ctrl-C cancela'))
                sys.stdout.flush()
                self.visible = True
            i += 1

    def clear(self):
        if self.visible:
            sys.stdout.write('\r\033[K')
            self.visible = False

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join()
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

    def _p(self, text):
        out('\n'.join(self.prefix + line for line in text.split('\n')))

    def provider(self, tool, detail):
        self._p(dim('  ') + TOOL_COLOR.get(tool, cyan)('◆ ' + tool) + (dim(' · ' + detail) if detail else ''))

    def action(self, verb, target):
        self._p('\n' + green('●') + ' ' + bold(verb) + (' ' + short(target, width() - len(verb) - 6) if target else ''))

    def result(self, text, failed=False):
        lines = [l for l in str(text).splitlines() if l.strip()]
        if not lines:
            self._p(dim('  └ (sem saída)'))
            return
        first = short(lines[0], width() - 8)
        self._p(dim('  └ ') + (red(first) if failed else dim(first)))
        if len(lines) > 1:
            self._p(dim(f'    + {len(lines) - 1} linhas · /trace para ver tudo'))

    def answer(self, text):
        body = markdown(text.strip())
        lines = body.split('\n')
        self._p('\n' + '● ' + lines[0] + ''.join('\n  ' + l for l in lines[1:]))

    def note(self, text):
        self._p(dim('  ' + text))

    def error(self, text):
        line = short(text, width() - 6)
        if line == getattr(self, 'last_error', None):
            return  # tools often report the same failure twice (item + turn)
        self.last_error = line
        self._p(red('  ✗ ' + line))


def clear_screen():
    """Start on a clean screen, like Claude Code and Codex (keeps the scrollback above)."""
    if sys.stdout.isatty():
        with LOCK:
            sys.stdout.write('\033[2J\033[H')
            sys.stdout.flush()


def welcome(session_id, cwd, engine, model, mode, lang_hint):
    w = min(width() - 2, 78)
    rows = [bold('✻ Omni AI'), '',
            dim('pasta   ') + home_path(cwd),
            dim('modelo  ') + f'{engine} · {model}' + ('' if mode == 'default' else dim(' · ') + yellow(mode)),
            dim('sessão  ') + session_id, '', dim(lang_hint)]
    strip = re.compile(r'\033\[[0-9;]*m')
    out(cyan('╭' + '─' * w + '╮'))
    for row in rows:
        if len(strip.sub('', row)) > w - 2:
            row = dim(short(strip.sub('', row), w - 2))
        pad = w - 2 - len(strip.sub('', row))
        out(cyan('│') + ' ' + row + ' ' * max(0, pad) + ' ' + cyan('│'))
    out(cyan('╰' + '─' * w + '╯'))


class InputBox:
    """Bottom input like Claude Code/Codex: rule above, placeholder, status bar below, Tab completion."""

    def __init__(self, history_file, commands, status, placeholder, hints):
        self.status, self.placeholder, self.hints = status, placeholder, hints
        self.session = None
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.completion import Completer, Completion
            from prompt_toolkit.formatted_text import HTML
            from prompt_toolkit.history import FileHistory
            from prompt_toolkit.key_binding import KeyBindings
            from prompt_toolkit.styles import Style
        except ImportError:
            return
        self.HTML = HTML

        class Commands(Completer):
            def get_completions(self, document, complete_event):
                text = document.text_before_cursor
                if not text.startswith('/') or ' ' in text:
                    return
                for name, meta in commands():
                    if name.startswith(text):
                        yield Completion(name, start_position=-len(text), display_meta=short(meta, 60))

        keys = KeyBindings()

        @keys.add('escape', 'enter')
        @keys.add('c-j')
        def _(event):
            event.current_buffer.insert_text('\n')

        Path(history_file).parent.mkdir(parents=True, exist_ok=True)
        self.session = PromptSession(
            history=FileHistory(str(history_file)), completer=Commands(), complete_while_typing=True,
            key_bindings=keys, multiline=False, mouse_support=False,
            style=Style.from_dict({'bottom-toolbar': 'noreverse #8a8a8a', 'rule': '#4a4a4a',
                                   'arrow': '#d7875f bold', 'placeholder': '#6c6c6c',
                                   'completion-menu.completion': 'bg:#262626 #d0d0d0',
                                   'completion-menu.completion.current': 'bg:#444444 #ffffff bold',
                                   'completion-menu.meta.completion': 'bg:#262626 #808080'}))

    def ask_args(self, command, hint, options):
        """Second prompt for a skill's arguments: Tab offers its options, any text is accepted, Enter alone skips."""
        from prompt_toolkit.completion import WordCompleter
        message = self.HTML(f'  <arrow>{escape(command)}</arrow> ')
        return self.session.prompt(message, completer=WordCompleter(options, sentence=True),
                                   placeholder=self.HTML(f'<placeholder>{escape(hint)} · Tab mostra opções · Enter sem nada pula</placeholder>'),
                                   bottom_toolbar=None)

    def ask(self):
        """Returns the typed text; raises EOFError (Ctrl-D) or KeyboardInterrupt (Ctrl-C)."""
        if not self.session:
            return input('\n› ')
        rule = '─' * width()
        message = self.HTML(f'<rule>{rule}</rule>\n<arrow>›</arrow> ')
        status = self.status

        def toolbar():
            left = status()
            right = self.hints
            gap = max(2, width() - len(left) - len(right) - 1)
            return self.HTML(f'<rule>{rule}</rule>\n {escape(left)}{" " * gap}{escape(right)}')
        print()
        return self.session.prompt(message, bottom_toolbar=toolbar,
                                   placeholder=self.HTML(f'<placeholder>{escape(self.placeholder)}</placeholder>'))


def escape(text):
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
