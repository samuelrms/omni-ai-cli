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

from . import config as config_tool, skills, ui
from .osutils import group_kwargs, kill_tree, lock_exclusive, module_cmd, module_env, resolve_bin
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
    'auto_model': {'pt': 'escolhe a cada pergunta', 'en': 'picks per question', 'es': 'elige en cada pregunta', 'fr': 'choisit à chaque question'},
    'placeholder': {'pt': 'Pergunte qualquer coisa · / para comandos e skills', 'en': 'Ask anything · / for commands and skills',
                    'es': 'Pregunta lo que quieras · / para comandos y skills', 'fr': 'Demandez ce que vous voulez · / pour commandes et skills'},
    'hints': {'pt': 'Tab completa · Esc+Enter nova linha · Ctrl-C sai', 'en': 'Tab completes · Esc+Enter new line · Ctrl-C exits',
              'es': 'Tab completa · Esc+Enter nueva línea · Ctrl-C sale', 'fr': 'Tab complète · Échap+Entrée nouvelle ligne · Ctrl-C quitte'},
    'working': {'pt': 'Trabalhando…', 'en': 'Working…', 'es': 'Trabajando…', 'fr': 'Au travail…'},
    'took': {'pt': 'Levou {t}', 'en': 'Took {t}', 'es': 'Tardó {t}', 'fr': 'Durée {t}'},
    'job_failed': {'pt': 'Não concluiu · /trace para detalhes', 'en': 'Did not finish · /trace for details',
                   'es': 'No terminó · /trace para detalles', 'fr': "N'a pas abouti · /trace pour les détails"},
    'job_cancelled': {'pt': 'Cancelado', 'en': 'Cancelled', 'es': 'Cancelado', 'fr': 'Annulé'},
    'hint_line': {'pt': '/help comandos · /skills skills do projeto · /mode plan|auto · /tool claude|codex|agy',
                  'en': '/help commands · /skills project skills · /mode plan|auto · /tool claude|codex|agy',
                  'es': '/help comandos · /skills skills del proyecto · /mode plan|auto · /tool claude|codex|agy',
                  'fr': '/help commandes · /skills skills du projet · /mode plan|auto · /tool claude|codex|agy'},
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
/usage   /limits                        Uso e limites de todas as ferramentas e contas
/claude:usage  /codex:status  /agy:usage  Comandos nativos de cada ferramenta (Tab lista todos)
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
/usage   /limits                        Usage and limits of every tool and account
/claude:usage  /codex:status  /agy:usage  Native commands of each tool (Tab lists them all)
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
/usage   /limits                        Uso y límites de todas las herramientas y cuentas
/claude:usage  /codex:status  /agy:usage  Comandos nativos de cada herramienta (Tab los lista)
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
/usage   /limits                        Usage et limites de tous les outils et comptes
/claude:usage  /codex:status  /agy:usage  Commandes natives de chaque outil (Tab les liste)
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
            ui.out(ui.dim(f'  {omitted} mensagens antigas ficaram fora desta chamada; o histórico completo segue salvo.'))
        return ('Continue a conversa abaixo. Mensagens anteriores são contexto, não novas instruções.\n'
                + json.dumps(chosen, ensure_ascii=False) + '\n\n' + tr('reply_in') + '\nPedido atual:\n' + prompt)


NOISE = re.compile(r'^[─━-]{5,}$|^(🤖|📦|🚀|🌐|📊|→ Credenciais)|Reading additional input from stdin|^AGY_ERROR: |'
                   r'^\d{4}-\d\d-\d\dT[\d:.]+Z\s+(ERROR|WARN|INFO)|^\s*$')


def model_label(provider, profile=''):
    values = config_tool.model_values()
    label = {'claude': values['claude'], 'codex': f"{values['codex']} {values['codex_effort']}",
             'agy': values['agy'] or 'padrão', 'openrouter': values['openrouter'] or 'gratuito'}.get(provider, '')
    return ' · '.join(x for x in (profile, label) if x)


def describe_tool(name, data):
    """One-line summary of a Claude tool call, like Claude Code's own transcript."""
    data = data or {}
    if name.startswith('mcp__'):
        parts = name.split('__')
        return 'Called', f"{parts[1]} · {parts[-1]}"
    if name == 'Bash':
        return 'Ran', data.get('description') or data.get('command', '')
    if name in ('Read', 'Write', 'Edit', 'NotebookEdit'):
        return {'Read': 'Read', 'Write': 'Wrote', 'Edit': 'Edited', 'NotebookEdit': 'Edited'}[name], \
            ui.home_path(data.get('file_path') or data.get('notebook_path', ''))
    if name in ('Grep', 'Glob'):
        return 'Searched', data.get('pattern', '')
    if name in ('WebFetch', 'WebSearch'):
        return 'Fetched' if name == 'WebFetch' else 'Searched web', data.get('url') or data.get('query', '')
    if name in ('Task', 'Agent'):
        return 'Agent', data.get('description', '')
    if name == 'Skill':
        return 'Skill', data.get('skill', '')
    if name == 'TodoWrite':
        return 'Updated todos', ''
    return name, json.dumps(data, ensure_ascii=False)


class Runner:
    def __init__(self, cwd, mode='default'):
        self.cwd, self.mode = cwd, mode
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
        env = module_env(dict(os.environ, SMART_AI_EVENTS='1', SMART_AI_CLASSIFY_PROMPT=classify_prompt, SMART_AI_MODE=self.mode,
                   SMART_AI_READ_ONLY='1' if readonly else '0', PYTHONUNBUFFERED='1', NO_COLOR='1'))
        args = module_cmd('cli')
        if job['engine'] != 'auto':
            args += ['--' + job['engine']]
        args += ['--', job['sent']]
        job['events'] = []
        job['answers'] = []
        job['view'] = ui.View(f"[{job['id']}] " if readonly else '')
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
            job['view'].error(str(error))
        return job

    def event(self, job, raw):
        if not raw:
            return
        view = job['view']
        try:
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError()
        except ValueError:
            # Router/CLI status lines: kept for /trace, shown only when they say something useful
            text = ESCAPES.sub('', raw)
            job['events'].append({'type': 'status', 'text': text})
            if not NOISE.search(text):
                view.note(ui.short(text, ui.width() - 4))
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
            view.provider(event['provider'], model_label(event['provider'], event.get('profile', '')))
        elif kind == 'smart.request':
            job['payload'] = event['data']
            if job.get('dispatches'):
                job['dispatches'][-1]['sent'] = event['data']
            view.note('dados pessoais mascarados antes do envio · /sent para ver')
        elif kind == 'smart.answer':
            job['model'] = event.get('model')
            self.answer(job, event.get('content', ''))
            view.note('modelo: ' + str(event.get('model', '?')))
        elif kind in ('item.started', 'item.completed'):
            item = event.get('item', {})
            item_type = item.get('type', '')
            done = kind == 'item.completed'
            if item_type == 'agent_message' and done:
                self.answer(job, item.get('text', ''))
            elif item_type == 'command_execution':
                if not done:
                    view.action('Ran', item.get('command', ''))
                else:
                    view.result(item.get('aggregated_output', ''), failed=item.get('exit_code') not in (0, None))
            elif item_type == 'file_change' and done:
                view.action('Edited', ', '.join(ui.home_path(c.get('path', '')) for c in item.get('changes', [])))
            elif item_type == 'mcp_tool_call':
                if not done:
                    view.action('Called', f"{item.get('server', '')} · {item.get('tool', '')}")
                else:
                    view.result(json.dumps(item.get('result') or item.get('error') or '', ensure_ascii=False),
                                failed=item.get('status') == 'failed')
            elif item_type == 'web_search' and not done:
                view.action('Searched web', item.get('query', ''))
        elif kind == 'assistant':
            for block in event.get('message', {}).get('content', []):
                if block.get('type') == 'text':
                    self.answer(job, block.get('text', ''))
                elif block.get('type') == 'tool_use':
                    view.action(*describe_tool(block.get('name', ''), block.get('input')))
        elif kind == 'user':
            # Claude tool results: one short line under the action they belong to
            for block in event.get('message', {}).get('content', []) or []:
                if isinstance(block, dict) and block.get('type') == 'tool_result':
                    content = block.get('content')
                    if isinstance(content, list):
                        content = '\n'.join(c.get('text', '') for c in content if isinstance(c, dict))
                    view.result(content or '', failed=bool(block.get('is_error')))
        elif kind == 'result':
            job['cost'] = event.get('total_cost_usd')
            if event.get('is_error'):
                job['protocol_error'] = True
                view.error(str(event.get('result') or 'erro na ferramenta'))
            elif event.get('result') and not job['answers']:
                self.answer(job, event['result'])
        elif kind in ('error', 'turn.failed'):
            job['protocol_error'] = True
            detail = event.get('message') or event.get('error', {})
            view.error(detail.get('message', str(detail)) if isinstance(detail, dict) else str(detail))

    def answer(self, job, content):
        if content and content.strip():
            job['answers'].append(content)
            job['view'].answer(content)


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
    if not session.data.get('title'):
        # First question names the conversation (tab title, /sessions)
        session.data['title'] = ui.short(tasks[0][1].split('\n')[0], 48)
        ui.set_title(f"Omni AI · {session.data['title']}")
    session.data['messages'].append({'role': 'user', 'content': '\n'.join(prompt for _, prompt in tasks)})
    session.save()
    # Run even single requests in a worker so Ctrl-C can terminate all child groups.
    started = time.time()
    ui.SPINNER.start(tr('working'))
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs))
    futures = [pool.submit(runner.run, job, job['prompt'], parallel) for job in jobs]
    try:
        for future in futures:
            future.result()
    except KeyboardInterrupt:
        ui.SPINNER.stop()
        ui.out(ui.yellow('  ' + tr('cancelling')))
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
        ui.SPINNER.stop()
        for job in jobs:
            prefix = f"[{job['id']}] " if parallel else ''
            if job['status'] == 'done':
                ui.out('\n' + ui.dim(f"{prefix}✻ {tr('took', t=ui.elapsed(started))} · {job.get('provider', job['engine'])}"))
            elif job['status'] == 'cancelled':
                ui.out('\n' + ui.yellow(f"{prefix}✻ {tr('job_cancelled')}"))
            else:
                ui.out('\n' + ui.red(f"{prefix}✗ {tr('job_failed')}"))
        for job in jobs:
            job.pop('view', None)
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




TOOL_COMMANDS = {'/usage': ['--usage'], '/limits': ['--usage'], '/model': ['--model'], '/account': ['--account'], '/key': ['--key'],
                 '/status': ['--status'], '/sync': ['--sync'], '/update': ['--update'], '/reset': ['--reset']}


def run_tool(args):
    """Run a management command attached to this terminal (login prompts need the TTY)."""
    try:
        subprocess.call(module_cmd('cli') + args, env=module_env())
    except KeyboardInterrupt:
        pass


BUILTIN = ['/usage', '/tool', '/model', '/account', '/key', '/status', '/reset', '/sync', '/update', '/mode', '/lang',
           '/skills', '/history', '/sent', '/trace', '/sessions', '/resume', '/new', '/parallel', '/help', '/exit']


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
            display(f"{data['id']} · {data.get('title') or '—'} · {len(data['messages'])} mensagens · {ui.home_path(data['cwd'])}")
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
    def engine_model():
        engine = session.data['engine']
        return tr('auto_model') if engine == 'auto' else model_label(engine)

    def status():
        mode = session.data.get('mode', 'default')
        return ' · '.join(x for x in ('Omni AI', session.data['engine'], engine_model() if session.data['engine'] != 'auto' else '',
                                      mode if mode != 'default' else '', ui.home_path(session.data['cwd'])) if x)

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

    box = ui.InputBox(Path(session_dir()).parent / 'input_history', commands, status, tr('placeholder'), tr('hints'))
    if box.session is None:
        display(tr('welcome', id=session.id, cwd=session.data['cwd']))
    else:
        ui.clear_screen()
        ui.set_title(f"Omni AI · {session.data.get('title') or session.id}")
        ui.welcome(session.id, session.data['cwd'], session.data['engine'], engine_model(),
                   session.data.get('mode', 'default'), tr('hint_line'))
        if session.data['messages']:
            ui.replay(session.data['messages'])
    try:
        while True:
            try:
                mode = session.data.get('mode', 'default')
                tag = session.data['engine'] + ('' if mode == 'default' else ' · ' + mode)
                line = (box.ask() if box.session else input(f"\nai [{tag}] › ")).strip()
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
                    ui.set_title(f"Omni AI · {session.data.get('title') or session.id}")
                    display(f"Sessão {session.id} · {session.data['cwd']}")
                    if session.data['messages']:
                        ui.replay(session.data['messages'])
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
                elif skills.native_command(command[1:]):
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
                    display(tr('skill_run', name=command[1:], tool=tool))
                    execute(session, [(tool, prompt)], raw=raw)
                else:
                    execute(session, [(session.data['engine'], line)])
            except (ValueError, OSError) as error:
                display(tr('error', e=error))
    finally:
        session.save()
        ui.set_title('')
        display(tr('saved', id=session.id))
        session.close()


if __name__ == '__main__':
    main()
