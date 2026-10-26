#!/usr/bin/env python3
"""Persistent interactive front end for the existing Omni AI router."""
import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
try:
    import readline  # arrow keys and editing; no separate history file
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from platform_utils import group_kwargs, kill_tree, lock_exclusive  # noqa: E402
import skills  # noqa: E402
ENGINES = {'auto', 'openrouter', 'codex', 'claude', 'agy'}
MODES = ('default', 'plan', 'edits', 'auto', 'yolo')
MAX_CONTEXT = 48000
PRINT_LOCK = threading.Lock()
ESCAPES = re.compile(r'\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f]')


def display(text):
    with PRINT_LOCK:
        print(ESCAPES.sub('', str(text)), flush=True)

LANGS = {'pt': 'Português (Brasil)', 'en': 'English', 'es': 'Español', 'fr': 'Français'}
T = {
    'welcome': {'pt': 'Omni AI · sessão {id}\nProjeto: {cwd}\nEscreva sua pergunta e tecle Enter. /help mostra comandos · Ctrl-C sai.',
                'en': 'Omni AI · session {id}\nProject: {cwd}\nType your question and press Enter. /help lists commands · Ctrl-C exits.',
                'es': 'Omni AI · sesión {id}\nProyecto: {cwd}\nEscribe tu pregunta y pulsa Enter. /help muestra comandos · Ctrl-C sale.',
                'fr': 'Omni AI · session {id}\nProjet : {cwd}\nTapez votre question puis Entrée. /help liste les commandes · Ctrl-C quitte.'},
    'saved': {'pt': 'Sessão salva. Retome com: ai --resume {id}', 'en': 'Session saved. Resume with: ai --resume {id}',
              'es': 'Sesión guardada. Retoma con: ai --resume {id}', 'fr': 'Session enregistrée. Reprenez avec : ai --resume {id}'},
    'cancelling': {'pt': 'Cancelando agentes... (Ctrl-C de novo no prompt para sair)', 'en': 'Cancelling agents... (Ctrl-C again at the prompt to exit)',
                   'es': 'Cancelando agentes... (Ctrl-C otra vez en el prompt para salir)', 'fr': 'Annulation des agents... (Ctrl-C à nouveau pour quitter)'},
    'error': {'pt': 'Erro: {e}', 'en': 'Error: {e}', 'es': 'Error: {e}', 'fr': 'Erreur : {e}'},
    'unknown': {'pt': 'Comando ou skill desconhecido. Use /help ou /skills.', 'en': 'Unknown command or skill. Use /help or /skills.',
                'es': 'Comando o skill desconocido. Usa /help o /skills.', 'fr': 'Commande ou skill inconnue. Utilisez /help ou /skills.'},
    'no_skills': {'pt': 'Nenhuma skill ou comando encontrado neste projeto nem no usuário.', 'en': 'No skills or commands found in this project or user folders.',
                  'es': 'No se encontraron skills ni comandos en este proyecto ni en el usuario.', 'fr': "Aucune skill ni commande trouvée dans ce projet ni chez l'utilisateur."},
    'skill_run': {'pt': '→ /{name} via {tool}', 'en': '→ /{name} via {tool}', 'es': '→ /{name} vía {tool}', 'fr': '→ /{name} via {tool}'},
    'bad_tool': {'pt': 'Ferramenta inválida: {v}', 'en': 'Invalid tool: {v}', 'es': 'Herramienta inválida: {v}', 'fr': 'Outil invalide : {v}'},
    'tool_set': {'pt': 'Ferramenta: {v}', 'en': 'Tool: {v}', 'es': 'Herramienta: {v}', 'fr': 'Outil : {v}'},
    'no_call': {'pt': 'Nenhuma chamada encontrada', 'en': 'No call found', 'es': 'Ninguna llamada encontrada', 'fr': 'Aucun appel trouvé'},
    'mode_now': {'pt': 'Modo: {v}', 'en': 'Mode: {v}', 'es': 'Modo: {v}', 'fr': 'Mode : {v}'},
    'mode_bad': {'pt': 'Modo inválido: default, plan, edits, auto, yolo', 'en': 'Invalid mode: default, plan, edits, auto, yolo',
                 'es': 'Modo inválido: default, plan, edits, auto, yolo', 'fr': 'Mode invalide : default, plan, edits, auto, yolo'},
    'mode_help': {
        'pt': '  default  padrão de cada ferramenta\n  plan     só planeja e lê, não altera nada\n  edits    aceita edições de arquivo\n  auto     executa sozinho com proteções da ferramenta\n  yolo     sem permissões nem sandbox (perigoso)',
        'en': '  default  each tool\'s default\n  plan     plans and reads only, changes nothing\n  edits    accepts file edits\n  auto     runs on its own with the tool\'s safeguards\n  yolo     no permissions or sandbox (dangerous)',
        'es': '  default  predeterminado de cada herramienta\n  plan     solo planifica y lee, no cambia nada\n  edits    acepta ediciones de archivos\n  auto     ejecuta solo con las protecciones de la herramienta\n  yolo     sin permisos ni sandbox (peligroso)',
        'fr': '  default  comportement par défaut de chaque outil\n  plan     planifie et lit seulement, ne modifie rien\n  edits    accepte les modifications de fichiers\n  auto     agit seul avec les protections de l\'outil\n  yolo     sans permissions ni sandbox (dangereux)'},
    'yolo_confirm': {'pt': 'yolo desliga permissões e sandbox: a IA pode apagar ou alterar qualquer arquivo e rodar qualquer comando. Confirmar? [s/N] ',
                     'en': 'yolo turns off permissions and sandbox: the AI can delete or change any file and run any command. Confirm? [y/N] ',
                     'es': 'yolo desactiva permisos y sandbox: la IA puede borrar o cambiar cualquier archivo y ejecutar cualquier comando. ¿Confirmar? [s/N] ',
                     'fr': 'yolo désactive permissions et sandbox : l\'IA peut supprimer ou modifier tout fichier et lancer toute commande. Confirmer ? [o/N] '},
    'lang_pick': {'pt': 'Idioma', 'en': 'Language', 'es': 'Idioma', 'fr': 'Langue'},
    'lang_set': {'pt': 'Idioma: Português (Brasil)', 'en': 'Language: English', 'es': 'Idioma: Español', 'fr': 'Langue : Français'},
    'reply_in': {'pt': 'Responda em português do Brasil.', 'en': 'Reply in English.', 'es': 'Responde en español.', 'fr': 'Réponds en français.'},
    'help': {
        'pt': """Escreva normalmente e tecle Enter. Setas: histórico de linhas. Ctrl-C: cancela resposta / sai. Ctrl-D: sai.
/tool auto|claude|codex|agy|openrouter   Escolher ferramenta (auto = decide sozinho)
/model [chave valor]                     Ver/alterar modelos (claude, codex, codex_effort, agy, openrouter)
/account [list|add|login|sync|remove] NOME    Contas Claude (login dentro do ai)
/key [claude|codex|agy|openrouter|remove X] Chaves de API (reserva da assinatura)
/status   /reset                         Status das contas / zerar cooldowns
/sync [--dry-run]                        Compartilhar MCPs e skills entre Claude, Codex e agy
/mode default|plan|edits|auto|yolo      Modo de permissão (vale para Claude, Codex e agy)
/lang pt|en|es|fr                        Mudar idioma
/skills                                  Skills e comandos do projeto e do usuário (/nome args executa; Tab completa)
/history                                 Mostrar conversa
/sent [agente-N]  /trace [agente-N]      Ver o que foi enviado / eventos
/sessions   /resume ID   /new            Sessões salvas
/parallel codex: tarefa | codex: tarefa  2–4 análises em paralelo
/exit                                    Sair""",
        'en': """Type normally and press Enter. Arrows: line history. Ctrl-C: cancel answer / exit. Ctrl-D: exit.
/tool auto|claude|codex|agy|openrouter   Pick tool (auto = decides for you)
/model [key value]                       Show/change models (claude, codex, codex_effort, agy, openrouter)
/account [list|add|login|sync|remove] NAME    Claude accounts (login inside ai)
/key [claude|codex|agy|openrouter|remove X] API keys (subscription fallback)
/status   /reset                         Account status / clear cooldowns
/sync [--dry-run]                        Share MCPs and skills across Claude, Codex and agy
/mode default|plan|edits|auto|yolo      Permission mode (applies to Claude, Codex and agy)
/lang pt|en|es|fr                        Change language
/skills                                  Project and user skills/commands (/name args runs; Tab completes)
/history                                 Show conversation
/sent [agente-N]  /trace [agente-N]      What was sent / events
/sessions   /resume ID   /new            Saved sessions
/parallel codex: task | codex: task      2–4 analyses in parallel
/exit                                    Exit""",
        'es': """Escribe normalmente y pulsa Enter. Flechas: historial. Ctrl-C: cancela respuesta / sale. Ctrl-D: sale.
/tool auto|claude|codex|agy|openrouter   Elegir herramienta (auto = decide sola)
/model [clave valor]                     Ver/cambiar modelos (claude, codex, codex_effort, agy, openrouter)
/account [list|add|login|sync|remove] NOMBRE  Cuentas Claude (login dentro de ai)
/key [claude|codex|agy|openrouter|remove X] Claves de API (respaldo de la suscripción)
/status   /reset                         Estado de cuentas / limpiar cooldowns
/sync [--dry-run]                        Compartir MCPs y skills entre Claude, Codex y agy
/mode default|plan|edits|auto|yolo      Modo de permisos (Claude, Codex y agy)
/lang pt|en|es|fr                        Cambiar idioma
/skills                                  Skills y comandos del proyecto y del usuario (/nombre args ejecuta; Tab completa)
/history                                 Mostrar conversación
/sent [agente-N]  /trace [agente-N]      Lo enviado / eventos
/sessions   /resume ID   /new            Sesiones guardadas
/parallel codex: tarea | codex: tarea    2–4 análisis en paralelo
/exit                                    Salir""",
        'fr': """Tapez normalement puis Entrée. Flèches : historique. Ctrl-C : annule la réponse / quitte. Ctrl-D : quitte.
/tool auto|claude|codex|agy|openrouter   Choisir l'outil (auto = choix automatique)
/model [clé valeur]                      Voir/changer les modèles (claude, codex, codex_effort, agy, openrouter)
/account [list|add|login|sync|remove] NOM     Comptes Claude (connexion dans ai)
/key [claude|codex|agy|openrouter|remove X] Clés API (secours de l'abonnement)
/status   /reset                         État des comptes / réinitialiser les cooldowns
/sync [--dry-run]                        Partager MCP et skills entre Claude, Codex et agy
/mode default|plan|edits|auto|yolo      Mode de permissions (Claude, Codex et agy)
/lang pt|en|es|fr                        Changer de langue
/skills                                  Skills et commandes du projet et de l'utilisateur (/nom args exécute ; Tab complète)
/history                                 Afficher la conversation
/sent [agente-N]  /trace [agente-N]      Ce qui a été envoyé / événements
/sessions   /resume ID   /new            Sessions enregistrées
/parallel codex: tâche | codex: tâche    2–4 analyses en parallèle
/exit                                    Quitter"""},
}
LANG_FILE = Path('~/.config/smart-ai/lang').expanduser()


def load_lang():
    try:
        value = LANG_FILE.read_text().strip()
        if value in LANGS:
            return value
    except OSError:
        pass
    return None


def save_lang(value):
    LANG_FILE.parent.mkdir(parents=True, exist_ok=True)
    LANG_FILE.write_text(value)


LANG = [load_lang() or (os.environ.get('LANG', 'pt')[:2] if os.environ.get('LANG', '')[:2] in LANGS else 'pt')]


def tr(key, **values):
    return T[key][LANG[0]].format(**values)


def choose_lang():
    for number, (code, name) in enumerate(LANGS.items(), 1):
        display(f'  {number}) {name}')
    try:
        answer = input(f"{tr('lang_pick')} [1-4]: ").strip()
    except (EOFError, KeyboardInterrupt):
        answer = ''
    codes = list(LANGS)
    if answer.isdigit() and 1 <= int(answer) <= 4:
        LANG[0] = codes[int(answer) - 1]
    elif answer in LANGS:
        LANG[0] = answer
    save_lang(LANG[0])


def session_dir():
    path = Path(os.environ.get('SMART_AI_SESSIONS', '~/.local/share/smart-ai/sessions')).expanduser()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


class Session:
    def __init__(self, identifier=None):
        self.id = identifier or uuid.uuid4().hex[:12]
        if not re.fullmatch(r'[a-f0-9]{12}', self.id):
            raise ValueError('ID de sessão inválido')
        self.path = session_dir() / (self.id + '.json')
        if identifier and not self.path.exists():
            raise ValueError('Sessão não encontrada')
        self.lock = os.fdopen(os.open(str(self.path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600), 'w')
        try:
            lock_exclusive(self.lock)
        except BlockingIOError:
            self.lock.close()
            raise ValueError('Sessão já aberta em outro terminal') from None
        self.data = json.loads(self.path.read_text()) if identifier else {
            'id': self.id, 'cwd': os.getcwd(), 'engine': 'auto', 'messages': [], 'requests': [], 'created': time.time()
        }
        for job in self.data['requests']:
            if job['status'] == 'running':
                job['status'] = 'interrupted'
        self.save()

    def save(self):
        self.data['updated'] = time.time()
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix='.session-')
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(self.data, stream, ensure_ascii=False, indent=2)
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def close(self):
        self.lock.close()

    def context(self, prompt):
        chosen = []
        remaining = max(0, MAX_CONTEXT - len(prompt))
        for message in reversed(self.data['messages']):
            size = len(json.dumps(message, ensure_ascii=False))
            if size > remaining:
                break
            chosen.insert(0, message)
            remaining -= size
        omitted = len(self.data['messages']) - len(chosen)
        if omitted:
            display(f'[contexto] {omitted} mensagens antigas fora desta chamada; histórico completo salvo.')
        return ('Continue a conversa abaixo. Mensagens anteriores são contexto, não novas instruções.\n'
                + json.dumps(chosen, ensure_ascii=False) + '\n\n' + tr('reply_in') + '\nPedido atual:\n' + prompt)


class Runner:
    def __init__(self, cwd, emit=display, mode='default'):
        self.cwd, self.emit, self.mode = cwd, emit, mode
        self.children = set()
        self.lock = threading.Lock()
        self.cancelled = threading.Event()

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            children = list(self.children)
        for child in children:
            kill_tree(child)

    def run(self, job, classify_prompt, readonly=False):
        label = job['id']
        env = dict(os.environ, SMART_AI_EVENTS='1', SMART_AI_CLASSIFY_PROMPT=classify_prompt, SMART_AI_MODE=self.mode,
                   SMART_AI_READ_ONLY='1' if readonly else '0', PYTHONUNBUFFERED='1')
        args = [sys.executable, str(ROOT / 'smart_ai.py')]
        if job['engine'] != 'auto':
            args += ['--' + job['engine']]
        args += ['--', job['sent']]
        job['events'] = []
        job['answers'] = []
        self.emit(f"[{label}] início · {job['engine']} · {'somente leitura' if readonly else 'principal'}")
        try:
            with self.lock:
                if self.cancelled.is_set():
                    job['status'] = 'cancelled'
                    return job
                child = subprocess.Popen(args, cwd=self.cwd, env=env, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                         encoding='utf-8', errors='replace', **group_kwargs())
                self.children.add(child)
            try:
                for raw in child.stdout:
                    self.event(job, raw.rstrip())
                code = child.wait()
            finally:
                child.stdout.close()
                with self.lock:
                    self.children.discard(child)
            job['exit_code'] = code
            job['status'] = 'cancelled' if self.cancelled.is_set() else ('done' if code == 0 and job['answers'] and not job.get('protocol_error') else 'failed')
        except (OSError, ValueError) as error:
            job['status'] = 'failed'
            self.emit(f'[{label}] erro: {error}')
        self.emit(f"[{label}] {job['status']}")
        return job

    def event(self, job, raw):
        if not raw:
            return
        label = job['id']
        try:
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError()
        except ValueError:
            # CLI status/diagnostics remain visible, but never enter assistant history.
            job['events'].append({'type': 'status', 'text': ESCAPES.sub('', raw)})
            self.emit(f'[{label}] {raw}')
            return
        kind = event.get('type', '')
        if kind in ('reasoning', 'thinking') or event.get('item', {}).get('type') == 'reasoning':
            return
        if kind == 'assistant':
            message = event.get('message', {})
            message['content'] = [block for block in message.get('content', []) if block.get('type') not in ('thinking', 'redacted_thinking')]
        job['events'].append(event)
        if kind == 'smart.provider':
            job['provider'] = event['provider']
            job['answers'] = []
            job['protocol_error'] = False
            job.setdefault('dispatches', []).append({'provider': event['provider'], 'profile': event.get('profile'),
                                                    'sent': job['sent'] if event['provider'] != 'openrouter' else None})
            self.emit(f"[{label}] atendendo: {event['provider']} {event.get('profile', '')}")
        elif kind == 'smart.request':
            job['payload'] = event['data']
            if job.get('dispatches'):
                job['dispatches'][-1]['sent'] = event['data']
            self.emit(f'[{label}] payload mascarado enviado ao OpenRouter · /sent para inspecionar')
        elif kind == 'smart.answer':
            job['model'] = event.get('model')
            self.answer(job, event.get('content', ''))
            self.emit(f"[{label}] modelo: {event.get('model', '?')}")
        elif kind in ('item.started', 'item.completed'):
            item = event.get('item', {})
            item_type = item.get('type', '')
            if item_type == 'agent_message' and kind == 'item.completed':
                self.answer(job, item.get('text', ''))
            elif item_type == 'command_execution':
                self.emit(f"[{label}] comando {kind.split('.')[1]}: {item.get('command', '')}")
            elif item_type in ('file_change', 'mcp_tool_call', 'web_search', 'collab_tool_call'):
                self.emit(f'[{label}] {item_type}: ' + json.dumps(item, ensure_ascii=False))
        elif kind == 'assistant':
            for block in event.get('message', {}).get('content', []):
                if block.get('type') == 'text':
                    self.answer(job, block.get('text', ''))
                elif block.get('type') == 'tool_use':
                    self.emit(f"[{label}] ferramenta: {block.get('name')} " + json.dumps(block.get('input', {}), ensure_ascii=False))
        elif kind == 'result':
            if event.get('is_error'):
                job['protocol_error'] = True
                self.emit(f'[{label}] ferramenta reportou erro')
            elif event.get('result') and not job['answers']:
                self.answer(job, event['result'])
        elif kind in ('error', 'turn.failed'):
            job['protocol_error'] = True
            self.emit(f'[{label}] erro: ' + json.dumps(event, ensure_ascii=False))
        elif kind in ('system', 'thread.started', 'turn.started', 'turn.completed'):
            self.emit(f'[{label}] {kind}')
        else:
            self.emit(f'[{label}] evento: {kind or "sem tipo"} · /trace para detalhes')

    def answer(self, job, content):
        if content:
            job['answers'].append(content)
            self.emit(f"\n[{job['id']}] {content}\n")


def execute(session, tasks, parallel=False, raw=False):
    runner = Runner(session.data['cwd'], mode=session.data.get('mode', 'default'))
    jobs = []
    for engine, prompt in tasks:
        job = {'id': 'agente-' + str(len(session.data['requests']) + 1), 'engine': engine,
               'prompt': prompt, 'sent': prompt if raw else session.context(prompt), 'status': 'running',
               'created': time.time()}
        if parallel:
            job['sent'] += '\n\nSubtarefa delegada: faça somente análise, sem alterar arquivos. Entregue evidências e conclusões.'
        session.data['requests'].append(job)
        jobs.append(job)
    session.data['messages'].append({'role': 'user', 'content': '\n'.join(prompt for _, prompt in tasks)})
    session.save()
    # Run even single requests in a worker so Ctrl-C can terminate all child groups.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs))
    futures = [pool.submit(runner.run, job, job['prompt'], parallel) for job in jobs]
    try:
        for future in futures:
            future.result()
    except KeyboardInterrupt:
        display(tr('cancelling'))
        runner.cancel()
        for future in futures:
            try:
                future.result(timeout=5)
            except concurrent.futures.TimeoutError:
                with runner.lock:
                    children = list(runner.children)
                for child in children:
                    kill_tree(child, force=True)
    finally:
        pool.shutdown(wait=True)
        for job in jobs:
            if job.get('answers') and job['status'] == 'done':
                session.data['messages'].append({'role': 'assistant', 'content': '\n'.join(job['answers']),
                                                'agent': job['id'], 'provider': job.get('provider', job['engine'])})
        session.save()


def parse_parallel(text):
    parts = text.split(' | ')
    if not 2 <= len(parts) <= 4:
        raise ValueError('Use 2 a 4 subtarefas separadas por " | ".')
    tasks = []
    for part in parts:
        engine, separator, prompt = part.partition(':')
        if not separator or engine.strip() not in ('codex', 'openrouter') or not prompt.strip():
            raise ValueError('Formato: /parallel codex: analisar testes | openrouter: explicar conceito')
        tasks.append((engine.strip(), prompt.strip()))
    return tasks




TOOL_COMMANDS = {'/model': ['--model'], '/account': ['--account'], '/key': ['--key'],
                 '/status': ['--status'], '/sync': ['--sync'], '/update': ['--update'], '/reset': ['--reset']}


def run_tool(args):
    """Run a management command attached to this terminal (login prompts need the TTY)."""
    try:
        subprocess.call([sys.executable, str(ROOT / 'smart_ai.py')] + args)
    except KeyboardInterrupt:
        pass


BUILTIN = ['/tool', '/model', '/account', '/key', '/status', '/reset', '/sync', '/update', '/mode', '/lang',
           '/skills', '/history', '/sent', '/trace', '/sessions', '/resume', '/new', '/parallel', '/help', '/exit']


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
            data = json.loads(path.read_text())
            display(f"{data['id']} · {len(data['messages'])} mensagens · {data['cwd']}")
        except (ValueError, KeyError):
            continue


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
    setup_completion(session.data['cwd'])
    if load_lang() is None and sys.stdin.isatty():
        choose_lang()
    display(tr('welcome', id=session.id, cwd=session.data['cwd']))
    try:
        while True:
            try:
                mode = session.data.get('mode', 'default')
                tag = session.data['engine'] + ('' if mode == 'default' else ' · ' + mode)
                line = input(f"\nai [{tag}] › ").strip()
            except EOFError:
                break
            except KeyboardInterrupt:
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
                elif command == '/lang':
                    if argument in LANGS:
                        LANG[0] = argument
                        save_lang(argument)
                    else:
                        choose_lang()
                    display(tr('lang_set'))
                elif command == '/history':
                    for message in session.data['messages']:
                        display(f"{message['role']} · {message.get('provider', '')}\n{message['content']}\n")
                elif command == '/sessions':
                    list_sessions()
                elif command in ('/resume', '/new'):
                    replacement = Session(argument) if command == '/resume' else Session()
                    session.close()
                    session = replacement
                    display(f"Sessão {session.id} · {session.data['cwd']}")
                elif command in ('/sent', '/trace'):
                    jobs = session.data['requests']
                    job = next((j for j in reversed(jobs) if not argument or j['id'] == argument), None)
                    if not job:
                        raise ValueError(tr('no_call'))
                    data = job.get('dispatches', job['sent']) if command == '/sent' else job.get('events', [])
                    display(json.dumps(data, ensure_ascii=False, indent=2) if not isinstance(data, str) else data)
                elif command == '/parallel':
                    execute(session, parse_parallel(argument), parallel=True)
                elif command == '/skills':
                    catalog = skills.discover(session.data['cwd'])
                    if not catalog:
                        display(tr('no_skills'))
                    for name, entry in sorted(catalog.items()):
                        kind, path, scope = next(iter(entry.values()))
                        display(f"/{name:<28} {', '.join(entry):<18} {scope:<8} {skills.description(path)[:70]}")
                elif line.startswith('/'):
                    entry = skills.discover(session.data['cwd']).get(command[1:])
                    if not entry:
                        raise ValueError(tr('unknown'))
                    tool = skills.pick_tool(entry, session.data['engine'])
                    prompt, raw = skills.build_prompt(command[1:], argument, tool, entry)
                    display(tr('skill_run', name=command[1:], tool=tool))
                    execute(session, [(tool, prompt)], raw=raw)
                else:
                    execute(session, [(session.data['engine'], line)])
            except (ValueError, OSError) as error:
                display(tr('error', e=error))
    finally:
        session.save()
        display(tr('saved', id=session.id))
        session.close()


if __name__ == '__main__':
    main()
