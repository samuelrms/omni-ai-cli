"""OpenAI-compatible HTTP server: `ai serve` exposes the router to anything that speaks the OpenAI API.

Split into submodules: auth (API keys), protocol (wire format), execution (one router process per request),
handler (HTTP).
"""
import argparse
import ipaddress
import logging
import logging.handlers
import os
import signal
import sys
import time

from . import auth, service
from .execution import FORCE_AFTER, SHUTDOWN, uses_shared_credentials
from .handler import REQUEST_LOG, Server


def _loopback(host):
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _positive(text):
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError('deve ser maior que zero')
    return value


def _log_to_file(path):
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3, encoding='utf-8')
    REQUEST_LOG.addHandler(handler)
    REQUEST_LOG.propagate = False


def keys_command(args, path=None, prefix='omni-', usage='keys'):
    action = args[0] if args else 'list'
    if action == 'list' and len(args) <= 1:
        names = auth.stored(path)
        for name, item in sorted(names.items()):
            print(f"{name}  criada em {time.strftime('%Y-%m-%d', time.localtime(item.get('created', 0)))}")
        if not names:
            print('Nenhuma chave salva.')
        return 0
    if action == 'add' and len(args) == 2:
        try:
            value = auth.add(args[1], path, prefix)
        except ValueError as error:
            print(error, file=sys.stderr)
            return 1
        print(f'Chave {args[1]} (guarde agora, ela não será mostrada de novo):\n{value}')
        return 0
    if action == 'remove' and len(args) == 2:
        if auth.remove(args[1], path):
            print(f'Chave {args[1]} removida.')
            return 0
        print(f'Chave {args[1]} não encontrada.', file=sys.stderr)
        return 1
    print(f'Uso: ai serve {usage} [list | add NOME | remove NOME]', file=sys.stderr)
    return 1


def main(argv):
    if argv and argv[0] == 'keys':
        return keys_command(argv[1:])
    if argv and argv[0] == 'admin-keys':
        return keys_command(argv[1:], auth.admin_keys_path(), 'omni-admin-', 'admin-keys')
    parser = argparse.ArgumentParser(prog='ai serve', description='Servidor HTTP compatível com a API da OpenAI')
    parser.add_argument('--host', default='127.0.0.1', help='Endereço (padrão: 127.0.0.1, só esta máquina)')
    parser.add_argument('--port', type=int, default=8080, help='Porta (padrão: 8080)')
    parser.add_argument('--max-concurrent', type=_positive, default=4, help='Pedidos simultâneos (padrão: 4)')
    parser.add_argument('--timeout', type=_positive, default=600, help='Limite de cada pedido, em segundos (padrão: 600)')
    parser.add_argument('--mode', choices=('plan', 'edits', 'auto', 'yolo'), default='plan',
                        help='O que as ferramentas podem fazer (padrão: plan, somente leitura)')
    parser.add_argument('--workdir', help='Pasta de trabalho das ferramentas (obrigatória fora do modo plan)')
    parser.add_argument('--enable-admin', action='store_true', default=os.environ.get('SMART_AI_ADMIN') == '1',
                        help='Liga a API de administração em /admin (exige ai serve admin-keys add NOME)')
    parser.add_argument('--admin-allow-http', action='store_true', default=os.environ.get('SMART_AI_ADMIN_ALLOW_HTTP') == '1',
                        help='Aceita /admin sem HTTPS vindo de fora desta máquina (só em rede confiável)')
    parser.add_argument('--log-file', help='Grava os pedidos neste arquivo, com rotação por tamanho')
    parser.add_argument('--install-service', action='store_true', help='Instala como serviço do usuário (launchd ou systemd)')
    parser.add_argument('--uninstall-service', action='store_true', help='Remove o serviço')
    parser.add_argument('--service-status', action='store_true', help='Mostra o estado do serviço')
    parser.add_argument('--yes', action='store_true', help='Confirma --mode yolo e arquivo de serviço não gerado pelo Omni')
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error('porta inválida')
    workdir = os.path.abspath(args.workdir) if args.workdir else None
    if args.service_status or args.uninstall_service or args.install_service:
        try:
            if args.service_status:
                return service.status()
            if args.uninstall_service:
                return service.uninstall(args)
            return service.install(args, _loopback(args.host))
        except service.ServiceError as error:
            print(error, file=sys.stderr)
            return 1
    if args.mode != 'plan' and not workdir:
        parser.error(f'--mode {args.mode} exige --workdir')
    if workdir and not os.path.isdir(workdir):
        parser.error(f'pasta de trabalho não encontrada: {workdir}')

    if args.enable_admin and not auth.admin_configured():
        print('--enable-admin exige uma chave própria: ai serve admin-keys add NOME', file=sys.stderr)
        return service.FATAL_EXIT
    if args.enable_admin and auth.admin_key_shared():
        print('A chave de administração não pode ser igual a uma chave de /v1.', file=sys.stderr)
        return service.FATAL_EXIT
    if args.log_file:
        _log_to_file(args.log_file)
    created = None
    if not auth.configured():
        created = auth.add('default')
    concurrent = args.max_concurrent
    if concurrent > 1 and args.mode != 'plan':
        concurrent = 1  # requests would overwrite each other's files in the shared folder
    if concurrent > 1 and uses_shared_credentials():
        concurrent = 1
        print('Aviso: perfil Claude legado troca credenciais durante a chamada; atendendo um pedido por vez.', file=sys.stderr)
    try:
        server = Server((args.host, args.port), concurrent, args.timeout, args.mode, workdir)
    except OSError as error:
        print(f'Não foi possível abrir {args.host}:{args.port}: {error}', file=sys.stderr)
        return 1

    server.admin_enabled, server.admin_allow_http = args.enable_admin, args.admin_allow_http

    def terminate(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)

    host = f'[{args.host}]' if ':' in args.host else args.host
    print(f'Omni AI server em http://{host}:{server.server_address[1]}/v1', flush=True)
    if created:
        print(f'Chave de API criada (guarde agora, ela não será mostrada de novo):\n{created}', flush=True)
    if args.mode != 'plan':
        print(f'Aviso: modo {args.mode}. Os pedidos podem alterar arquivos em {workdir}, um por vez, sem aprovação. '
              'Quem tem a chave também pode alcançar os logins das contas.', file=sys.stderr, flush=True)
    if not _loopback(args.host):
        print('Aviso: acessível pela rede. Use HTTPS com proxy reverso. Quem tem a chave usa suas contas e pode ler '
              'arquivos desta máquina por meio das ferramentas.', file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.logins.stop_all()
        running = server.stop_all(SHUTDOWN)
        deadline = time.time() + FORCE_AFTER + 1
        while time.time() < deadline and any(e.child is not None and e.child.poll() is None for e in running):
            time.sleep(0.1)
        server.server_close()
    return 0
