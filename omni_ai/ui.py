"""How the chat looks: welcome box, input box with status bar, compact tool actions, markdown answers, spinner."""
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import time

from .tools import TOOLS

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'vendor'))

SECRET = re.compile(r'^\s*/key\b|sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{20,}|npm_[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}')
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
LOCK = threading.RLock()
LANG = ['pt']  # set by the chat; screen texts follow it
TEXT = {
    'cancel': {'pt': 'Ctrl-C cancela', 'en': 'Ctrl-C cancels', 'es': 'Ctrl-C cancela', 'fr': 'Ctrl-C annule'},
    'no_output': {'pt': '(sem saída)', 'en': '(no output)', 'es': '(sin salida)', 'fr': '(aucune sortie)'},
    'more': {'pt': '+ {n} linhas · /trace para ver tudo', 'en': '+ {n} lines · /trace to see all',
             'es': '+ {n} líneas · /trace para ver todo', 'fr': '+ {n} lignes · /trace pour tout voir'},
    'older': {'pt': '… {n} mensagens anteriores · /history mostra tudo', 'en': '… {n} earlier messages · /history shows all',
              'es': '… {n} mensajes anteriores · /history muestra todo', 'fr': '… {n} messages précédents · /history montre tout'},
    'session': {'pt': 'sessão', 'en': 'session', 'es': 'sesión', 'fr': 'session'},
    'limit': {'pt': 'LIMITE', 'en': 'LIMIT', 'es': 'LÍMITE', 'fr': 'LIMITE'},
    'error': {'pt': 'ERRO', 'en': 'ERROR', 'es': 'ERROR', 'fr': 'ERREUR'},
    'warn': {'pt': 'AVISO', 'en': 'WARNING', 'es': 'AVISO', 'fr': 'AVERTISSEMENT'},
    'file': {'pt': 'arquivo', 'en': 'file', 'es': 'archivo', 'fr': 'fichier'},
    'args': {'pt': 'Tab mostra opções · Enter sem nada pula', 'en': 'Tab shows options · Enter alone skips',
             'es': 'Tab muestra opciones · Enter vacío omite', 'fr': 'Tab montre les options · Entrée seule ignore'},
}


def t(key, **values):
    return TEXT[key].get(LANG[0], TEXT[key]['pt']).format(**values)


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
                sys.stdout.write('\r\033[K' + rgb(_mix(BRAND, (i % 20) / 19), self.FRAMES[i % len(self.FRAMES)]) + ' '
                                 + shimmer(self.label, i) + dim(f" {elapsed(self.started)} · {t('cancel')}"))
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

    def _p(self, text):
        out('\n'.join(self.prefix + line for line in text.split('\n')))

    def provider(self, tool, detail):
        self._p(dim('  ') + rgb(TOOL_RGB.get(tool, (6, 182, 212)), '● ' + tool) + (dim(' · ' + detail) if detail else ''))

    def action(self, verb, target):
        self._p('\n' + green('●') + ' ' + bold(verb) + (' ' + short(target, width() - len(verb) - 6) if target else ''))

    def result(self, text, failed=False):
        lines = [l for l in str(text).splitlines() if l.strip()]
        if not lines:
            self._p(dim('  └ ' + t('no_output')))
            return
        first = short(lines[0], width() - 8)
        self._p(dim('  └ ') + (red(first) if failed else dim(first)))
        if len(lines) > 1:
            self._p(dim('    ' + t('more', n=len(lines) - 1)))

    def answer(self, text):
        body = markdown(text.strip())
        lines = body.split('\n')
        self._p('\n' + gradient(MARK) + ' ' + lines[0] + ''.join('\n  ' + l for l in lines[1:]))

    def note(self, text):
        if LIMIT_WORDS.search(text) or text.lstrip().startswith(('⚠', '✗', '❌')):
            self._p('  ' + badge(t('limit') if LIMIT_WORDS.search(text) else t('warn'), (234, 179, 8)) + ' '
                    + yellow(text.lstrip('⚠✗❌ ')))
        else:
            self._p(dim('  ' + text))

    def error(self, text):
        line = short(text, width() - 6)
        if line == getattr(self, 'last_error', None):
            return  # tools often report the same failure twice (item + turn)
        self.last_error = line
        if LIMIT_WORDS.search(line):
            self._p('  ' + badge(t('limit'), (234, 179, 8)) + ' ' + yellow(line))
        else:
            self._p('  ' + badge(t('error'), (220, 38, 38)) + ' ' + red(line))


def replay(messages, limit=40):
    """Show a resumed conversation the way it looked: your lines with ›, answers with ●."""
    if len(messages) > limit:
        out(dim('  ' + t('older', n=len(messages) - limit)))
    for message in messages[-limit:]:
        if message.get('role') == 'user':
            lines = message.get('content', '').split('\n')
            out('\n' + rgb((232, 96, 76), '❯') + ' ' + lines[0] + ''.join('\n  ' + l for l in lines[1:]))
        else:
            view = View()
            view.answer(message.get('content', ''))
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
    at = {ORBIT_SLOTS[(i + step) % 4]: rgb(TOOL_RGB[t], '●') for i, t in enumerate(ORBIT_TOOLS)}
    d = dim('·')
    star = rgb(hue(step / 8), MARK) if step else gradient(MARK)
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
    if not (COLOR and sys.stdout.isatty()) or os.environ.get('SMART_AI_NO_SPLASH') == '1':
        return False
    cols, rows = shutil.get_terminal_size((100, 30))
    big = cols >= len(BIG_TITLE[0]) + 4 and rows >= 16
    title_w = len(BIG_TITLE[0]) if big else len('O M N I   A I')
    tools = dim(' · ').join(rgb(TOOL_RGB[t], t) for t in TOOLS)
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
                    lines.append(center(''.join(rgb(hue(phase + c / 70 + r / 40), ch) if ch != ' ' else ' '
                                                for c, ch in enumerate(row)), title_w))
            else:
                lines.append(center(bold(rainbow('O M N I   A I', phase)), title_w))
            filled = round(bar_w * progress)
            bar = ''.join(rgb(hue(phase + i / bar_w / 2), '━') for i in range(filled)) + dim('─' * (bar_w - filled))
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
    w = min(width() - 2, 78)
    tools = dim(' · ').join(rgb(TOOL_RGB[t], t) for t in TOOLS)
    where = home_path(cwd) + dim(' · ') + f'{engine} · {model}' + ('' if mode == 'default' else dim(' · ') + rgb((239, 68, 68) if mode == 'yolo' else (59, 130, 246), mode))
    strip = re.compile(r'\033\[[0-9;]*m')
    border = lambda t: rgb((88, 70, 140), t)

    def frame(step, phase):
        logo = orbit(step)
        title = bold(rainbow('Omni AI', phase)) if phase is not None else bold(gradient('Omni AI'))
        rows = [f'{logo[0]}     {title}', f'{logo[1]}     {tools}', f'{logo[2]}     {where}', '',
                dim(t('session') + ' ') + session_id + dim('  ·  ') + dim(lang_hint)]
        lines = [border('╭' + '─' * w + '╮')]
        for i, row in enumerate(rows):
            if i >= 3 and len(strip.sub('', row)) > w - 4:
                row = dim(short(strip.sub('', row), w - 4))
            lines.append(border('│') + '  ' + row + ' ' * max(0, w - 2 - len(strip.sub('', row))) + border('│'))
        lines.append(border('╰' + '─' * w + '╯'))
        return lines

    lines = frame(0, None)
    out('\n'.join(lines))
    if not (animate and COLOR and sys.stdout.isatty()):
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


def fuzzy(query, text):
    """Rank for 'query' inside 'text': 0 prefix, 1 substring, 2 letters in order, None no match."""
    query, text = query.lower(), text.lower()
    if text.startswith(query):
        return 0
    if query in text:
        return 1
    position = 0
    for char in query:
        position = text.find(char, position) + 1
        if not position:
            return None
    return 2


SKIP_DIRS = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', 'dist', 'build', '.next', '.cache',
             'target', '.idea', '.vscode', 'coverage', 'vendor'}


def project_files(cwd, limit=4000):
    """Files for @mentions: git's list when it's a repo (respects .gitignore), else a bounded walk."""
    import subprocess
    try:
        out = subprocess.run(['git', '-C', str(cwd), 'ls-files', '--cached', '--others', '--exclude-standard'],
                             capture_output=True, text=True, timeout=3)
        if out.returncode == 0 and out.stdout:
            return out.stdout.splitlines()[:limit]
    except (OSError, subprocess.TimeoutExpired):
        pass
    found = []
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith('.')]
        for name in files:
            found.append(os.path.relpath(os.path.join(root, name), cwd))
            if len(found) >= limit:
                return found
    return found


class InputBox:
    """Bottom input like Claude Code/Codex: rule above, placeholder, status bar below, Tab completion."""

    def __init__(self, history_file, commands, status, placeholder, hints, arguments=None, cwd=None, mode=None, activity=None):
        self.status, self.placeholder, self.hints, self.mode, self.activity = status, placeholder, hints, mode, activity
        self.session = None
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
            from prompt_toolkit.completion import Completer, Completion
            from prompt_toolkit.formatted_text import HTML
            from prompt_toolkit.history import FileHistory
            from prompt_toolkit.key_binding import KeyBindings
            from prompt_toolkit.styles import Style
        except ImportError:
            return
        self.HTML = HTML

        files_cache = {}

        def ranked(query, items):
            scored = [(fuzzy(query, value), value, meta) for value, meta in items]
            return sorted(((r, v, m) for r, v, m in scored if r is not None), key=lambda x: (x[0], len(x[1])))

        class Smart(Completer):
            """/commands and skills (fuzzy), their arguments, and @file mentions from the project."""
            def get_completions(self, document, complete_event):
                text = document.text_before_cursor
                word = document.get_word_before_cursor(WORD=True)
                if word.startswith('@') and cwd:
                    folder = cwd()
                    if folder not in files_cache:
                        files_cache[folder] = project_files(folder)
                    for _, path, _ in ranked(word[1:], [(f, '') for f in files_cache[folder]])[:40]:
                        yield Completion('@' + path, start_position=-len(word), display_meta=t('file'))
                    return
                if not text.startswith('/'):
                    return
                if ' ' not in text:
                    for _, name, meta in ranked(text[1:], [(n[1:], m) for n, m in commands()])[:60]:
                        yield Completion('/' + name, start_position=-len(text), display_meta=short(meta, 70))
                    return
                if arguments:
                    command, *done = text.split()
                    if text.endswith(' '):
                        current = ''
                    else:
                        current = done.pop() if done else ''
                    for _, value, meta in ranked(current, arguments(command, done)):
                        yield Completion(value, start_position=-len(current), display_meta=short(meta, 60))

        keys = KeyBindings()
        from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
        from prompt_toolkit.keys import Keys
        # Shift+Enter arrives as one of these in terminals that can tell it apart (iTerm2, Ghostty, Kitty,
        # WezTerm, VS Code); treat it like Ctrl-J, which inserts a new line
        for sequence in ('\x1b[13;2u', '\x1b[27;2;13~', '\x1b[13;2~'):
            ANSI_SEQUENCES[sequence] = Keys.ControlJ

        @keys.add('escape', 'enter')
        @keys.add('c-j')
        def _(event):
            event.current_buffer.insert_text('\n')

        @keys.add('enter')
        def _(event):
            buffer = event.current_buffer
            if buffer.complete_state and buffer.complete_state.current_completion:
                buffer.apply_completion(buffer.complete_state.current_completion)
                return
            # A trailing backslash continues on the next line, like in a shell and in Claude Code
            if buffer.document.text_before_cursor.endswith('\\'):
                buffer.delete_before_cursor(1)
                buffer.insert_text('\n')
                return
            buffer.validate_and_handle()

        class PrivateHistory(FileHistory):
            """Arrow-key history that never writes secrets: no /key lines, nothing that looks like a key."""
            def store_string(self, string):
                if not SECRET.search(string):
                    super().store_string(string)

        Path(history_file).parent.mkdir(parents=True, exist_ok=True)
        Path(history_file).touch(mode=0o600, exist_ok=True)
        os.chmod(history_file, 0o600)
        self.session = PromptSession(
            history=PrivateHistory(str(history_file)), completer=Smart(), complete_while_typing=True,
            auto_suggest=AutoSuggestFromHistory(), enable_history_search=False,
            key_bindings=keys, multiline=False, mouse_support=False,
            style=Style.from_dict({'bottom-toolbar': 'noreverse #8a8a8a', 'rule': '#4a4a4a',
                                   'arrow': '#e8604c bold', 'placeholder': '#6c6c6c', 'auto-suggestion': '#5f5f5f',
                                   'completion-menu.completion': 'bg:#262626 #d0d0d0',
                                   'completion-menu.completion.current': 'bg:#444444 #ffffff bold',
                                   'completion-menu.meta.completion': 'bg:#262626 #808080',
                                   'running': '#e8604c', 'queued': '#8a8a8a'}))

    def ask_args(self, command, hint, options):
        """Second prompt for a skill's arguments: Tab offers its options, any text is accepted, Enter alone skips."""
        from prompt_toolkit.completion import WordCompleter
        message = self.HTML(f'  <arrow>{escape(command)}</arrow> ')
        return self.session.prompt(message, completer=WordCompleter(options, sentence=True),
                                   placeholder=self.HTML(f'<placeholder>{escape(hint)} · {escape(t("args"))}</placeholder>'),
                                   bottom_toolbar=None)

    def ask(self):
        """Returns the typed text; raises EOFError (Ctrl-D) or KeyboardInterrupt (Ctrl-C)."""
        if not self.session:
            return input('\n› ')
        rule = '─' * width()
        activity = self.activity

        def message():
            # What runs and what waits in line sits right above the input, redrawn while you type
            lines = activity() if activity else []
            above = ''.join(f'{line}\n' for line in lines)
            return self.HTML(f'{above}<rule>{rule}</rule>\n<arrow>❯</arrow> ')
        status = self.status

        def toolbar():
            left = status()
            right = self.hints() if callable(self.hints) else self.hints
            mode = self.mode() if self.mode else 'default'
            chip = f'<style bg="{MODE_COLORS[mode]}" fg="#000000"><b> {mode.upper()} </b></style> ' if mode in MODE_COLORS else ''
            # "✦ Omni AI" keeps cycling through the colors while you type
            phase = time.time() / 4
            brand = ''.join(f'<style fg="#{r:02x}{g:02x}{b:02x}">{escape(ch)}</style>'
                            for i, ch in enumerate(MARK + ' Omni AI') for r, g, b in [hue(phase + i / 22)])
            left = left[len('Omni AI'):].lstrip(' ·') if left.startswith('Omni AI') else left
            visible = len(MARK + ' Omni AI · ') + len(left) + (len(mode) + 3 if chip else 0)
            gap = max(2, width() - visible - len(right) - 1)
            return self.HTML(f'<rule>{rule}</rule>\n {brand}<rule> · </rule>{chip}{escape(left)}{" " * gap}{escape(right)}')
        print()
        sys.stdout.write('\033[>4;1m')  # xterm modifyOtherKeys=1: Shift+Enter becomes distinguishable
        sys.stdout.flush()
        try:
            return self.session.prompt(message, bottom_toolbar=toolbar, refresh_interval=0.12,
                                       placeholder=self.HTML(f'<placeholder>{escape(self.placeholder() if callable(self.placeholder) else self.placeholder)}</placeholder>'))
        finally:
            sys.stdout.write('\033[>4m')
            sys.stdout.flush()


def escape(text):
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
