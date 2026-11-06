"""Model settings: the MODELS map, resolution (env > saved > default), validation and the model/env commands."""
import os
import re
import shlex

from ..tools import TOOLS
from .store import MODELS_FILE, read_json, write_json

# key: (default, env override, description); one per tool, plus the two settings that are not a model
MODELS = {name: (tool.model_default, tool.model_env, tool.model_help) for name, tool in TOOLS.items()}
MODELS = dict(list(MODELS.items())[:2] + [('codex_effort', ('high', 'SMART_AI_CODEX_EFFORT', 'Codex: minimal, low, medium, high, xhigh'))]
              + list(MODELS.items())[2:]
              + [('openrouter_paid', ('off', 'SMART_AI_OPENROUTER_PAID', 'OpenRouter: on autoriza modelos pagos (cobrados na sua conta)'))])
EFFORTS = ('minimal', 'low', 'medium', 'high', 'xhigh')


def model_values():
    saved = read_json(MODELS_FILE, {})
    values = {}
    for key, (default, env, _) in MODELS.items():
        values[key] = os.environ.get(env) or saved.get(key) or default
    return values


def validate_model(key, value):
    if key not in MODELS:
        raise SystemExit(f'Chave inválida: {key}. Use: {", ".join(MODELS)}')
    if not re.fullmatch(r'[A-Za-z0-9._:/@\[\]-]*', value) or value.startswith('-'):  # a dash would be read as an option
        raise SystemExit('Valor com caracteres inválidos.')
    if key == 'codex_effort' and value not in EFFORTS:
        raise SystemExit('Esforço inválido: ' + ', '.join(EFFORTS))
    if key == 'openrouter_paid' and value not in ('on', 'off'):
        raise SystemExit('Use: /model openrouter_paid on|off')
    if key == 'openrouter' and value and value != 'openrouter/free' and not value.endswith(':free') \
            and model_values()['openrouter_paid'] != 'on':
        raise SystemExit('Modelo pago não autorizado. Autorize antes com: /model openrouter_paid on')


def cmd_model(args):
    if not args:
        values = model_values()
        saved = read_json(MODELS_FILE, {})
        for key, (default, env, help_text) in MODELS.items():
            origin = 'env' if os.environ.get(env) else ('salvo' if saved.get(key) else 'padrão')
            print(f'  {key:<13} {values[key] or "(automático)":<24} [{origin}]  {help_text}')
        print('\nAlterar: /model codex gpt-6-astra · voltar ao padrão: /model codex default')
        return
    key = args[0]
    if len(args) == 1:
        validate_model(key, '' if key != 'codex_effort' else 'high')
        print(f"{key} = {model_values()[key] or '(automático)'}  ·  {MODELS[key][2]}")
        return
    value = args[1]
    saved = read_json(MODELS_FILE, {})
    if value == 'default':
        validate_model(key, MODELS[key][0] if key in MODELS else '')
        saved.pop(key, None)
    else:
        validate_model(key, value)
        saved[key] = value
    current = saved.get('openrouter', '')
    if key == 'openrouter_paid' and value == 'off' and current and current != 'openrouter/free' \
            and not current.endswith(':free'):
        saved.pop('openrouter')
        print('  Modelo pago do OpenRouter removido; voltou ao automático gratuito.')
    write_json(MODELS_FILE, saved)
    print(f'✓ {key} = {model_values()[key] or "(automático)"}')
    if key == 'openrouter_paid' and model_values()[key] == 'on':
        print('  Atenção: modelos pagos do OpenRouter serão cobrados na sua conta OpenRouter.')
        print('  Escolha o modelo com /model openrouter ID · desligue com /model openrouter_paid off')
    if os.environ.get(MODELS[key][1]):
        print(f'  Aviso: a variável {MODELS[key][1]} está definida e tem prioridade.')


def cmd_env():
    values = model_values()
    for key, (_, env, _) in MODELS.items():
        print(f'{env}={shlex.quote(values[key])}')
