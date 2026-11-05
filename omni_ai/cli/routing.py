"""Routing: Claude with account/key fallback, the runner map, and auto classify-then-route."""
from .. import config as config_tool
from .. import usage
from ..tools import CLIS
from .base import (
    RATE_LIMITED, NOT_READY, RED, GREEN, YELLOW, CYAN, DIM, NC,
    say, config, emit, installed, cooldown_left,
)
from .classifier import classify, CLASSIFIED_BY
from .executors import (
    run_claude, run_codex, run_cursor, run_agy, run_openrouter, run_grok, run_deepseek,
    over_ceiling, key_account,
)


def claude_with_fallback(prompt):
    for profile in config_tool.enabled_profiles():
        left = cooldown_left(profile)
        if left:
            say(f"{DIM}  ⏳ Perfil '{profile}' em cooldown (restam: {left // 60}m{left % 60}s){NC}")
            continue
        conf = config_tool.profiles()['profiles'].get(profile, {})
        if over_ceiling(f'claude:{profile}', lambda conf=conf: usage.claude_reading(conf), f'Claude {profile}'):
            continue
        result = run_claude(profile, prompt)
        if result == 0:
            return 0
        if result not in (RATE_LIMITED, NOT_READY):
            return result
        say(f'{YELLOW}🔄 Tentando próximo perfil...{NC}')
    for name, _, _ in config_tool.stored_keys('claude'):
        account = key_account('', name)
        if cooldown_left(account):
            continue
        say(f'{YELLOW}🔑 Claude: tentando com a chave {name}{NC}')
        result = run_claude(account, prompt)
        if result == 0:
            return 0
        if result != RATE_LIMITED:
            return result
    say(f'\n{RED}✗ Todas as contas Claude estão em rate limit!{NC}')
    say(f'{YELLOW}  Tentando resolver com ferramentas alternativas...{NC}\n')
    for tool, runner in (('codex', run_codex), ('agy', run_agy)):
        if installed(tool):
            say(f'{YELLOW}↪ Forçando {tool} como último recurso...{NC}')
            if runner(prompt) == 0:
                return 0
    say(f'\n{RED}✗ Nenhuma ferramenta disponível. Aguarde o cooldown.{NC}')
    return 1


def runners():
    """What runs each tool; every entry in tools.TOOLS needs one (the tests check)."""
    return {'claude': claude_with_fallback, 'codex': run_codex, 'cursor': run_cursor, 'agy': run_agy,
            'openrouter': run_openrouter, 'grok': run_grok, 'deepseek': run_deepseek}


def route(prompt, tool='auto'):
    forced = runners()
    if tool in forced:
        return forced[tool](prompt)
    label = classify(prompt)
    # the chat shows this as one short line: why this tool was picked
    emit({'type': 'smart.route', 'label': label, 'by': CLASSIFIED_BY[0]})
    if label == 'complex':
        say(f'{DIM}📊 Classificado como: {CYAN}query complexa{NC}{DIM} → usando Claude{NC}\n')
        return forced['claude'](prompt)
    say(f'{DIM}📊 Classificado como: {YELLOW}query simples{NC}{DIM} → roteando para alternativa{NC}\n')
    routing = config().get('routing', {})
    order = [routing.get('prefer_tool_for_simple', 'codex')] + routing.get('fallback_order', ['openrouter', 'codex', 'agy'])
    # Grok, DeepSeek and Cursor cost money, so auto mode only uses them when listed in routing.fallback_order
    for name in dict.fromkeys(t for t in order if t in forced and t != 'claude'):
        if name == 'grok' and not (installed('grok') or config_tool.stored_key('grok')[0]):
            continue
        if name == 'deepseek' and not config_tool.stored_key('deepseek')[0]:
            continue
        # grok also answers over its API key (checked above); the other CLIs need to be installed
        if name in CLIS and name != 'grok' and not installed(name):
            continue
        if forced[name](prompt) == 0:
            return 0
    say(f'\n{DIM}→ Usando Claude como fallback final{NC}')
    return forced['claude'](prompt)
