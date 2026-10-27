"""First-run guide: what each tool needs (install, login, keys), with the live state of this machine.

Shown once on the first chat launch, and any time with /setup (or `ai --setup`).
"""
import json
import os
from pathlib import Path
import subprocess

from . import config as config_tool
from . import ui
from .osutils import resolve_bin
from .tools import CLASSIFIER_KEYS, TOOLS

MARKER = config_tool.CONFIG_DIR / 'onboarded'

T = {
    'title': {'pt': 'Primeiros passos no Omni AI', 'en': 'Getting started with Omni AI',
              'es': 'Primeros pasos con Omni AI', 'fr': 'Premiers pas avec Omni AI'},
    'intro': {'pt': 'O Omni usa as assinaturas que você já tem. Basta UMA ferramenta pronta para começar; as outras entram quando a primeira bater no limite.',
              'en': 'Omni uses the subscriptions you already have. ONE ready tool is enough to start; the others step in when the first hits its limit.',
              'es': 'Omni usa las suscripciones que ya tienes. Basta UNA herramienta lista para empezar; las demás entran cuando la primera llega al límite.',
              'fr': "Omni utilise vos abonnements existants. UN outil prêt suffit pour commencer ; les autres prennent le relais quand le premier atteint sa limite."},
    'agents': {'pt': 'Ferramentas com assinatura', 'en': 'Subscription tools', 'es': 'Herramientas con suscripción', 'fr': 'Outils avec abonnement'},
    'keys': {'pt': 'Chaves de API', 'en': 'API keys', 'es': 'Claves de API', 'fr': 'Clés API'},
    'ready': {'pt': 'pronto', 'en': 'ready', 'es': 'listo', 'fr': 'prêt'},
    'no_login': {'pt': 'instalado, falta login', 'en': 'installed, needs login', 'es': 'instalado, falta iniciar sesión', 'fr': 'installé, connexion requise'},
    'missing': {'pt': 'não instalado', 'en': 'not installed', 'es': 'no instalado', 'fr': 'non installé'},
    'saved': {'pt': 'salva', 'en': 'saved', 'es': 'guardada', 'fr': 'enregistrée'},
    'none': {'pt': 'não configurada', 'en': 'not set', 'es': 'sin configurar', 'fr': 'non configurée'},
    'install': {'pt': 'instalar', 'en': 'install', 'es': 'instalar', 'fr': 'installer'},
    'login': {'pt': 'login', 'en': 'login', 'es': 'login', 'fr': 'connexion'},
    'create': {'pt': 'criar em', 'en': 'create at', 'es': 'crear en', 'fr': 'créer sur'},
    'save': {'pt': 'salvar', 'en': 'save', 'es': 'guardar', 'fr': 'enregistrer'},
    'free': {'pt': 'grátis', 'en': 'free', 'es': 'gratis', 'fr': 'gratuit'},
    'paid': {'pt': 'pago por uso', 'en': 'pay per use', 'es': 'pago por uso', 'fr': "payant à l'usage"},
    'fallback': {'pt': 'reserva quando a assinatura acaba', 'en': 'fallback when the subscription runs out',
                 'es': 'respaldo cuando se acaba la suscripción', 'fr': "secours quand l'abonnement est épuisé"},
    'groq_use': {'pt': 'classifica cada pergunta e escolhe a ferramenta', 'en': 'classifies each prompt and picks the tool',
                 'es': 'clasifica cada pregunta y elige la herramienta', 'fr': "classe chaque question et choisit l'outil"},
    'or_use': {'pt': 'responde perguntas simples sem gastar assinatura', 'en': 'answers simple questions without using a subscription',
               'es': 'responde preguntas simples sin gastar suscripción', 'fr': 'répond aux questions simples sans consommer d’abonnement'},
    'shell_tip': {'pt': 'Dentro do chat, rode comandos de terminal saindo com /exit, ou abra outra aba.',
                  'en': 'Run terminal commands outside the chat (/exit) or in another tab.',
                  'es': 'Ejecuta comandos de terminal fuera del chat (/exit) o en otra pestaña.',
                  'fr': 'Lancez les commandes terminal hors du chat (/exit) ou dans un autre onglet.'},
    'next_ok': {'pt': 'Tudo certo para começar. Rode /setup quando quiser rever este guia.',
                'en': 'All set to start. Run /setup any time to see this guide again.',
                'es': 'Todo listo para empezar. Usa /setup para ver esta guía de nuevo.',
                'fr': 'Tout est prêt. Lancez /setup pour revoir ce guide.'},
    'next_todo': {'pt': 'Nenhuma ferramenta pronta ainda: faça o login de pelo menos uma acima. Depois rode /setup para conferir.',
                  'en': 'No tool is ready yet: log in to at least one above. Then run /setup to check.',
                  'es': 'Aún no hay herramientas listas: inicia sesión en al menos una. Luego usa /setup para comprobar.',
                  'fr': "Aucun outil prêt : connectez-vous à au moins un ci-dessus. Puis lancez /setup pour vérifier."},
}

# name, label, install command, login command
AGENTS = [(name, tool.label, tool.install, tool.login) for name, tool in TOOLS.items() if tool.bin]
WINDOWS_INSTALL = {name: tool.install_windows for name, tool in TOOLS.items() if tool.install_windows}
# The free classifiers come first: without one of them every prompt is labelled by keywords only
KEYS = [(name, 'free', 'groq_use') for name in CLASSIFIER_KEYS] + \
       [(name, 'free', 'or_use') for name in TOOLS if name == 'openrouter'] + \
       [(name, tool.price, 'fallback') for name, tool in TOOLS.items() if tool.key and name != 'openrouter']


def logged_in(tool):
    try:
        if tool == 'claude':
            out = subprocess.run([resolve_bin('claude'), 'auth', 'status'], capture_output=True, text=True, timeout=15).stdout
            return bool(json.loads(out).get('loggedIn'))
        if tool == 'codex':
            return subprocess.run([resolve_bin('codex'), 'login', 'status'], capture_output=True, timeout=15).returncode == 0
        if tool == 'cursor':
            # exits 0 either way; only the text says whether it is logged in
            done = subprocess.run([resolve_bin(TOOLS[tool].bin), 'status'], capture_output=True, text=True, timeout=15)
            return done.returncode == 0 and 'not logged in' not in (done.stdout + done.stderr).lower()
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        return False
    files = {'agy': '~/.gemini/jetski-standalone-oauth-token', 'grok': '~/.grok/auth.json'}
    return Path(files[tool]).expanduser().is_file()


def guide(lang='pt'):
    t = lambda key: T[key].get(lang, T[key]['pt'])
    ok, warn, off = ui.rgb((34, 197, 94), '●'), ui.rgb((234, 179, 8), '◐'), ui.rgb((148, 163, 184), '○')
    lines = ['', ui.gradient(ui.MARK) + ' ' + ui.bold(t('title')), ui.dim(t('intro')), '', ui.bold(t('agents'))]
    any_ready = False
    for tool, label, install, login in AGENTS:
        if resolve_bin(TOOLS[tool].bin) is None:
            lines += [f'  {off} {label:<12} {ui.dim(t("missing"))}',
                      f'      {t("install")}: ' + ui.rgb(ui.TOOL_RGB[tool], WINDOWS_INSTALL.get(tool, install) if os.name == 'nt' else install),
                      f'      {t("login")}:   ' + ui.rgb(ui.TOOL_RGB[tool], login)]
        elif logged_in(tool):
            any_ready = True
            lines.append(f'  {ok} {label:<12} {t("ready")}')
        else:
            lines += [f'  {warn} {label:<12} {t("no_login")}', f'      {t("login")}: ' + ui.rgb(ui.TOOL_RGB[tool], login)]
    lines += ['', ui.bold(t('keys'))]
    for provider, price, use in KEYS:
        env, _, url = config_tool.KEYS[provider]
        if config_tool.stored_key(provider)[0]:
            lines.append(f'  {ok} {provider:<11} {t("saved")} · {ui.dim(t(use))}')
        else:
            lines += [f'  {off} {provider:<11} {ui.dim(t("none") + " · " + t(price) + " · " + t(use))}',
                      f'      {t("create")} https://{url}  →  {t("save")}: ' + ui.bold(f'/key {provider}')]
    lines += ['', ui.dim(t('shell_tip')), t('next_ok') if any_ready else t('next_todo'), '']
    return '\n'.join(lines)


def first_run():
    """True only the first time; later launches skip the guide."""
    if MARKER.exists():
        return False
    MARKER.parent.mkdir(parents=True, exist_ok=True)
    MARKER.write_text('1')
    return True


def main():
    lang = (os.environ.get('LANG', 'pt')[:2])
    try:
        lang = Path('~/.config/smart-ai/lang').expanduser().read_text().strip() or lang
    except OSError:
        pass
    print(guide(lang if lang in T['title'] else 'pt'))


if __name__ == '__main__':
    main()
