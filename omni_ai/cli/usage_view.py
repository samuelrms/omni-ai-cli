"""The `ai --usage` screen: every tool and account, % used per window, limits and today's spend."""
import json
import sys
import time

from .. import config as config_tool
from .. import usage
from ..tools import CLIS, TOOLS
from .base import (
    LOG_FILE, RED, GREEN, YELLOW, BOLD, DIM, NC,
    say, installed, state, cooldown_left,
)
from .limits import active_limit, limit_until
from .executors import key_account


def ago(seconds):
    seconds = int(seconds)
    return f'{seconds // 3600}h{seconds % 3600 // 60:02d}m' if seconds >= 3600 else f'{seconds // 60}m'


def show_usage(watch=False):
    """One screen for every tool and account: who it is, % used of each window (when the tool says),
    or the tool's own limit message. With watch, redraws every 30s until Ctrl-C."""
    if not watch:
        return render_usage()
    try:
        while True:
            sys.stdout.write('\033[H\033[2J')
            render_usage()
            say(f"\n{DIM}atualiza a cada 30s · {time.strftime('%H:%M:%S')} · Ctrl-C sai{NC}")
            time.sleep(30)
    except KeyboardInterrupt:
        say()
    return 0


def spend_today(today):
    """tool: (US$, tokens, requests) from the chat's per-request log, for one day."""
    path = LOG_FILE.with_name('costs.jsonl')
    totals = {}
    try:
        rows = path.read_text(errors='replace').splitlines()
    except OSError:
        return totals
    for row in rows:
        try:
            item = json.loads(row)
        except ValueError:
            continue
        if not str(item.get('at', '')).startswith(today):
            continue
        cost, tokens, count = totals.get(item.get('tool', '?'), (0.0, 0, 0))
        totals[item.get('tool', '?')] = (cost + float(item.get('cost') or 0), tokens + int(item.get('tokens') or 0), count + 1)
    return totals


def render_usage():
    limits = state().get('limits', {})
    today = time.strftime('%Y-%m-%d')
    lines = LOG_FILE.read_text(errors='replace').splitlines() if LOG_FILE.is_file() else []
    skip, alert = usage.thresholds()

    def calls(tool):
        return sum(1 for l in lines if l.startswith('[' + today) and f'tool={tool} ' in l)

    def limit_line(key):
        item = active_limit(limits.get(key))
        if not item:
            return ''
        back = f" · volta em {ago(item['until'] - time.time())}" if limit_until(item['text'], item['at']) else ''
        return f"{YELLOW}limitado{NC} {DIM}· {item['text']} (visto há {ago(time.time() - item['at'])}{back}){NC}"

    def who(info):
        return f" {DIM}· {info['email']}{' · ' + info['plan'] if info.get('plan') else ''}{NC}" if info and info.get('email') else ''

    def windows(items, pad=15):
        for window in items:
            used = window['used']
            color = RED if used >= max(alert + 10, 90) else YELLOW if used >= alert - 10 else GREEN
            reset = ''
            if window.get('resets'):
                reset = f"  {DIM}reseta em {usage.resets_in(window['resets'])} · {usage.clock(window['resets'])}{NC}"
            name = usage.window_label(window['minutes'], window.get('label'))
            filled = usage.bar(used, 24).count('█')
            say(f"{' ' * pad}{DIM}{name:<7}{NC} {color}{'█' * filled}{NC}{DIM}{'░' * (24 - filled)}{NC} {BOLD}{color}{used:>3.0f}%{NC}{reset}")

    def keys_of(tool, prefix):
        for name, value, origin in config_tool.stored_keys(tool):
            account = key_account(prefix, name)
            text = limit_line(account if prefix else f'claude:{account}')
            left = cooldown_left(account)
            status = (text or f'{YELLOW}limitada{NC}') + f' {DIM}· volta em {left // 60}m{NC}' if left else text or f'{GREEN}configurada{NC}'
            say(f"  {'chave ' + name:<12} {status} {DIM}· {value[:7]}…{value[-4:]}{' · variável de ambiente' if origin == 'env' else ''}{NC}")

    def state_of(live, fallback_key, cooldown=0):
        if live and live.get('windows'):
            top = usage.peak(live['windows'])
            if live.get('blocked') or top >= 100:
                return f'{RED}no limite{NC}'
            note = f" {DIM}(última leitura há {ago(time.time() - live['at'])}; a Anthropic pediu uma pausa nas consultas){NC}" \
                if live.get('stale') else ''
            if top >= skip:
                return f'{YELLOW}acima do teto de {skip:.0f}% · o Omni pula{NC}' + note
            return f'{GREEN}disponível{NC}' + note
        text = limit_line(fallback_key)
        if not text and not cooldown and fallback_key.startswith('claude:') and usage.load_cache().get('backoff', {}).get(fallback_key, 0) > time.time():
            text = f"{GREEN}disponível{NC} {DIM}· uso indisponível agora: a Anthropic pediu uma pausa nas consultas{NC}"
        if cooldown:
            return (text or f'{YELLOW}limitado{NC}') + f' {DIM}· o Omni tenta de novo em {cooldown // 60}m{NC}'
        return text or f'{GREEN}sem limite registrado{NC}'

    profiles = [(n, c) for n, c in config_tool.ordered(config_tool.profiles()) if c.get('enabled', True)]
    codex = [('primary', None)] + [(n, config_tool.tool_env('codex', c)) for n, c in config_tool.tool_accounts('codex', True)] \
        if installed('codex') else []
    or_keys = config_tool.stored_keys('openrouter')
    if sys.stdout.isatty():
        sys.stdout.write(f'{DIM}consultando contas…{NC}\r')
        sys.stdout.flush()
    data = usage.snapshot(profiles, codex, or_keys)
    if sys.stdout.isatty():
        sys.stdout.write('\033[K')
    say(f'{BOLD}Uso e limites{NC}  {DIM}% usado de cada janela · teto do roteador {skip:.0f}% · alerta em {alert:.0f}%{NC}\n')

    say(f'{BOLD}Claude{NC}  {DIM}{calls("claude")} chamadas hoje{NC}')
    for name, conf in config_tool.ordered(config_tool.profiles()):
        if not conf.get('enabled', True):
            say(f'  {name:<12} {DIM}desativada{NC}')
            continue
        info = data.get(('claude-who', name))
        if not info and config_tool.logged_in(conf) is False:
            say(f'  {name:<12} {RED}sem login{NC} {DIM}· /account login {name}{NC}')
            continue
        live = data.get(('claude', name))
        say(f'  {name:<12} {state_of(live, f"claude:{name}", cooldown_left(name))}{who(info)}')
        windows((live or {}).get('windows', []))
    keys_of('claude', '')
    say(f'  {DIM}+ nova conta: /account add NOME · outra chave: /key add claude [NOME]{NC}')
    for tool, label in ((name, TOOLS[name].label) for name in CLIS if TOOLS[name].in_usage):
        say(f'\n{BOLD}{label}{NC}  {DIM}{calls(tool)} chamadas hoje{NC}')
        if not installed(tool):
            say(f'  {RED}não instalado{NC}')
            keys_of(tool, tool)
            say(f"  {DIM}+ usar com chave de API: /key {'add ' if config_tool.stored_keys(tool) else ''}{tool}{NC}")
            continue
        for name, conf in [('primary', None)] + (config_tool.tool_accounts(tool, True) if tool in config_tool.TOOL_HOMES else []):
            account = tool if conf is None else f'{tool}:{name}'
            live = data.get(('codex', name)) if tool == 'codex' else None
            say(f"  {'assinatura' if conf is None else name:<12} {state_of(live, account, cooldown_left(account))}{who(live)}")
            windows((live or {}).get('windows', []))
        keys_of(tool, tool)
        key_hint = f'outra chave: /key add {tool} [NOME]' if config_tool.stored_keys(tool) else f'chave de reserva: /key {tool}'
        say(f'  {DIM}+ nova conta: /account add {tool} NOME · {key_hint}{NC}')
    say(f'\n{BOLD}OpenRouter{NC}  {DIM}{calls("openrouter")} chamadas hoje{NC}')
    if not or_keys:
        say(f'  {DIM}sem chave · /key openrouter{NC}')
    for name, value, _ in or_keys:
        credits = data.get(('openrouter', name))
        label = f"chave {name}"
        if credits and credits['limit']:
            say(f"  {label:<12} {GREEN}configurada{NC} {DIM}· US$ {credits['used']:.2f} de US$ {float(credits['limit']):.2f}{NC}")
            windows([{'minutes': 0, 'used': credits['used'] / float(credits['limit']) * 100}])
        elif credits:
            say(f"  {label:<12} {GREEN}configurada{NC} {DIM}· US$ {credits['used']:.2f} usados · sem teto{NC}")
        else:
            say(f"  {label:<12} {GREEN}configurada{NC}")
    say(f"  {DIM}+ outra chave: /key add openrouter [NOME]{NC}")
    spent = spend_today(today)
    if spent:
        say(f'\n{BOLD}Gasto de hoje{NC}  {DIM}pedidos feitos pelo chat; custo só quando a ferramenta informa{NC}')
        for tool, (cost, tokens, count) in sorted(spent.items(), key=lambda item: -item[1][0]):
            money = f'US$ {cost:.4f}' if cost else 'sem custo informado'
            say(f'  {tool:<12} {money:<22} {DIM}{tokens:,} tokens · {count} pedido(s){NC}'.replace(',', '.'))
        total = sum(cost for cost, _, _ in spent.values())
        if total:
            say(f'  {"total":<12} {BOLD}US$ {total:.4f}{NC}')
    say(f'\n{DIM}Teto e alerta: routing.usage_skip_percent e routing.usage_alert_percent no profiles.json · /usage watch{NC}')
    return 0
