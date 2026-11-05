"""Bottom input box (prompt_toolkit): rule, placeholder, status bar, Tab completion, @file mentions."""
import os
import sys
import time
from pathlib import Path

from . import color
from . import text as txt

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
            scored = [(txt.fuzzy(query, value), value, meta) for value, meta in items]
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
                        yield Completion('@' + path, start_position=-len(word), display_meta=txt.t('file'))
                    return
                if not text.startswith('/'):
                    return
                if ' ' not in text:
                    for _, name, meta in ranked(text[1:], [(n[1:], m) for n, m in commands()])[:60]:
                        yield Completion('/' + name, start_position=-len(text), display_meta=txt.short(meta, 70))
                    return
                if arguments:
                    command, *done = text.split()
                    if text.endswith(' '):
                        current = ''
                    else:
                        current = done.pop() if done else ''
                    for _, value, meta in ranked(current, arguments(command, done)):
                        yield Completion(value, start_position=-len(current), display_meta=txt.short(meta, 60))

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
                before = buffer.text
                buffer.apply_completion(buffer.complete_state.current_completion)
                # picking what is already typed (e.g. "/clear" → "/clear") sends it instead of asking for a second Enter
                if buffer.text != before:
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
                if not txt.SECRET.search(string):
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
        message = self.HTML(f'  <arrow>{txt.escape(command)}</arrow> ')
        return self.session.prompt(message, completer=WordCompleter(options, sentence=True),
                                   placeholder=self.HTML(f'<placeholder>{txt.escape(hint)} · {txt.escape(txt.t("args"))}</placeholder>'),
                                   bottom_toolbar=None)

    def ask(self):
        """Returns the typed text; raises EOFError (Ctrl-D) or KeyboardInterrupt (Ctrl-C)."""
        if not self.session:
            return input('\n› ')
        rule = '─' * txt.width()
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
            chip = f'<style bg="{color.MODE_COLORS[mode]}" fg="#000000"><b> {mode.upper()} </b></style> ' if mode in color.MODE_COLORS else ''
            # "✦ Omni AI" keeps cycling through the colors while you type
            phase = time.time() / 4
            brand = ''.join(f'<style fg="#{r:02x}{g:02x}{b:02x}">{txt.escape(ch)}</style>'
                            for i, ch in enumerate(color.MARK + ' Omni AI') for r, g, b in [color.hue(phase + i / 22)])
            left = left[len('Omni AI'):].lstrip(' ·') if left.startswith('Omni AI') else left
            visible = len(color.MARK + ' Omni AI · ') + len(left) + (len(mode) + 3 if chip else 0)
            gap = max(2, txt.width() - visible - len(right) - 1)
            return self.HTML(f'<rule>{rule}</rule>\n {brand}<rule> · </rule>{chip}{txt.escape(left)}{" " * gap}{txt.escape(right)}')
        print()
        sys.stdout.write('\033[>4;1m')  # xterm modifyOtherKeys=1: Shift+Enter becomes distinguishable
        sys.stdout.flush()
        try:
            return self.session.prompt(message, bottom_toolbar=toolbar, refresh_interval=0.12,
                                       placeholder=self.HTML(f'<placeholder>{txt.escape(self.placeholder() if callable(self.placeholder) else self.placeholder)}</placeholder>'))
        finally:
            sys.stdout.write('\033[>4m')
            sys.stdout.flush()
