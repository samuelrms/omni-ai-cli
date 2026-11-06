#!/usr/bin/env python3
"""Persistent interactive front end for the existing Omni AI router.

Split into submodules: i18n, session, spend, runner, versions, queue, ops.
Public names are re-exported here so omni_ai.chat.<name> keeps working.
"""
import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from .. import config as config_tool
from .. import onboarding
from .. import skills
from .. import ui
from ..tools import KEY_NAMES, PARALLEL, TOOLS

from . import i18n, session, spend, runner, versions, queue, ops

from .i18n import (display, LANGS, T, LANG_FILE, load_lang, save_lang, LANG, set_lang,
                   tr, choose_lang, LANG_NAMES, ENGINES, MODES, PRINT_LOCK, ESCAPES, TOOL_TEXT)
from .session import MAX_CONTEXT, SAVE_LOCK, session_dir, Session
from .spend import costs_file, spend, spend_label, record_spend
from .runner import ACTIVE, NOISE, model_label, describe_tool, Runner, execute
from .versions import (RELEASES, COMMITS, CONVENTIONAL, CHANGE, github, releases,
                       translations_file, translate, installed_version, newer_release,
                       announce_release, show_versions)
from .queue import Queue, run_spec, queue_command, activity
from .ops import (SKIP_SYSTEM, already_answered, parse_parallel, COMPACT_PROMPT, compact,
                  TOOL_COMMANDS, run_tool, BUILTIN, NATIVE_HINT, run_native,
                  TERMINAL_SETUP, TERMINAL_OK, terminal_setup, setup_completion, list_sessions)

def main(argv=None):
    parser = argparse.ArgumentParser(description='Omni AI terminal interativo')
    parser.add_argument('--resume')
    parser.add_argument('--sessions', action='store_true')
    args = parser.parse_args(argv)
    if args.sessions:
        list_sessions()
        return
    try:
        session = Session(args.resume)
    except ValueError as error:
        parser.error(str(error))
    set_lang(LANG[0])
    setup_completion(session.data['cwd'])
    if load_lang() is None and sys.stdin.isatty():
        choose_lang()
    def engine_model():
        engine = session.data['engine']
        return tr('auto_model') if engine == 'auto' else model_label(engine)

    def status():
        mode = session.data.get('mode', 'default')
        # the mode is drawn as a colored chip by the input box
        return ' · '.join(x for x in ('Omni AI', session.data['engine'], engine_model() if session.data['engine'] != 'auto' else '',
                                      ui.home_path(session.data['cwd'])) if x)

    def commands():
        described = {}
        for line in tr('help').splitlines()[1:]:
            # "usage   usage   description": the description is the last chunk after 2+ spaces
            *usage, meta = re.split(r'\s{2,}', line.strip())
            for name in re.findall(r'(/[a-z]+)', ' '.join(usage)):
                described.setdefault(name, meta)
        # Every entry ends with who owns it: omni (this chat), claude, codex or agy
        listed = [(name, f"{described.get(name, '')} · omni") for name in BUILTIN]
        for name, entry in sorted(skills.discover(session.data['cwd']).items()):
            kind, path, scope = next(iter(entry.values()))
            listed.append(('/' + name, f"{skills.description(path)} · {'/'.join(entry)} ({scope})"))
        for tool, items in skills.NATIVE.items():
            listed += [(f'/{tool}:{command}', f'{meta} · {tool}') for command, meta in items]
        return listed

    def arguments(command, done):
        """What can come next after a command and the words already typed: (value, description)."""
        values = config_tool.model_values()
        if command in ('/usage', '/limits'):
            return [('watch', 'atualiza a cada 30s · Ctrl-C sai')] if not done else []
        if command == '/tool':
            return [(e, model_label(e) if e != 'auto' else tr('auto_model')) for e in sorted(ENGINES)] if not done else []
        if command == '/mode':
            helps = dict(line.split(None, 1) for line in tr('mode_help').splitlines() if line.strip())
            return [(m, helps.get(m, '')) for m in MODES] if not done else []
        if command == '/lang':
            return list(LANGS.items()) if not done else []
        if command == '/model':
            if not done:
                return [(k, f'{values[k] or "(automático)"} · {meta[2]}') for k, meta in config_tool.MODELS.items()]
            if len(done) == 1:
                choices = {'claude': ['opus', 'sonnet', 'haiku'], 'codex_effort': list(config_tool.EFFORTS),
                           'openrouter_paid': ['on', 'off']}.get(done[0], [])
                return [(c, 'atual' if c == values.get(done[0]) else '') for c in choices] + [('default', 'volta ao padrão')]
            return []
        if command == '/key':
            if not done:
                return [(p, 'configurada' if config_tool.stored_key(p)[0] else 'adicionar') for p in config_tool.KEYS] + \
                       [('add', 'mais uma chave (usadas em ordem)'), ('remove', 'apagar uma chave')]
            if done in (['remove'], ['add']):
                return [(p, f"{len(config_tool.stored_keys(p))} salva(s)") for p in config_tool.KEYS]
            if len(done) == 2 and done[0] == 'remove':
                return [(n, '') for n, _, o in config_tool.stored_keys(done[1]) if o != 'env']
            return []
        if command == '/account':
            actions = [('list', 'listar'), ('add', 'nova conta'), ('login', 'refazer login'), ('sync', 'compartilhar skills/MCP'),
                       ('enable', 'ativar'), ('disable', 'desativar'), ('remove', 'remover')]
            if not done:
                return actions
            tools = [(t, f'contas do {t}') for t in config_tool.TOOL_HOMES]
            if len(done) == 1:
                claude = [] if done[0] in ('list', 'add') else \
                    [(name, conf.get('name', '')) for name, conf in config_tool.ordered(config_tool.profiles())]
                return tools + claude
            if len(done) == 2 and done[1] in config_tool.TOOL_HOMES and done[0] not in ('list', 'add'):
                return [(name, '') for name, _ in config_tool.tool_accounts(done[1])]
            return []
        if command == '/resume' and not done:
            listed = []
            for path in sorted(session_dir().glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:30]:
                try:
                    data = json.loads(path.read_text())
                    listed.append((data['id'], f"{data.get('title') or '—'} · {ui.home_path(data['cwd'])}"))
                except (ValueError, KeyError):
                    continue
            return listed
        if command == '/queue':
            if not done:
                return [('rm', 'tirar da fila'), ('up', 'subir uma posição'), ('down', 'descer uma posição'), ('clear', 'esvaziar a fila')]
            if len(done) == 1 and done[0] != 'clear':
                return [(str(i), label.split('\n')[0]) for i, label in enumerate(queue.snapshot()[1], 1)]
            return []
        if command == '/setup':
            return [('install', 'instala as ferramentas que faltam')] if not done else []
        if command == '/sync':
            return [('--dry-run', 'só mostra o que faria')] if not done else []
        if command == '/parallel':
            return [(name + ':', f'subtarefa no {TOOLS[name].label} (somente leitura)') for name in PARALLEL] + [('|', 'separa subtarefas')]
        entry = skills.discover(session.data['cwd']).get(command[1:])
        if entry:
            tool = skills.pick_tool(entry, session.data['engine'])
            return [(o, skills.argument_hint(entry[tool][1])) for o in skills.hint_options(skills.argument_hint(entry[tool][1]))]
        return []

    def redundant(label):
        # only plain questions; commands and skills always run
        last = next((m for m in reversed(session.data['messages']) if m['role'] == 'assistant'), None)
        return not label.startswith('/') and last is not None and already_answered(label, last['content'])

    def persist(pending):
        # saved on every change, so even a killed terminal leaves the line behind for --resume
        if pending or session.data.get('queue'):
            session.data['queue'] = pending
            session.save()

    queue = Queue(skip=redundant, run=lambda spec: run_spec(session, spec), changed=persist)
    box = ui.InputBox(Path(session_dir()).parent / 'input_history', commands, status,
                      lambda: tr('placeholder'), lambda: tr('hints'),
                      arguments=arguments, cwd=lambda: session.data['cwd'], mode=lambda: session.data.get('mode', 'default'),
                      activity=lambda: activity(queue))

    def submit(label, spec):
        """With the input box, requests go to the queue and you keep typing; without it they run right away."""
        if box.session:
            queue.put(label, spec)
        else:
            run_spec(session, spec)

    def restore():
        """Requests left in the queue when this session was closed: show them and offer to run them."""
        pending = session.data.pop('queue', None) or []
        if not pending or not box.session:
            return
        display(tr('queue_restore', n=len(pending)))
        for i, spec in enumerate(pending, 1):
            display(f"{i:>2}. {ui.short(spec.get('label', '').split(chr(10))[0], ui.width() - 5)}")
        try:
            answer = input(tr('queue_restore_ask')).strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = 'n'
        if answer in ('', 's', 'sim', 'y', 'yes', 'si', 'sí', 'o', 'oui'):
            for spec in pending:
                spec = dict(spec)
                queue.put(spec.pop('label', ''), spec, waited=True)
        else:
            session.save()

    def idle(command):
        # these take over the terminal or swap the session under the running request
        if queue.busy():
            raise ValueError(tr('busy', c=command))
    if box.session is None:
        display(tr('welcome', id=session.id, cwd=session.data['cwd']))
    else:
        ui.set_title(f"Omni AI · {session.data.get('title') or session.id}")
        splashed = ui.splash(1.5)
        ui.clear_screen()
        ui.welcome(session.id, session.data['cwd'], session.data['engine'], engine_model(),
                   session.data.get('mode', 'default'), tr('hint_line'), animate=not splashed)
        if session.data['messages']:
            ui.replay(session.data['messages'])
        announce_release()
        if onboarding.first_run():
            # first launch: offer to install every tool that is missing, then show what is still to do
            onboarding.install(LANG[0])
            display(onboarding.guide(LANG[0]))
    ui.SPINNER.embedded = box.session is not None
    if box.session:
        from prompt_toolkit.patch_stdout import patch_stdout
        # answers from the queue print above the input instead of over what you're typing
        printing = patch_stdout(raw=True)
    else:
        printing = contextlib.nullcontext()
    try:
        printing.__enter__()
        restore()
        while True:
            try:
                mode = session.data.get('mode', 'default')
                tag = session.data['engine'] + ('' if mode == 'default' else ' · ' + mode)
                line = (box.ask() if box.session else input(f"\nai [{tag}] › ")).strip()
                # the "done" title stays until you're back and send something
                ui.set_title(f"Omni AI · {session.data.get('title') or session.id}")
            except EOFError:
                break
            except KeyboardInterrupt:
                if queue.busy():
                    # an explicit cancel: nothing stays saved
                    display(ui.yellow('  ' + tr('queue_dropped', n=queue.cancel())))
                    continue
                display('')
                break
            if not line:
                continue
            command, _, argument = line.partition(' ')
            try:
                if command in ('/exit', '/quit'):
                    break
                elif command == '/help':
                    display(tr('help'))
                elif command == '/tool':
                    if argument not in ENGINES:
                        raise ValueError(tr('bad_tool', v=', '.join(sorted(ENGINES))))
                    session.data['engine'] = argument
                    session.save()
                    display(tr('tool_set', v=argument))
                elif command in TOOL_COMMANDS:
                    idle(command)
                    run_tool(TOOL_COMMANDS[command] + argument.split())
                elif command == '/mode':
                    if not argument:
                        display(tr('mode_now', v=session.data.get('mode', 'default')))
                        display(tr('mode_help'))
                        continue
                    if argument not in MODES:
                        raise ValueError(tr('mode_bad'))
                    if argument == 'yolo':
                        try:
                            confirm = input(tr('yolo_confirm')).strip().lower()
                        except (EOFError, KeyboardInterrupt):
                            confirm = ''
                        if confirm not in ('s', 'sim', 'y', 'yes', 'si', 'sí', 'o', 'oui'):
                            continue
                    session.data['mode'] = argument
                    session.save()
                    display(tr('mode_now', v=argument))
                elif command == '/setup':
                    if argument.strip() == 'install':
                        idle(command)  # installers take over the terminal
                        onboarding.install(LANG[0])
                    display(onboarding.guide(LANG[0]))
                elif command == '/terminal-setup':
                    display(terminal_setup())
                elif command == '/lang':
                    if argument in LANGS:
                        set_lang(argument)
                        save_lang(argument)
                    else:
                        choose_lang()
                    display(tr('lang_set'))
                elif command == '/history':
                    for message in session.data['messages']:
                        display(f"{message['role']} · {message.get('provider', '')}\n{message['content']}\n")
                elif command in ('/print', '/export'):
                    from .. import export as _export
                    # Copy under the lock the queue worker holds while it appends turns, so the
                    # export sees a consistent snapshot instead of a list mutating mid-read.
                    with SAVE_LOCK:
                        snapshot = {
                            'id': session.id,
                            'title': session.data.get('title'),
                            'cwd': session.data.get('cwd', ''),
                            'created': session.data.get('created'),
                            'messages': [dict(m) for m in session.data['messages']],
                        }
                    if not snapshot['messages']:
                        display('A conversa está vazia; nada para exportar.')
                        continue
                    conv = _export.normalize(snapshot)
                    if command == '/print':
                        out = os.path.abspath(_export.default_name(conv, 'pdf'))
                        _export.to_pdf(conv, out)
                        display(f'📄 PDF da conversa salvo em {out}')
                    else:
                        text = _export.to_markdown(conv, argument.strip() or None)
                        out = os.path.abspath(_export.default_name(conv, 'md'))
                        with open(out, 'w', encoding='utf-8') as stream:
                            stream.write(text)
                        copied = _export.to_clipboard(text)
                        display(f'📝 Resumo para continuar em outra LLM salvo em {out}'
                                + (' (copiado para a área de transferência)' if copied else ''))
                elif command == '/sessions':
                    list_sessions()
                elif command in ('/resume', '/new'):
                    idle(command)
                    replacement = Session(argument) if command == '/resume' else Session()
                    session.close()
                    session = replacement
                    ui.set_title(f"Omni AI · {session.data.get('title') or session.id}")
                    display(f"Sessão {session.id} · {session.data['cwd']}")
                    if session.data['messages']:
                        ui.replay(session.data['messages'])
                    restore()
                elif command in ('/sent', '/trace'):
                    jobs = session.data['requests']
                    job = next((j for j in reversed(jobs) if not argument or j['id'] == argument), None)
                    if not job:
                        raise ValueError(tr('no_call'))
                    data = job.get('dispatches', job['sent']) if command == '/sent' else job.get('events', [])
                    display(json.dumps(data, ensure_ascii=False, indent=2) if not isinstance(data, str) else data)
                elif command == '/clear':
                    idle(command)
                    replacement = Session()
                    session.close()
                    session = replacement
                    ui.clear_screen()
                    ui.set_title('Omni AI')
                    display(tr('cleared', id=session.id))
                elif command == '/queue':
                    queue_command(queue, argument)
                elif command == '/versions':
                    show_versions(argument.strip())
                elif command == '/compact':
                    submit(line, {'kind': 'compact'})
                elif command == '/parallel':
                    tasks = parse_parallel(argument)
                    submit(line, {'kind': 'parallel', 'tasks': tasks})
                elif command == '/skills':
                    catalog = skills.discover(session.data['cwd'])
                    if not catalog:
                        display(tr('no_skills'))
                    for name, entry in sorted(catalog.items()):
                        kind, path, scope = next(iter(entry.values()))
                        display(f"/{name:<28} {', '.join(entry):<18} {scope:<8} {skills.description(path)[:70]}")
                elif skills.native_command(command[1:]):
                    idle(command)
                    run_native(*skills.native_command(command[1:]), session.data['cwd'])
                elif line.startswith('/') and '\n' not in command:
                    entry = skills.discover(session.data['cwd']).get(command[1:])
                    if not entry:
                        raise ValueError(tr('unknown'))
                    tool = skills.pick_tool(entry, session.data['engine'])
                    hint = skills.argument_hint(entry[tool][1])
                    if not argument and hint and box.session:
                        try:
                            argument = box.ask_args(command, hint, skills.hint_options(hint)).strip()
                        except KeyboardInterrupt:
                            continue
                    prompt, raw = skills.build_prompt(command[1:], argument, tool, entry)
                    if entry[tool][2] == 'projeto':
                        display(tr('skill_project', path=entry[tool][1]))
                    display(tr('skill_run', name=command[1:], tool=tool))
                    submit(line, {'kind': 'skill', 'tool': tool, 'prompt': prompt, 'raw': raw})
                else:
                    # the tool is fixed when you send, so a /tool typed later only affects what comes after
                    submit(line, {'kind': 'ask', 'engine': session.data['engine'], 'text': line})
            except (ValueError, OSError) as error:
                display(tr('error', e=error))
    finally:
        # leaving with requests in line: keep them for --resume, stop only what runs now
        pending = queue.pending()
        queue.changed = None
        if queue.busy():
            queue.cancel()
            queue.wait()
        printing.__exit__(None, None, None)
        if pending:
            session.data['queue'] = pending
            display(tr('queue_saved', n=len(pending), id=session.id))
        session.save()
        ui.set_title('')
        display(tr('saved', id=session.id))
        session.close()
