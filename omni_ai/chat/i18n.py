"""Language, translations, and screen-safe printing for the chat."""
import os
import re
import threading
from pathlib import Path

from .. import config as config_tool
from .. import ui
from ..tools import KEY_NAMES, TOOLS

ENGINES = {'auto', *TOOLS}
MODES = ('default', 'plan', 'edits', 'auto', 'yolo')
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
    'new_version': {'pt': 'Versão nova disponível: {v} · {s} · /update atualiza · /versions mostra o que mudou',
                    'en': 'New version available: {v} · {s} · /update installs it · /versions shows what changed',
                    'es': 'Nueva versión disponible: {v} · {s} · /update la instala · /versions muestra qué cambió',
                    'fr': 'Nouvelle version disponible : {v} · {s} · /update l\'installe · /versions montre ce qui a changé'},
    'versions_title': {'pt': 'O que mudou · mais recente primeiro · /versions N abre a nota técnica',
                       'en': 'What changed · newest first · /versions N opens the technical note',
                       'es': 'Qué cambió · lo más reciente primero · /versions N abre la nota técnica',
                       'fr': 'Ce qui a changé · plus récent en premier · /versions N ouvre la note technique'},
    'route_complex': {'pt': 'tarefa de código → Claude · classificado por {by}', 'en': 'coding task → Claude · labelled by {by}',
                      'es': 'tarea de código → Claude · clasificado por {by}', 'fr': 'tâche de code → Claude · classée par {by}'},
    'route_simple': {'pt': 'pergunta simples → ferramenta mais barata · classificado por {by}',
                     'en': 'simple question → cheapest tool · labelled by {by}',
                     'es': 'pregunta simple → herramienta más barata · clasificado por {by}',
                     'fr': 'question simple → outil le moins cher · classée par {by}'},
    'by_rules': {'pt': 'palavras-chave', 'en': 'keywords', 'es': 'palabras clave', 'fr': 'mots-clés'},
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
/doctor                                 Diagnóstico de instalação, contas e chaves
/sync [--dry-run]                        Compartilhar MCPs e skills entre Claude, Codex e agy
/mode default|plan|edits|auto|yolo      Modo de permissão (vale para Claude, Codex e agy)
/setup [install]                         Guia de primeiro acesso; install instala as ferramentas que faltam
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
/doctor                                 Diagnose install, accounts and keys
/sync [--dry-run]                        Share MCPs and skills across Claude, Codex and agy
/mode default|plan|edits|auto|yolo      Permission mode (applies to Claude, Codex and agy)
/setup [install]                         First-run guide; install installs the missing tools
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
/doctor                                 Diagnóstico de instalación, cuentas y claves
/sync [--dry-run]                        Compartir MCPs y skills entre Claude, Codex y agy
/mode default|plan|edits|auto|yolo      Modo de permisos (Claude, Codex y agy)
/setup [install]                         Guía de primer acceso; install instala las herramientas que faltan
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
/doctor                                 Diagnostic installation, comptes et clés
/sync [--dry-run]                        Partager MCP et skills entre Claude, Codex et agy
/mode default|plan|edits|auto|yolo      Mode de permissions (Claude, Codex et agy)
/setup [install]                         Guide de démarrage ; install installe les outils manquants
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


LANG_NAMES = {'pt': 'Brazilian Portuguese', 'es': 'Spanish', 'fr': 'French'}
