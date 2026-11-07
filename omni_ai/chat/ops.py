"""Chat operations: parallel parsing, compaction, native/tool commands, setup helpers."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .. import config as config_tool
from .. import onboarding
from .. import skills
from .. import ui
from ..osutils import module_cmd, module_env, resolve_bin
from ..tools import PARALLEL
from .i18n import tr, display, LANG, LANGS
from .session import MAX_CONTEXT, session_dir
from .runner import Runner, ACTIVE
SKIP_SYSTEM = ('You check a queue of chat requests. Given the previous answer and the next queued request, reply "yes" '
               'only if the previous answer already fully resolves the next request, so running it would be redundant. '
               'Reply "no" if it asks anything new, wants an action, or you are unsure. One word: yes or no.')


def already_answered(request, answer):
    """Free classifiers only: a queued request that the answer before it already settled is not sent again."""
    from .. import cli
    from ..pii import PIIMasker
    masker = PIIMasker()
    messages = [{'role': 'system', 'content': SKIP_SYSTEM},
                {'role': 'user', 'content': f'<previous_answer>\n{masker.mask(answer)[0][-6000:]}\n</previous_answer>\n'
                                            f'<next_request>\n{masker.mask(request)[0][:2000]}\n</next_request>\nyes or no:'}]
    for provider, url, extra in cli.free_classifiers():
        for _, key, _ in config_tool.stored_keys(provider):
            verdict = cli.ask_classifier(url, key, dict(extra, messages=messages, temperature=0, max_tokens=300), ('yes', 'no'))
            if verdict:
                return verdict == 'yes'
    return False

def parse_parallel(text):
    parts = text.split(' | ')
    if not 2 <= len(parts) <= 4:
        raise ValueError('Use 2 a 4 subtarefas separadas por " | ".')
    tasks = []
    for part in parts:
        engine, separator, prompt = part.partition(':')
        if not separator or engine.strip() not in PARALLEL or not prompt.strip():
            raise ValueError(f"Formato: /parallel codex: analisar testes | openrouter: explicar conceito ({', '.join(PARALLEL)})")
        tasks.append((engine.strip(), prompt.strip()))
    return tasks



COMPACT_PROMPT = ('Resuma a conversa abaixo para ela continuar em outra janela sem perder nada importante: objetivo, '
                  'decisões tomadas, arquivos e comandos citados, o que já foi feito, o que falta e pendências. '
                  'Escreva só o resumo, em tópicos curtos. {reply}\n\n{conversation}')


def compact(session):
    """Replace the history with one summary written by the session's engine (read-only run)."""
    messages = session.data['messages']
    if len(messages) < 2:
        display(tr('compact_empty'))
        return
    job = {'id': 'compact', 'engine': session.data.get('engine', 'auto'), 'prompt': 'compact',
           'sent': COMPACT_PROMPT.format(reply=tr('reply_in'), conversation=json.dumps(messages, ensure_ascii=False)[-MAX_CONTEXT:])}
    runner = Runner(session.data['cwd'], mode='plan')
    ACTIVE.add(runner)
    ui.SPINNER.start(tr('compacting'))
    try:
        runner.run(job, 'resumir conversa', readonly=True)
    except KeyboardInterrupt:
        runner.cancel()
    finally:
        ACTIVE.discard(runner)
        ui.SPINNER.stop()
    summary = '\n'.join(job.get('answers') or []).strip()
    if job.get('status') != 'done' or not summary:
        display(tr('compact_failed'))
        return
    session.data.setdefault('compacted', []).append({'at': time.time(), 'messages': messages})
    session.data['messages'] = [{'role': 'assistant', 'content': 'Resumo da conversa até aqui:\n' + summary,
                                 'provider': job.get('provider', job['engine']), 'compacted': len(messages)}]
    session.save()
    display(tr('compacted', n=len(messages)))

TOOL_COMMANDS = {'/usage': ['--usage'], '/limits': ['--usage'], '/model': ['--model'], '/account': ['--account'], '/key': ['--key'],
                 '/status': ['--status'], '/doctor': ['--doctor'], '/sync': ['--sync'], '/update': ['--update'], '/reset': ['--reset']}


def run_tool(args):
    """Run a management command attached to this terminal (login prompts need the TTY)."""
    try:
        subprocess.call(module_cmd('cli') + args, env=module_env())
    except KeyboardInterrupt:
        pass


BUILTIN = ['/usage', '/clear', '/compact', '/queue', '/versions', '/setup', '/terminal-setup', '/tool', '/model', '/account', '/key', '/status', '/doctor', '/reset', '/sync', '/update', '/mode', '/lang',
           '/skills', '/history', '/sent', '/trace', '/sessions', '/resume', '/new', '/parallel', '/print', '/export', '/help', '/exit']

NATIVE_HINT = {
    'codex': {'pt': 'Abrindo o Codex. Digite /{c} e, para voltar ao Omni, saia com Ctrl-C duas vezes.',
              'en': 'Opening Codex. Type /{c}; to come back to Omni, press Ctrl-C twice.',
              'es': 'Abriendo Codex. Escribe /{c}; para volver a Omni, Ctrl-C dos veces.',
              'fr': 'Ouverture de Codex. Tapez /{c} ; pour revenir à Omni, Ctrl-C deux fois.'},
    'agy': {'pt': 'Abrindo o Antigravity. Digite /{c} e, para voltar ao Omni, saia com Ctrl-C.',
            'en': 'Opening Antigravity. Type /{c}; to come back to Omni, press Ctrl-C.',
            'es': 'Abriendo Antigravity. Escribe /{c}; para volver a Omni, Ctrl-C.',
            'fr': "Ouverture d'Antigravity. Tapez /{c} ; pour revenir à Omni, Ctrl-C."},
    'claude': {'pt': 'Abrindo o Claude com /{c}. Para voltar ao Omni, saia com Ctrl-C duas vezes.',
               'en': 'Opening Claude with /{c}. To come back to Omni, press Ctrl-C twice.',
               'es': 'Abriendo Claude con /{c}. Para volver a Omni, Ctrl-C dos veces.',
               'fr': 'Ouverture de Claude avec /{c}. Pour revenir à Omni, Ctrl-C deux fois.'},
}


def run_native(tool, command, cwd):
    """Built-in commands live in each tool's own screen: open it attached to this terminal, then come back."""
    binary = resolve_bin(os.environ.get(tool.upper() + '_BIN', tool))
    if not binary:
        raise ValueError(f'{tool} não está instalado')
    ui.out(ui.dim(NATIVE_HINT[tool][LANG[0]].format(c=command)))
    # Claude runs a slash command given as its first input; Codex and Antigravity would send it to the model
    argv = [binary, '/' + command] if tool == 'claude' else [binary]
    env = {k: v for k, v in os.environ.items() if k != 'CLAUDE_CONFIG_DIR'}
    try:
        subprocess.call(argv, cwd=cwd, env=env)
    except KeyboardInterrupt:
        pass
    ui.out(ui.dim('↩ Omni AI'))

TERMINAL_SETUP = {
    'Apple_Terminal': {
        'pt': 'Terminal do Mac: ele envia Shift+Enter igual a Enter, então nenhum programa consegue separar os dois.\n'
              'Para quebrar linha use \\ + Enter, ou ligue Option+Enter:\n'
              '  Terminal › Ajustes › Perfis › (seu perfil) › Teclado › marque "Usar Option como tecla Meta".',
        'en': 'macOS Terminal sends Shift+Enter exactly like Enter, so no program can tell them apart.\n'
              'For a new line use \\ + Enter, or turn on Option+Enter:\n'
              '  Terminal › Settings › Profiles › (your profile) › Keyboard › check "Use Option as Meta key".',
        'es': 'La Terminal de macOS envía Shift+Enter igual que Enter, así que ningún programa puede distinguirlos.\n'
              'Para nueva línea usa \\ + Enter, o activa Option+Enter:\n'
              '  Terminal › Ajustes › Perfiles › (tu perfil) › Teclado › marca "Usar Opción como tecla Meta".',
        'fr': "Le Terminal macOS envoie Maj+Entrée exactement comme Entrée : aucun programme ne peut les distinguer.\n"
              "Pour une nouvelle ligne, utilisez \\ + Entrée, ou activez Option+Entrée :\n"
              "  Terminal › Réglages › Profils › (votre profil) › Clavier › cochez « Utiliser Option comme touche Méta ».",
    },
    'iTerm.app': {
        'pt': 'iTerm2: Shift+Enter já funciona. Se não, Ajustes › Perfis › Teclas › "Report modifiers using CSI u".',
        'en': 'iTerm2: Shift+Enter already works. If not: Settings › Profiles › Keys › "Report modifiers using CSI u".',
        'es': 'iTerm2: Shift+Enter ya funciona. Si no: Ajustes › Perfiles › Teclas › "Report modifiers using CSI u".',
        'fr': 'iTerm2 : Maj+Entrée fonctionne déjà. Sinon : Réglages › Profils › Touches › « Report modifiers using CSI u ».',
    },
}
TERMINAL_OK = {'pt': 'Seu terminal envia Shift+Enter separado; ele quebra a linha. \\ + Enter e Option+Enter também funcionam.',
               'en': 'Your terminal reports Shift+Enter; it inserts a new line. \\ + Enter and Option+Enter work too.',
               'es': 'Tu terminal envía Shift+Enter por separado; inserta una nueva línea. \\ + Enter y Option+Enter también.',
               'fr': 'Votre terminal distingue Maj+Entrée ; elle insère une nouvelle ligne. \\ + Entrée et Option+Entrée aussi.'}


def terminal_setup():
    program = os.environ.get('TERM_PROGRAM', '')
    text = TERMINAL_SETUP.get(program, TERMINAL_OK)
    return text[LANG[0]] if isinstance(text, dict) and LANG[0] in text else text.get('pt', '')


def setup_completion(cwd):
    """Tab completes built-in commands and the project's/user's skills."""
    if 'readline' not in sys.modules:
        return
    import readline
    names = sorted(set(BUILTIN) | {'/' + name for name in skills.discover(cwd)})

    def complete(text, state):
        matches = [n for n in names if n.startswith(text)] if text.startswith('/') else []
        return matches[state] if state < len(matches) else None
    readline.set_completer_delims(' \t\n')
    readline.set_completer(complete)
    # macOS ships libedit instead of GNU readline
    readline.parse_and_bind('bind ^I rl_complete' if 'libedit' in (readline.__doc__ or '') else 'tab: complete')


def list_sessions():
    for path in sorted(session_dir().glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            display(f"{data['id']} · {data.get('title') or '—'} · {len(data['messages'])} mensagens · {ui.home_path(data['cwd'])}")
        except (ValueError, KeyError):
            continue
