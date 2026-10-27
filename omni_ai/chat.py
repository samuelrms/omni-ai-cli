#!/usr/bin/env python3
"""Persistent interactive front end for the existing Omni AI router."""
import argparse
import concurrent.futures
import contextlib
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

from . import config as config_tool, onboarding, skills, ui
from .osutils import group_kwargs, kill_tree, lock_exclusive, module_cmd, module_env, resolve_bin
from .tools import KEY_NAMES, PARALLEL, TOOLS
ENGINES = {'auto', *TOOLS}
MODES = ('default', 'plan', 'edits', 'auto', 'yolo')
MAX_CONTEXT = 48000
PRINT_LOCK = threading.Lock()
SAVE_LOCK = threading.Lock()
ACTIVE = set()  # runners working right now, so Ctrl-C at the input can stop them
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
    'hints': {'pt': '/ comandos · @ arquivos · \\⏎ nova linha · → aceita · Ctrl-R busca · Ctrl-C sai',
              'en': '/ commands · @ files · \\⏎ new line · → accept · Ctrl-R search · Ctrl-C exits',
              'es': '/ comandos · @ archivos · \\⏎ nueva línea · → acepta · Ctrl-R busca · Ctrl-C sale',
              'fr': '/ commandes · @ fichiers · \\⏎ nouvelle ligne · → accepter · Ctrl-R recherche · Ctrl-C quitte'},
    'cleared': {'pt': 'Conversa limpa. Sessão nova: {id}', 'en': 'Conversation cleared. New session: {id}',
                'es': 'Conversación limpia. Sesión nueva: {id}', 'fr': 'Conversation effacée. Nouvelle session : {id}'},
    'compact_empty': {'pt': 'Nada para compactar ainda.', 'en': 'Nothing to compact yet.', 'es': 'Nada que compactar todavía.',
                      'fr': 'Rien à compacter pour le moment.'},
    'compacting': {'pt': 'Resumindo a conversa…', 'en': 'Summarizing the conversation…', 'es': 'Resumiendo la conversación…',
                   'fr': 'Résumé de la conversation…'},
    'compacted': {'pt': 'Conversa compactada: {n} mensagens viraram um resumo.', 'en': 'Conversation compacted: {n} messages became one summary.',
                  'es': 'Conversación compactada: {n} mensajes en un resumen.', 'fr': 'Conversation compactée : {n} messages en un résumé.'},
    'compact_failed': {'pt': 'Não deu para resumir agora; a conversa ficou como estava.', 'en': 'Could not summarize now; the conversation is unchanged.',
                       'es': 'No se pudo resumir; la conversación sigue igual.', 'fr': "Impossible de résumer ; la conversation n'a pas changé."},
    'queued': {'pt': 'na fila ({n})', 'en': 'queued ({n})', 'es': 'en cola ({n})', 'fr': "en file d'attente ({n})"},
    'next_up': {'pt': '▶ da fila: {v}', 'en': '▶ from the queue: {v}', 'es': '▶ de la cola: {v}', 'fr': '▶ de la file : {v}'},
    'queue_bad': {'pt': 'Número fora da fila (há {n} esperando).', 'en': 'Number not in the queue ({n} waiting).',
                  'es': 'Número fuera de la cola (hay {n} esperando).', 'fr': "Numéro hors de la file ({n} en attente)."},
    'queue_usage': {'pt': 'Use: /queue · /queue rm N · /queue up N · /queue down N · /queue clear',
                    'en': 'Use: /queue · /queue rm N · /queue up N · /queue down N · /queue clear',
                    'es': 'Usa: /queue · /queue rm N · /queue up N · /queue down N · /queue clear',
                    'fr': 'Utilisez : /queue · /queue rm N · /queue up N · /queue down N · /queue clear'},
    'queue_removed': {'pt': 'Tirado da fila: {v}', 'en': 'Removed from the queue: {v}', 'es': 'Quitado de la cola: {v}',
                      'fr': 'Retiré de la file : {v}'},
    'queue_moved': {'pt': 'Agora é o {n}º da fila.', 'en': 'Now #{n} in the queue.', 'es': 'Ahora es el {n}º de la cola.',
                    'fr': 'Maintenant n° {n} dans la file.'},
    'queue_cleared': {'pt': '{n} pedido(s) tirado(s) da fila.', 'en': '{n} request(s) removed from the queue.',
                      'es': '{n} pedido(s) quitado(s) de la cola.', 'fr': '{n} demande(s) retirée(s) de la file.'},
    'queue_empty': {'pt': 'Fila vazia.', 'en': 'Queue is empty.', 'es': 'Cola vacía.', 'fr': 'File vide.'},
    'queue_saved': {'pt': '{n} pedido(s) da fila ficaram salvos; retome com: ai --resume {id}',
                    'en': '{n} queued request(s) saved; resume with: ai --resume {id}',
                    'es': '{n} pedido(s) de la cola guardados; retoma con: ai --resume {id}',
                    'fr': '{n} demande(s) en attente enregistrée(s) ; reprenez avec : ai --resume {id}'},
    'queue_restore': {'pt': 'Esta sessão tem {n} pedido(s) na fila de antes:', 'en': 'This session has {n} request(s) left in the queue:',
                      'es': 'Esta sesión tiene {n} pedido(s) en la cola de antes:', 'fr': 'Cette session a {n} demande(s) restée(s) en file :'},
    'queue_restore_ask': {'pt': 'Continuar com eles? [S/n] ', 'en': 'Run them now? [Y/n] ', 'es': '¿Continuar con ellos? [S/n] ',
                          'fr': 'Les relancer ? [O/n] '},
    'queue_cancel': {'pt': 'Ctrl-C cancela tudo', 'en': 'Ctrl-C cancels all', 'es': 'Ctrl-C cancela todo', 'fr': 'Ctrl-C annule tout'},
    'queue_dropped': {'pt': 'Cancelado; {n} pedido(s) da fila descartado(s).', 'en': 'Cancelled; {n} queued request(s) dropped.',
                      'es': 'Cancelado; {n} pedido(s) de la cola descartado(s).', 'fr': 'Annulé ; {n} demande(s) en attente supprimée(s).'},
    'busy': {'pt': 'Espere a fila terminar (ou Ctrl-C cancela) para usar {c}.', 'en': 'Wait for the queue to finish (or Ctrl-C cancels) to use {c}.',
             'es': 'Espera a que termine la cola (o Ctrl-C cancela) para usar {c}.', 'fr': 'Attendez la fin de la file (ou Ctrl-C annule) pour utiliser {c}.'},
    'skipped': {'pt': '↷ pulado: a resposta anterior já cobre "{v}"', 'en': '↷ skipped: the previous answer already covers "{v}"',
                'es': '↷ omitido: la respuesta anterior ya cubre "{v}"', 'fr': '↷ ignoré : la réponse précédente couvre déjà « {v} »'},
    'no_versions': {'pt': 'Não deu para buscar as versões agora.', 'en': 'Could not fetch the releases now.',
                    'es': 'No se pudieron obtener las versiones.', 'fr': 'Impossible de récupérer les versions.'},
    'versions_title': {'pt': 'O que mudou · mais recente primeiro · /versions N abre a nota técnica',
                       'en': 'What changed · newest first · /versions N opens the technical note',
                       'es': 'Qué cambió · lo más reciente primero · /versions N abre la nota técnica',
                       'fr': 'Ce qui a changé · plus récent en premier · /versions N ouvre la note technique'},
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
/tool auto|{tools} Escolher ferramenta (auto = decide sozinho)
/usage [watch]   /limits                Uso real de cada conta em barras (watch atualiza a cada 30s)
/clear                                   Limpar a conversa e começar do zero
/compact                                 Resumir a conversa para economizar contexto
/queue [rm|up|down N|clear]              Ver e editar a fila (tirar, subir, descer, esvaziar)
/versions [N]                            O que mudou em cada versão (N abre a nota técnica)
/claude:usage  /codex:status  /agy:usage  Comandos nativos de cada ferramenta (Tab lista todos)
/model [chave valor]                     Ver/alterar modelos ({models})
/account [list|add|login|remove] [codex|agy|grok] NOME  Contas (Claude por padrão; quantas quiser)
/key [add|remove] [{keys}] [NOME] Chaves de API (várias por ferramenta, usadas em ordem)
/status   /reset                         Status das contas / zerar cooldowns
/sync [--dry-run]                        Compartilhar MCPs e skills entre Claude, Codex e agy
/mode default|plan|edits|auto|yolo      Modo de permissão (vale para Claude, Codex e agy)
/setup                                   Guia de primeiro acesso: logins e chaves que faltam
/terminal-setup                          Como quebrar linha (Shift+Enter) no seu terminal
/lang pt|en|es|fr                        Mudar idioma
/skills                                  Skills e comandos do projeto e do usuário (/nome args executa; Tab completa)
/history                                 Mostrar conversa
/sent [agente-N]  /trace [agente-N]      Ver o que foi enviado / eventos
/sessions   /resume ID   /new            Sessões salvas
/parallel codex: tarefa | codex: tarefa  2–4 análises em paralelo
/exit                                    Sair""",
        'en': """Type normally and press Enter. Arrows: line history. Ctrl-C: cancel answer / exit. Ctrl-D: exit.
/tool auto|{tools} Pick tool (auto = decides for you)
/usage [watch]   /limits                Real usage of each account as bars (watch refreshes every 30s)
/clear                                   Clear the conversation and start over
/compact                                 Summarize the conversation to save context
/queue [rm|up|down N|clear]              See and edit the queue (remove, move up/down, empty)
/versions [N]                            What changed in each release (N opens the technical note)
/claude:usage  /codex:status  /agy:usage  Native commands of each tool (Tab lists them all)
/model [key value]                       Show/change models ({models})
/account [list|add|login|remove] [codex|agy|grok] NAME  Accounts (Claude by default; as many as you want)
/key [add|remove] [{keys}] [NAME] API keys (several per tool, tried in order)
/status   /reset                         Account status / clear cooldowns
/sync [--dry-run]                        Share MCPs and skills across Claude, Codex and agy
/mode default|plan|edits|auto|yolo      Permission mode (applies to Claude, Codex and agy)
/setup                                   First-run guide: logins and keys still missing
/terminal-setup                          How to get new lines (Shift+Enter) in your terminal
/lang pt|en|es|fr                        Change language
/skills                                  Project and user skills/commands (/name args runs; Tab completes)
/history                                 Show conversation
/sent [agente-N]  /trace [agente-N]      What was sent / events
/sessions   /resume ID   /new            Saved sessions
/parallel codex: task | codex: task      2–4 analyses in parallel
/exit                                    Exit""",
        'es': """Escribe normalmente y pulsa Enter. Flechas: historial. Ctrl-C: cancela respuesta / sale. Ctrl-D: sale.
/tool auto|{tools} Elegir herramienta (auto = decide sola)
/usage [watch]   /limits                Uso real de cada cuenta en barras (watch actualiza cada 30s)
/clear                                   Limpiar la conversación y empezar de cero
/compact                                 Resumir la conversación para ahorrar contexto
/queue [rm|up|down N|clear]              Ver y editar la cola (quitar, subir, bajar, vaciar)
/versions [N]                            Qué cambió en cada versión (N abre la nota técnica)
/claude:usage  /codex:status  /agy:usage  Comandos nativos de cada herramienta (Tab los lista)
/model [clave valor]                     Ver/cambiar modelos ({models})
/account [list|add|login|remove] [codex|agy|grok] NOMBRE  Cuentas (Claude por defecto; las que quieras)
/key [add|remove] [{keys}] [NOMBRE] Claves de API (varias por herramienta, en orden)
/status   /reset                         Estado de cuentas / limpiar cooldowns
/sync [--dry-run]                        Compartir MCPs y skills entre Claude, Codex y agy
/mode default|plan|edits|auto|yolo      Modo de permisos (Claude, Codex y agy)
/setup                                   Guía de primer acceso: logins y claves que faltan
/terminal-setup                          Cómo hacer saltos de línea (Shift+Enter) en tu terminal
/lang pt|en|es|fr                        Cambiar idioma
/skills                                  Skills y comandos del proyecto y del usuario (/nombre args ejecuta; Tab completa)
/history                                 Mostrar conversación
/sent [agente-N]  /trace [agente-N]      Lo enviado / eventos
/sessions   /resume ID   /new            Sesiones guardadas
/parallel codex: tarea | codex: tarea    2–4 análisis en paralelo
/exit                                    Salir""",
        'fr': """Tapez normalement puis Entrée. Flèches : historique. Ctrl-C : annule la réponse / quitte. Ctrl-D : quitte.
/tool auto|{tools} Choisir l'outil (auto = choix automatique)
/usage [watch]   /limits                Usage réel de chaque compte en barres (watch rafraîchit toutes les 30 s)
/clear                                   Effacer la conversation et repartir de zéro
/compact                                 Résumer la conversation pour économiser du contexte
/queue [rm|up|down N|clear]              Voir et modifier la file (retirer, monter, descendre, vider)
/versions [N]                            Ce qui a changé à chaque version (N ouvre la note technique)
/claude:usage  /codex:status  /agy:usage  Commandes natives de chaque outil (Tab les liste)
/model [clé valeur]                      Voir/changer les modèles ({models})
/account [list|add|login|remove] [codex|agy|grok] NOM  Comptes (Claude par défaut ; autant que voulu)
/key [add|remove] [{keys}] [NOM] Clés API (plusieurs par outil, essayées dans l'ordre)
/status   /reset                         État des comptes / réinitialiser les cooldowns
/sync [--dry-run]                        Partager MCP et skills entre Claude, Codex et agy
/mode default|plan|edits|auto|yolo      Mode de permissions (Claude, Codex et agy)
/setup                                   Guide de démarrage : connexions et clés manquantes
/terminal-setup                          Comment faire un saut de ligne (Maj+Entrée) dans votre terminal
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


def set_lang(code):
    LANG[0] = code
    ui.LANG[0] = code  # the screen's own texts (badges, spinner, hints) follow too


# Tool lists in the help text come from the tool registry
TOOL_TEXT = {'tools': '|'.join(TOOLS), 'keys': '|'.join(KEY_NAMES), 'models': ', '.join(config_tool.MODELS)}


def tr(key, **values):
    return T[key][LANG[0]].format(**{**TOOL_TEXT, **values})


def choose_lang():
    for number, (code, name) in enumerate(LANGS.items(), 1):
        display(f'  {number}) {name}')
    try:
        answer = input(f"{tr('lang_pick')} [1-4]: ").strip()
    except (EOFError, KeyboardInterrupt):
        answer = ''
    codes = list(LANGS)
    if answer.isdigit() and 1 <= int(answer) <= 4:
        set_lang(codes[int(answer) - 1])
    elif answer in LANGS:
        set_lang(answer)
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
        # the queue worker and the input loop both save
        with SAVE_LOCK:
            self.data['updated'] = time.time()
            for _ in range(5):
                try:
                    # a running job holds its screen view (not saved) and keeps appending events meanwhile
                    text = json.dumps(self.data, ensure_ascii=False, indent=2, default=lambda value: None)
                    break
                except RuntimeError:  # changed while being written out: take it again
                    continue
            else:
                return
            fd, name = tempfile.mkstemp(dir=self.path.parent, prefix='.session-')
            try:
                with os.fdopen(fd, 'w') as stream:
                    stream.write(text)
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
    if provider not in TOOLS:
        return profile
    # codex shows its effort too; an empty model is the tool's own default
    label = f"{values['codex']} {values['codex_effort']}" if provider == 'codex' else values[provider] or \
        {'openrouter': 'gratuito', 'deepseek': 'deepseek-chat'}.get(provider, 'padrão')
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
    ACTIVE.add(runner)
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
        ACTIVE.discard(runner)
        ui.SPINNER.stop()
        for job in jobs:
            prefix = f"[{job['id']}] " if parallel else ''
            if job['status'] == 'done':
                ui.out('\n' + ui.gradient(ui.MARK) + ' ' + ui.dim(f"{prefix}{tr('took', t=ui.elapsed(started))} · {job.get('provider', job['engine'])}"))
            elif job['status'] == 'cancelled':
                ui.out('\n' + ui.yellow(f"{prefix}{ui.MARK} {tr('job_cancelled')}"))
            else:
                ui.out('\n' + ui.red(f"{prefix}✗ {tr('job_failed')}"))
        for job in jobs:
            job.pop('view', None)
            if job.get('answers') and job['status'] == 'done':
                session.data['messages'].append({'role': 'assistant', 'content': '\n'.join(job['answers']),
                                                'agent': job['id'], 'provider': job.get('provider', job['engine'])})
        session.save()
        if time.time() - started > 20:
            # Long run finished: ring the bell and mark the tab, so it's noticed from another window
            ui.attention(f"{ui.MARK} {tr('took', t=ui.elapsed(started))} · Omni AI · {session.data.get('title') or session.id}")


class Queue:
    """Requests typed while another one runs wait their turn here; the input stays free the whole time.

    A request is a callable or a dict spec ({'kind': 'ask' | 'skill' | 'parallel' | 'compact', ...}) that run()
    executes; specs are what gets saved with the session, so a closed chat can pick the line up again.
    """

    def __init__(self, skip=None, run=None, changed=None):
        self.items, self.current, self.current_spec = [], None, None
        self.skip, self.run, self.changed = skip, run, changed
        self.condition = threading.Condition()
        threading.Thread(target=self._loop, daemon=True).start()

    def put(self, label, work, waited=None):
        with self.condition:
            # only a request that actually waited gets a "from the queue" line when its turn comes
            busy = self.current is not None or bool(self.items)
            self.items.append((label, work, busy if waited is None else waited))
            self.condition.notify()
        self._changed()

    def busy(self):
        with self.condition:
            return self.current is not None or bool(self.items)

    def snapshot(self):
        with self.condition:
            return self.current, [label for label, _, _ in self.items]

    def pending(self):
        """Saveable specs, the running one first (it reruns on resume: it never finished)."""
        with self.condition:
            specs = ([dict(self.current_spec, label=self.current)] if isinstance(self.current_spec, dict) else []) + \
                    [dict(work, label=label) for label, work, _ in self.items if isinstance(work, dict)]
        return specs

    def _changed(self):
        if self.changed:
            self.changed(self.pending())

    def _loop(self):
        while True:
            with self.condition:
                while not self.items:
                    self.condition.wait()
                label, work, waited = self.items.pop(0)
                self.current, self.current_spec = label, work
            self._changed()
            if waited:
                ui.out('\n' + ui.dim(tr('next_up', v=ui.short(label.split('\n')[0], ui.width() - 20))))
            try:
                if waited and self.skip and self.skip(label):
                    ui.out(ui.dim(tr('skipped', v=ui.short(label.split('\n')[0], ui.width() - 50))))
                    continue
                work() if callable(work) else self.run(work)
            except Exception as error:  # a broken request must not take the worker (and the queue) down
                display(tr('error', e=error))
            finally:
                with self.condition:
                    self.current = self.current_spec = None
                    self.condition.notify_all()
                self._changed()

    def remove(self, number):
        """Take a waiting request out (1 = next in line); returns its label."""
        with self.condition:
            self._check(number)
            label = self.items.pop(number - 1)[0]
        self._changed()
        return label

    def move(self, number, offset):
        """Move a waiting request up (offset -1) or down (+1); returns its new position."""
        with self.condition:
            self._check(number)
            target = min(max(number - 1 + offset, 0), len(self.items) - 1)
            self.items.insert(target, self.items.pop(number - 1))
        self._changed()
        return target + 1

    def clear(self):
        """Drop every waiting request, leave the running one alone; returns how many were dropped."""
        with self.condition:
            dropped = len(self.items)
            self.items.clear()
        self._changed()
        return dropped

    def _check(self, number):
        if not 1 <= number <= len(self.items):
            raise ValueError(tr('queue_bad', n=len(self.items)))

    def cancel(self):
        """Drop what waits and stop what runs; returns how many waiting requests were dropped."""
        dropped = self.clear()
        for runner in list(ACTIVE):
            runner.cancel()
        return dropped

    def wait(self, timeout=10):
        with self.condition:
            self.condition.wait_for(lambda: self.current is None and not self.items, timeout)


def run_spec(session, spec):
    """Run a saved queue request in the given session."""
    kind = spec['kind']
    if kind == 'ask':
        execute(session, [(spec['engine'], spec['text'])])
    elif kind == 'skill':
        execute(session, [(spec['tool'], spec['prompt'])], raw=spec.get('raw', False))
    elif kind == 'parallel':
        execute(session, [tuple(task) for task in spec['tasks']], parallel=True)
    elif kind == 'compact':
        compact(session)
    else:
        raise ValueError(f'pedido desconhecido na fila: {kind}')


def queue_command(queue, argument):
    """/queue · /queue rm N · /queue up N · /queue down N · /queue clear"""
    action, _, number = argument.strip().partition(' ')
    if action in ('rm', 'remove', 'up', 'down'):
        if not number.strip().isdigit():
            raise ValueError(tr('queue_usage'))
        n = int(number)
        if action in ('rm', 'remove'):
            display(tr('queue_removed', v=ui.short(queue.remove(n).split('\n')[0], 60)))
        else:
            display(tr('queue_moved', n=queue.move(n, -1 if action == 'up' else 1)))
    elif action == 'clear':
        display(tr('queue_cleared', n=queue.clear()))
    elif action:
        raise ValueError(tr('queue_usage'))
    current, waiting = queue.snapshot()
    if current is None and not waiting:
        display(tr('queue_empty'))
        return
    if current is not None:
        display(f"▶ {ui.short(current.split(chr(10))[0], ui.width() - 4)}")
    for i, label in enumerate(waiting, 1):
        display(f"{i:>2}. {ui.short(label.split(chr(10))[0], ui.width() - 5)}")


def activity(queue):
    """Lines above the input: the request running now and the ones waiting, in order."""
    current, waiting = queue.snapshot()
    if current is None:
        return []
    room = ui.width() - 30
    since = ui.elapsed(ui.SPINNER.started) if ui.SPINNER.started else ''
    lines = [f"<running>{ui.SPINNER.frame()} {ui.escape(ui.SPINNER.label or tr('working'))}</running> "
             f"<queued>{since} · {ui.escape(ui.short(current.split(chr(10))[0], room))} · {ui.escape(tr('queue_cancel'))}</queued>"]
    if waiting:
        lines.append(f"<queued>  {ui.escape(tr('queued', n=len(waiting)))}</queued>")
        lines += [f"<queued>  {i}. {ui.escape(ui.short(label.split(chr(10))[0], room))}</queued>" for i, label in enumerate(waiting[:5], 1)]
        if len(waiting) > 5:
            lines.append(f'<queued>  … +{len(waiting) - 5}</queued>')
    return lines


RELEASES = 'https://api.github.com/repos/samuelrms/omni-ai-cli/releases?per_page=15'
COMMITS = 'https://api.github.com/repos/samuelrms/omni-ai-cli/commits?sha=main&per_page=40'
CONVENTIONAL = re.compile(r'^(feat|fix|perf)(\([^)]*\))?!?:\s*(.+)')
CHANGE = re.compile(r'^\s*[*-]\s+(?:\*\*[^*]+\*\*:?\s*)?(.+?)(?:\s+\(\[[0-9a-f]{7}\].*)?$')


def github(url):
    import urllib.request
    request = urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'omni-ai-cli'})
    with urllib.request.urlopen(request, timeout=10) as resp:
        return json.loads(resp.read().decode())


def releases():
    """Published releases; before the first one, each feat/fix merged on main stands in as an entry."""
    items = [r for r in github(RELEASES) if not r.get('draft')]
    if items:
        return items
    changes = []
    for item in github(COMMITS):
        subject, _, body = item['commit']['message'].partition('\n')
        match = CONVENTIONAL.match(subject)
        if match:
            changes.append({'tag_name': item['sha'][:7], 'name': match.group(3), 'commit': True, 'published_at': item['commit']['committer']['date'],
                            'body': f'* {match.group(3)}\n\n{body.strip()}'})
    return changes


def show_versions(argument):
    """Timeline like a changelog page: simple view of every release, the technical note only for the one asked."""
    try:
        items = releases()
    except (OSError, ValueError):
        display(tr('no_versions'))
        return
    if argument:
        chosen = next((r for i, r in enumerate(items, 1) if argument in (str(i), r.get('tag_name'), r.get('tag_name', '').lstrip('v'))), None)
        if not chosen:
            raise ValueError(tr('no_versions'))
        ui.out(ui.bold(chosen.get('name') or chosen['tag_name']) + ui.dim(' · ' + chosen.get('published_at', '')[:10]))
        ui.out(ui.markdown(chosen.get('body') or ''))
        return
    ui.out(ui.dim(tr('versions_title')))
    for i, item in enumerate(items, 1):
        # simple view: the change titles without commit links and scopes
        changes = [item['name']] if item.get('commit') else \
            [m.group(1) for m in map(CHANGE.match, (item.get('body') or '').splitlines()) if m]
        ui.out(f"\n{ui.gradient(ui.MARK)} {ui.bold(item.get('tag_name', ''))} {ui.dim(item.get('published_at', '')[:10] + f' · {i}')}")
        for change in changes[:4]:
            ui.out('  ' + ui.short(change, ui.width() - 4))
        if len(changes) > 4:
            ui.out(ui.dim(f'  … +{len(changes) - 4}'))


SKIP_SYSTEM = ('You check a queue of chat requests. Given the previous answer and the next queued request, reply "yes" '
               'only if the previous answer already fully resolves the next request, so running it would be redundant. '
               'Reply "no" if it asks anything new, wants an action, or you are unsure. One word: yes or no.')


def already_answered(request, answer):
    """Free classifiers only: a queued request that the answer before it already settled is not sent again."""
    from . import cli
    from .pii import PIIMasker
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
                 '/status': ['--status'], '/sync': ['--sync'], '/update': ['--update'], '/reset': ['--reset']}


def run_tool(args):
    """Run a management command attached to this terminal (login prompts need the TTY)."""
    try:
        subprocess.call(module_cmd('cli') + args, env=module_env())
    except KeyboardInterrupt:
        pass


BUILTIN = ['/usage', '/clear', '/compact', '/queue', '/versions', '/setup', '/terminal-setup', '/tool', '/model', '/account', '/key', '/status', '/reset', '/sync', '/update', '/mode', '/lang',
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
        if onboarding.first_run():
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


if __name__ == '__main__':
    main()
