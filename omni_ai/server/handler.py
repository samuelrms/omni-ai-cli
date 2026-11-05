"""HTTP layer: authentication, limits, disconnect detection, JSON and SSE responses."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import select
import socket
import socketserver
import sys
import threading
import time
import uuid

from .. import config as config_tool
from ..cli.base import installed
from ..tools import TOOLS
from . import auth, protocol
from .execution import DISCONNECTED, Execution, OK, RATE_LIMITED, SWITCHED, TIMEOUT
from .protocol import AUTO_MODEL, RequestError, TOOL_PREFIX

MAX_BODY_BYTES = 1_000_000
SOCKET_TIMEOUT = 30
WATCH_INTERVAL = 1
FAILURES = {
    RATE_LIMITED: (429, 'All accounts and providers are at their limit. Try again later.', 'rate_limit_error', 'rate_limit_exceeded'),
    TIMEOUT: (504, 'The request timed out.', 'timeout_error', 'timeout'),
    SWITCHED: (502, 'The provider failed after the answer had started.', 'api_error', 'provider_failed'),
}
GENERIC_FAILURE = (502, 'No provider could answer this request.', 'api_error', 'provider_failed')


def available(name):
    tool = TOOLS[name]
    if tool.bin and installed(name):
        return True
    # Grok answers over its API key without the CLI; Claude Code and the other CLIs need to be installed
    return (not tool.bin or name == 'grok') and bool(config_tool.stored_keys(name))


def tool_of(model):
    if model == AUTO_MODEL:
        return 'auto'
    name = model[len(TOOL_PREFIX):] if model.startswith(TOOL_PREFIX) else ''
    if name in TOOLS and available(name):
        return name
    raise RequestError(404, f"The model '{model}' does not exist or is not configured.", param='model', code='model_not_found')


def model_ids():
    return [AUTO_MODEL] + [TOOL_PREFIX + name for name in TOOLS if available(name)]


def api_provider(name):
    """Whether this tool answered through an HTTP API, the only path that honours sampling parameters."""
    return name in ('openrouter', 'deepseek') or (name == 'grok' and not installed('grok'))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, address, max_concurrent=4, timeout=600):
        host = address[0]
        self.address_family = socket.AF_INET6 if ':' in host else socket.AF_INET
        self.slots = threading.BoundedSemaphore(max_concurrent)
        self.request_timeout = timeout
        self.running = set()
        self.running_lock = threading.Lock()
        super().__init__(address, Handler)

    def server_bind(self):
        # HTTPServer.server_bind resolves the host name, which can block for over 30 s without working reverse DNS
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def stop_all(self, reason):
        with self.running_lock:
            executions = list(self.running)
        for execution in executions:
            execution.stop(reason)
        return executions


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'OmniAI'
    sys_version = ''
    timeout = SOCKET_TIMEOUT

    def log_message(self, fmt, *args):
        # Request line and status only: never headers or bodies
        print(f'{self.log_date_time_string()} {fmt % args}', file=sys.stderr, flush=True)

    def send_error(self, code, message=None, explain=None):
        """Malformed requests get the same error format as everything else."""
        self.close_connection = True
        self._json(code, protocol.error_body(message or self.responses.get(code, ('Error',))[0]))

    def _json(self, status, body, headers=None):
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        if self.close_connection:
            self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()

    def _fail(self, error):
        # The body may be unread at this point, and it must not be parsed as the next request
        self.close_connection = True
        headers = {'Retry-After': str(error.retry_after)} if error.retry_after else {}
        if error.status == 401:
            headers['WWW-Authenticate'] = 'Bearer'
        self._json(error.status, protocol.error_body(error.message, error.kind, error.param, error.code), headers)

    def _authenticate(self):
        scheme, _, value = (self.headers.get('Authorization') or '').partition(' ')
        try:
            valid = scheme.lower() == 'bearer' and auth.verify(value.strip())
        except SystemExit:  # the keys file is corrupt: fail closed
            raise RequestError(500, 'The server key file could not be read.', 'api_error') from None
        if not valid:
            raise RequestError(401, 'Invalid or missing API key.', 'authentication_error', code='invalid_api_key')

    def _read_body(self):
        try:
            length = int(self.headers.get('Content-Length', ''))
        except ValueError:
            raise RequestError(411, 'Content-Length is required.') from None
        if length < 0 or length > MAX_BODY_BYTES:
            raise RequestError(413, f'The request body exceeds {MAX_BODY_BYTES} bytes.')
        try:
            data = self.rfile.read(length)
        except OSError:
            raise RequestError(408, 'Timed out reading the request body.') from None
        if len(data) != length:
            raise RequestError(400, 'The request body is incomplete.')
        try:
            return json.loads(data.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            raise RequestError(400, 'The request body is not valid JSON.') from None

    def do_GET(self):
        try:
            self._authenticate()
            path = self.path.split('?', 1)[0].rstrip('/')
            ids = model_ids()
            if path == '/v1/models':
                self._json(200, {'object': 'list', 'data': [protocol.model_body(i) for i in ids]})
            elif path.startswith('/v1/models/') and path[len('/v1/models/'):] in ids:
                self._json(200, protocol.model_body(path[len('/v1/models/'):]))
            elif path.startswith('/v1/models/'):
                raise RequestError(404, 'The model does not exist or is not configured.', code='model_not_found')
            else:
                raise RequestError(404, 'Unknown route.')
        except RequestError as error:
            self._fail(error)

    def do_POST(self):
        try:
            self._authenticate()
            if self.path.split('?', 1)[0].rstrip('/') != '/v1/chat/completions':
                raise RequestError(404, 'Unknown route.')
            request = protocol.parse_request(self._read_body())
            tool = tool_of(request['model'])
            if not self.server.slots.acquire(blocking=False):
                raise RequestError(429, 'The server is busy. Try again shortly.', 'rate_limit_error',
                                   code='server_busy', retry_after=1)
            try:
                self._complete(request, tool)
            finally:
                self.server.slots.release()
        except RequestError as error:
            self._fail(error)
        except OSError:
            self.close_connection = True  # the client went away while we were answering

    def _watch(self, execution, finished):
        """Only a read notices a client that left while nothing is being written to it."""
        while not finished.wait(WATCH_INTERVAL):
            try:
                ready, _, _ = select.select([self.connection], [], [], 0)
                if ready:
                    if not self.connection.recv(1, socket.MSG_PEEK):
                        execution.stop(DISCONNECTED)
                        return
                    return  # bytes of a pipelined request: the client is alive, leave them for later
            except (OSError, ValueError):
                execution.stop(DISCONNECTED)
                return

    def _complete(self, request, tool):
        execution = Execution(tool, request['prompt'], request['last_user'], request['temperature'], request['max_tokens'])
        stream = None
        if request['stream']:
            stream = Stream(self, request, execution)
        finished = threading.Event()
        threading.Thread(target=self._watch, args=(execution, finished), daemon=True).start()
        timer = threading.Timer(self.server.request_timeout, execution.stop, args=(TIMEOUT,))
        timer.daemon = True
        timer.start()
        with self.server.running_lock:
            self.server.running.add(execution)
        try:
            outcome = execution.run(stream.delta if stream else None)
        finally:
            finished.set()
            timer.cancel()
            with self.server.running_lock:
                self.server.running.discard(execution)
        if outcome == DISCONNECTED:
            self.close_connection = True
            return
        answer, finish_reason = execution.answer, 'stop'
        estimated_cut = request['max_tokens'] and not api_provider(execution.provider)
        if outcome == OK and estimated_cut and len(answer) > request['max_tokens'] * 4:
            answer, finish_reason = answer[:request['max_tokens'] * 4], 'length'
        usage, source = protocol.usage_body(execution.tokens, request['prompt'], answer)
        if outcome == OK and request['max_tokens'] and usage['completion_tokens'] >= request['max_tokens']:
            finish_reason = 'length'
        if stream:
            stream.finish(outcome, answer, finish_reason, usage, source)
        elif outcome != OK:
            status, message, kind, code = FAILURES.get(outcome, GENERIC_FAILURE)
            raise RequestError(status, message, kind, code=code, retry_after=60 if status == 429 else None)
        else:
            request_id = 'chatcmpl-' + uuid.uuid4().hex
            self._json(200, protocol.completion_body(request_id, int(time.time()), request['model'], answer, finish_reason, usage),
                       result_headers(request, execution, source))


def result_headers(request, execution, source=None):
    ignored = list(request['ignored'])
    if request['temperature'] is not None and not api_provider(execution.provider):
        ignored.insert(0, 'temperature')
    headers = {'X-Omni-Provider': str(execution.provider or execution.tool)}
    if source:
        headers['X-Omni-Usage'] = source
    if ignored:
        headers['X-Omni-Ignored-Params'] = ', '.join(ignored)
    return headers


class Stream:
    """SSE output. Headers go out with the first text, so a failure before that is still a normal HTTP error
    and the router can change provider without the client noticing."""

    def __init__(self, handler, request, execution):
        self.handler, self.request, self.execution = handler, request, execution
        self.id = 'chatcmpl-' + uuid.uuid4().hex
        self.created = int(time.time())
        self.started = False

    def _send(self, body):
        self.handler.wfile.write(f'data: {json.dumps(body, ensure_ascii=False)}\n\n'.encode('utf-8'))
        self.handler.wfile.flush()

    def _chunk(self, delta, finish_reason=None):
        self._send(protocol.chunk_body(self.id, self.created, self.request['model'], delta, finish_reason))

    def _start(self):
        if self.started:
            return
        self.started = True
        handler = self.handler
        handler.close_connection = True
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/event-stream')
        handler.send_header('Cache-Control', 'no-cache')
        handler.send_header('Connection', 'close')
        for name, value in result_headers(self.request, self.execution).items():
            handler.send_header(name, value)
        handler.end_headers()
        self._chunk({'role': 'assistant', 'content': ''})

    def delta(self, text):
        self._start()
        self._chunk({'content': text})

    def finish(self, outcome, answer, finish_reason, usage, source):
        if outcome != OK:
            status, message, kind, code = FAILURES.get(outcome, GENERIC_FAILURE)
            if not self.started:
                raise RequestError(status, message, kind, code=code, retry_after=60 if status == 429 else None)
            self._send(protocol.error_body(message, kind, code=code))  # too late to change the status
            return
        already_sent = self.started
        self._start()
        if not already_sent:
            self._chunk({'content': answer})
        self._chunk({}, finish_reason)
        if self.request['include_usage']:
            self._send(dict(protocol.chunk_body(self.id, self.created, self.request['model'], {}), choices=[], usage=usage))
        self.handler.wfile.write(b'data: [DONE]\n\n')
        self.handler.wfile.flush()
