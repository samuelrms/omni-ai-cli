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
import threading
import time

from ..osutils import in_container, private_file
from . import auth, service
from .execution import FORCE_AFTER, SHUTDOWN, uses_shared_credentials
from .handler import REQUEST_LOG, Server
from .workspaces import Workspaces, list_work, remove_work


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


class PrivateRotatingLog(logging.handlers.RotatingFileHandler):
    def _open(self):
        return private_file(self.baseFilename, self.mode, encoding=self.encoding, tighten=True)


def _log_to_file(path):
    handler = PrivateRotatingLog(path, maxBytes=5_000_000, backupCount=3, encoding='utf-8')
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


def _show_work(item):
    size = f" · {item['size'] / 1024 ** 2:.1f} MB" if item['size'] is not None else ''
    notes = (' · no merge' if item['merged'] else '') + (' · em uso' if item['in_use'] else '')
    print(f"{item['id']}  {item['kind']} · {item['age_days']:.1f} dias{size}{notes}")


def work_command(argv):
    """Branches and copies left by write requests. Nothing is deleted without --yes, and unmerged work is always named first."""
    parser = argparse.ArgumentParser(prog='ai serve work', description='Branches e cópias criadas pelos pedidos de escrita')
    parser.add_argument('action', nargs='?', choices=('list', 'remove', 'prune'), default='list')
    parser.add_argument('id', nargs='?', help='remove: o id mostrado em list')
    parser.add_argument('--workdir', default='.', help='A mesma pasta usada no servidor (padrão: a atual)')
    parser.add_argument('--older-than', type=_positive, help='prune: com mais de N dias')
    parser.add_argument('--merged', action='store_true', help='prune: só branches que já estão no merge')
    parser.add_argument('--yes', action='store_true', help='Apaga de verdade. Sem isso, só mostra o que seria apagado')
    args = parser.parse_args(argv)
    if not os.path.isdir(args.workdir):
        parser.error(f'pasta não encontrada: {args.workdir}')
    items = list_work(args.workdir)
    if args.action == 'list':
        for item in items:
            _show_work(item)
        if not items:
            print('Nada guardado para esta pasta.')
        return 0
    if args.action == 'remove':
        if not args.id:
            parser.error('informe o id')
        chosen = [item for item in items if item['id'] == args.id]
        if not chosen:
            print(f'{args.id} não encontrado.', file=sys.stderr)
            return 1
    else:
        if args.older_than is None and not args.merged:
            parser.error('prune precisa de --older-than ou --merged')
        chosen = [item for item in items if (args.older_than is None or item['age_days'] > args.older_than)
                  and (not args.merged or item['merged'])]
    busy = [item for item in chosen if item['in_use']]
    chosen = [item for item in chosen if not item['in_use']]
    for item in busy:
        print(f"{item['id']} parece em uso; não será apagado.", file=sys.stderr)
    if not args.yes:
        for item in chosen:
            _show_work(item)
        print(f'{len(chosen)} item(ns) seriam apagados. Para apagar de verdade, repita com --yes.')
        return 1 if busy and args.action == 'remove' else 0
    failed = [item['id'] for item in chosen if not remove_work(args.workdir, item)]
    print(f'{len(chosen) - len(failed)} item(ns) apagados.')
    if failed:
        print('Não consegui apagar: ' + ', '.join(failed), file=sys.stderr)
    return 1 if failed or (busy and args.action == 'remove') else 0


def main(argv):
    if argv and argv[0] == 'keys':
        return keys_command(argv[1:])
    if argv and argv[0] == 'work':
        return work_command(argv[1:])
    if argv and argv[0] == 'admin-keys':
        return keys_command(argv[1:], auth.admin_keys_path(), 'omni-admin-', 'admin-keys')
    parser = argparse.ArgumentParser(prog='ai serve', description='Servidor HTTP compatível com a API da OpenAI')
    parser.add_argument('--host', default='127.0.0.1', help='Endereço (padrão: 127.0.0.1, só esta máquina)')
    parser.add_argument('--port', type=int, default=8080, help='Porta (padrão: 8080)')
    parser.add_argument('--max-concurrent', type=_positive, default=4, help='Pedidos simultâneos (padrão: 4)')
    parser.add_argument('--queue-size', type=_positive, default=16, help='Modos de escrita: pedidos em espera (padrão: 16)')
    parser.add_argument('--queue-wait', type=_positive, default=60, help='Modos de escrita: espera máxima, em segundos (padrão: 60)')
    parser.add_argument('--timeout', type=_positive, default=600, help='Limite de cada pedido, em segundos (padrão: 600)')
    parser.add_argument('--mode', choices=('plan', 'edits', 'auto', 'yolo'), default='plan',
                        help='O que as ferramentas podem fazer (padrão: plan, somente leitura)')
    parser.add_argument('--workdir', help='Pasta de trabalho das ferramentas (obrigatória fora do modo plan)')
    parser.add_argument('--enable-admin', action='store_true', default=os.environ.get('SMART_AI_ADMIN') == '1',
                        help='Liga a API de administração em /admin (exige ai serve admin-keys add NOME)')
    parser.add_argument('--admin-allow-http', action='store_true', default=os.environ.get('SMART_AI_ADMIN_ALLOW_HTTP') == '1',
                        help='Aceita /admin sem HTTPS vindo de fora desta máquina (só em rede confiável)')
    parser.add_argument('--tool-emulation', action='store_true', default=os.environ.get('SMART_AI_TOOL_EMULATION') == '1',
                        help='Experimental: function calling emulado em Claude Code e Grok, quando não há provedor de API')
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
        if os.environ.get('SMART_AI_SERVICE') == '1':  # the key would be printed to a log file
            print('Nenhuma chave configurada. Crie uma com `ai serve keys add NOME` e reinicie o serviço.', file=sys.stderr)
            return service.FATAL_EXIT
        created = auth.add('default')
    concurrent = args.max_concurrent
    if concurrent > 1 and uses_shared_credentials():
        concurrent = 1
        print('Aviso: perfil Claude legado troca credenciais durante a chamada; atendendo um pedido por vez.', file=sys.stderr)
    try:
        server = Server((args.host, args.port), concurrent, args.timeout, args.mode, workdir)
    except OSError as error:
        print(f'Não foi possível abrir {args.host}:{args.port}: {error}', file=sys.stderr)
        return 1

    server.admin_enabled, server.admin_allow_http = args.enable_admin, args.admin_allow_http
    server.tool_emulation = args.tool_emulation
    if args.mode != 'plan':
        server.workspaces = Workspaces(workdir)
        server.queue_size, server.queue_wait = args.queue_size, args.queue_wait

    def terminate(*_):
        # Not an exception: it would surface anywhere in the main thread, and the server treats errors there
        # as problems with one connection and goes on serving. shutdown() waits for the loop, so not from here.
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, terminate)

    host = f'[{args.host}]' if ':' in args.host else args.host
    print(f'Omni AI server em http://{host}:{server.server_address[1]}/v1', flush=True)
    if created:
        print(f'Chave de API criada (guarde agora, ela não será mostrada de novo):\n{created}', flush=True)
    if args.mode != 'plan':
        place = 'uma branch por pedido' if server.workspaces.uses_git else 'uma cópia da pasta por pedido, sem git'
        print(f'Aviso: modo {args.mode}, {place}, sem aprovação. Base: {workdir}. '
              'Quem tem a chave também pode alcançar os logins das contas.', file=sys.stderr, flush=True)
    if not _loopback(args.host):
        exposure = ('Dentro de um container, quem decide o acesso é a porta publicada.' if in_container()
                    else 'Acessível pela rede. Use HTTPS com proxy reverso.')
        print(f'Aviso: {exposure} Quem tem a chave usa suas contas e pode ler arquivos deste ambiente por meio das ferramentas.',
              file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # Ctrl-C
        pass
    finally:
        server.logins.stop_all()
        running = server.stop_all(SHUTDOWN)
        deadline = time.time() + FORCE_AFTER + 1
        while time.time() < deadline and any(e.child is not None and e.child.poll() is None for e in running):
            time.sleep(0.1)
        server.server_close()
    return 0
