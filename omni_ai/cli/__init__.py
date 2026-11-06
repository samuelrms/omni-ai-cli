#!/usr/bin/env python3
"""Omni AI router: the `ai` command on macOS, Linux and Windows.

  ai                                  open the chat
  ai "question"                       automatic routing
  ai --claude|--codex|--cursor|... "question"   (one flag per tool)
  ai --mode plan|edits|auto|yolo "question"
  ai --usage                         Uso e limites de todas as ferramentas e contas
  ai --setup                         First-run guide: logins and API keys
  ai --status | --doctor | --reset | --log | --classify "prompt"
  ai --model ... | --account ... | --key ... | --sync ...

Split into submodules: base (primitives), limits, executors, classify, routing,
doctor, usage_view, commands. Public names are re-exported here so omni_ai.cli.<name>
keeps working.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from . import base, limits, executors, classifier, routing, health, usage_view, commands

from .. import config as config_tool
from .. import usage
from ..osutils import REPO, enable_ansi, module_cmd, module_env, resolve_bin
from ..tools import CLIS, KEY_NAMES, TOOLS

from .base import (
    STATE_FILE, LOG_FILE, BINS, FLAGS, TOOL_FLAGS, KEY_LIST, MODES, RATE_LIMITED, NOT_READY,
    LIMIT_TEXT, COLOR, RED, GREEN, YELLOW, BLUE, CYAN, MAGENTA, BOLD, DIM, NC,
    say, config, profile_conf, state, set_state, log_usage, cooldown_left, mark_rate_limited,
    mode, models, emit, emit_provider, run, installed, header,
)
from .limits import (
    LIMIT_MESSAGE, LIMIT_IN, LIMIT_AT, LIMIT_FALLBACK,
    limit_until, remember_limit, active_limit, hit_limit, tail_hit_limit,
)
from .executors import (
    run_claude, key_account, run_with_key_retry, run_codex, run_agy, run_openrouter,
    run_cursor, run_deepseek, run_grok, over_ceiling,
)
from .classifier import (
    GROQ_CLASSIFIER_MODELS, GROK_CLASSIFIER_MODEL, CEREBRAS_CLASSIFIER_MODELS, CLASSIFIER_MODELS,
    CLASSIFIER_SYSTEM, ask_text, ask_classifier, free_classifiers, classify_llm, classify,
    classify_rules, CLASSIFIED_BY,
)
from .routing import claude_with_fallback, runners, route
from .health import version, show_status, ai_version, doctor
from .usage_view import ago, show_usage, spend_today, render_usage
from .commands import SOURCE_LABEL, history_command, completion_words, completions, reset_cooldowns, show_log, update


USAGE = f'''{BOLD}Uso:{NC}
  ai                                 Abrir o chat (/help dentro dele)
  ai "pergunta"                      Roteamento automático
  ai {TOOL_FLAGS} "pergunta"
  ai --mode plan|edits|auto|yolo "pergunta"
  ai --prompt-file ARQUIVO           Lê o pedido de um arquivo
  ai --model [chave valor]           Modelos
  ai --account [list|add|login|sync|remove] NOME
  ai --key [{KEY_LIST}|remove X]
  ai --sync [--dry-run]              MCPs e skills entre Claude, Codex e agy
  ai --usage                         Uso e limites de todas as ferramentas e contas
  ai --setup                          Guia de primeiro acesso: logins e chaves
  ai --install                       Instala as ferramentas que faltam (pergunta antes)
  ai --status | --doctor | --reset | --log | --classify "prompt"
  ai serve [--host H] [--port P] [--mode M --workdir PASTA]   Servidor compatível com a API da OpenAI
  ai serve keys [list|add|remove]    Chaves de API do servidor
  ai serve admin-keys [list|add|remove]   Chaves da API de administração
  ai serve work [list|remove ID|prune]   Branches e cópias dos pedidos de escrita
  ai serve --install-service | --uninstall-service | --service-status
  ai --update                        Atualizar para a versão mais nova
  ai --version                       Versão do Omni AI CLI
  ai --completions zsh|bash          Script de autocomplete do shell
  ai history [list|<id>]             Conversas de todas as ferramentas (--source, --json)
'''


def main(argv):
    enable_ansi()
    if not argv and sys.stdin.isatty():
        from .. import chat
        return chat.main([])
    head = argv[0] if argv else ''
    delegated = {'--model': ('config_tool', ['model']), '-m': ('config_tool', ['model']),
                 '--account': ('config_tool', ['account']), '--key': ('config_tool', ['key']),
                 '--keys': ('config_tool', ['key']), '--setup-openrouter': ('config_tool', ['key', 'set', 'openrouter']),
                 '--setup-secondary': ('config_tool', ['account', 'add', 'secondary'])}
    if head in ('--chat', '--interactive', '-i', '--resume', '--sessions'):
        from .. import chat
        return chat.main(argv[1:] if head in ('--chat', '--interactive', '-i') else argv)
    if head in delegated:
        _, base_args = delegated[head]
        try:
            config_tool.main(base_args + argv[1:])
        except SystemExit as error:
            if error.code not in (None, 0):
                print(error.code, file=sys.stderr)
                return 1
        return 0
    if head in ('--update', '--upgrade'):
        return update()
    # `--history` is always the command; bare `history` only when it really looks like one
    # (alone, a known subcommand/flag, or a single id), so `ai history of Brazil` stays a prompt.
    if head == '--history' or (head == 'history' and (
            len(argv) == 1 or argv[1] in ('list', '--json', '--source') or len(argv) == 2)):
        return history_command(argv[1:])
    # Same care as `history`: `ai serve dinner at eight` stays a prompt
    if head == '--serve' or (head == 'serve' and (
            len(argv) == 1 or argv[1].startswith('--') or (argv[1] in ('keys', 'admin-keys') and (
                len(argv) == 2 or argv[2] in ('list', 'add', 'remove'))) or (argv[1] == 'work' and (
                len(argv) == 2 or argv[2] in ('list', 'remove', 'prune') or argv[2].startswith('--'))))):
        from .. import server
        return server.main(argv[1:])
    if head == '--sync':
        from .. import sync
        return sync.main(argv[1:])
    if head in ('--print', '--export'):
        from .. import export
        return export.main(argv)
    if head == '--menu-json':
        from .. import skills as _skills
        data = {
            'tools': ['auto'] + list(TOOLS.keys()),
            'modes': list(MODES),
            'langs': ['pt', 'en', 'es', 'fr'],
            'skills': sorted(_skills.discover(os.getcwd()).keys()),
        }
        say(json.dumps(data, ensure_ascii=False))
        return 0
    tool, words, i = 'auto', [], 0
    while i < len(argv):
        arg = argv[i]
        if arg == '--':
            words += argv[i + 1:]
            break
        if arg.lstrip('-') in FLAGS and arg.startswith('-'):
            tool = FLAGS[arg.lstrip('-')]
        elif arg == '--mode':
            value = argv[i + 1] if i + 1 < len(argv) else ''
            if value not in MODES:
                print('Modo inválido: use default, plan, edits, auto ou yolo', file=sys.stderr)
                return 1
            os.environ['SMART_AI_MODE'] = value
            i += 1
        elif arg == '--prompt-file':
            try:
                words.append(Path(argv[i + 1]).read_text(encoding='utf-8'))
            except (IndexError, OSError, UnicodeDecodeError) as error:
                print(f'Não foi possível ler o arquivo do pedido: {error}', file=sys.stderr)
                return 1
            i += 1
        elif arg in ('--status', '-s'):
            show_status()
            return 0
        elif arg == '--doctor' or (arg == 'doctor' and len(argv) == 1):
            return doctor()
        elif arg in ('--version', '-v'):
            say(ai_version())
            return 0
        elif arg == '--completions':
            return completions(argv[i + 1] if i + 1 < len(argv) else '')
        elif arg in ('--setup', '--install'):
            from .. import onboarding
            onboarding.main(install_missing=arg == '--install' or 'install' in argv[i + 1:])
            return 0
        elif arg in ('--usage', '--limits'):
            return show_usage(watch='watch' in argv[i + 1:] or '--watch' in argv[i + 1:])
        elif arg in ('--reset', '-r'):
            reset_cooldowns()
            return 0
        elif arg in ('--log', '-l'):
            show_log()
            return 0
        elif arg == '--classify':
            prompt = ' '.join(argv[i + 1:])
            result = classify(prompt)
            say(f'Prompt: {DIM}"{prompt}"{NC}')
            say('Classificação: ' + (f'{GREEN}SIMPLES{NC} → alternativas' if result == 'simple' else f'{CYAN}COMPLEXA{NC} → Claude'))
            return 0
        elif arg in ('--help', '-h'):
            say(USAGE)
            return 0
        else:
            words.append(arg)
        i += 1
    prompt = ' '.join(words).strip()
    if not prompt:
        say(USAGE)
        return 1
    return route(prompt, tool)


def entry():
    try:
        return main(sys.argv[1:])
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # Output piped into something that stopped reading (e.g. `ai --usage | head`)
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
