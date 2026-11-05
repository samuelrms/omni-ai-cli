"""OpenAI-compatible HTTP server: `ai serve` exposes the router to anything that speaks the OpenAI API.

Split into submodules: auth (API keys), protocol (wire format), execution (one router process per request),
handler (HTTP).
"""
import argparse
import ipaddress
import signal
import sys
import time

from . import auth
from .execution import FORCE_AFTER, SHUTDOWN, uses_shared_credentials
from .handler import Server


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


def keys_command(args):
    action = args[0] if args else 'list'
    if action == 'list' and len(args) <= 1:
        names = auth.stored()
        for name, item in sorted(names.items()):
            print(f"{name}  criada em {time.strftime('%Y-%m-%d', time.localtime(item.get('created', 0)))}")
        if not names:
            print('Nenhuma chave salva.')
        return 0
    if action == 'add' and len(args) == 2:
        try:
            value = auth.add(args[1])
        except ValueError as error:
            print(error, file=sys.stderr)
            return 1
        print(f'Chave {args[1]} (guarde agora, ela não será mostrada de novo):\n{value}')
        return 0
    if action == 'remove' and len(args) == 2:
        if auth.remove(args[1]):
            print(f'Chave {args[1]} removida.')
            return 0
        print(f'Chave {args[1]} não encontrada.', file=sys.stderr)
        return 1
    print('Uso: ai serve keys [list | add NOME | remove NOME]', file=sys.stderr)
    return 1


def main(argv):
    if argv and argv[0] == 'keys':
        return keys_command(argv[1:])
    parser = argparse.ArgumentParser(prog='ai serve', description='Servidor HTTP compatível com a API da OpenAI')
    parser.add_argument('--host', default='127.0.0.1', help='Endereço (padrão: 127.0.0.1, só esta máquina)')
    parser.add_argument('--port', type=int, default=8080, help='Porta (padrão: 8080)')
    parser.add_argument('--max-concurrent', type=_positive, default=4, help='Pedidos simultâneos (padrão: 4)')
    parser.add_argument('--timeout', type=_positive, default=600, help='Limite de cada pedido, em segundos (padrão: 600)')
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error('porta inválida')

    created = None
    if not auth.configured():
        created = auth.add('default')
    concurrent = args.max_concurrent
    if concurrent > 1 and uses_shared_credentials():
        concurrent = 1
        print('Aviso: perfil Claude legado troca credenciais durante a chamada; atendendo um pedido por vez.', file=sys.stderr)
    try:
        server = Server((args.host, args.port), concurrent, args.timeout)
    except OSError as error:
        print(f'Não foi possível abrir {args.host}:{args.port}: {error}', file=sys.stderr)
        return 1

    def terminate(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)

    host = f'[{args.host}]' if ':' in args.host else args.host
    print(f'Omni AI server em http://{host}:{server.server_address[1]}/v1', flush=True)
    if created:
        print(f'Chave de API criada (guarde agora, ela não será mostrada de novo):\n{created}', flush=True)
    if not _loopback(args.host):
        print('Aviso: acessível pela rede. Use HTTPS com proxy reverso. Quem tem a chave usa suas contas e pode ler '
              'arquivos desta máquina por meio das ferramentas.', file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        running = server.stop_all(SHUTDOWN)
        deadline = time.time() + FORCE_AFTER + 1
        while time.time() < deadline and any(e.child is not None and e.child.poll() is None for e in running):
            time.sleep(0.1)
        server.server_close()
    return 0
