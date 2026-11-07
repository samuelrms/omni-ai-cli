"""HTTP layer: authentication, limits, disconnect detection, JSON and SSE responses."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import re
import selectors
import socket
import socketserver
import sys
import threading
import time
import traceback
import uuid

from .. import config as config_tool
from ..cli.base import installed
from ..tools import TOOLS
from . import admin, auth, protocol
from .emulation import Emulated, target as emulation_target
from .logins import Logins
from .native import Native, capable
from .execution import DENIED, DISCONNECTED, Execution, OK, RATE_LIMITED, SWITCHED, TIMEOUT
from .protocol import AUTO_MODEL, RequestError, TOOL_PREFIX

MAX_BODY_BYTES = 1_000_000
SOCKET_TIMEOUT = 30
HEADER_DEADLINE = 30  # the socket timeout is per read, so a slow client could otherwise stay forever
MIN_CONNECTIONS = 128
BUSY_RESPONSE = (b'HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\nContent-Length: 2\r\n'
                 b'Retry-After: 5\r\nConnection: close\r\n\r\n{}')
WATCH_INTERVAL = 1
UNSAFE_HEADER_VALUE = re.compile(r'[^\t\x20-\x7e\xa0-\xff]')  # a line break would start a new header
FAILURES = {
    RATE_LIMITED: (429, 'All accounts and providers are at their limit. Try again later.', 'rate_limit_error', 'rate_limit_exceeded'),
    TIMEOUT: (504, 'The request timed out.', 'timeout_error', 'timeout'),
    DENIED: (403, "The tool tried an action that this server's mode does not allow.",
             'permission_error', 'action_not_allowed'),
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
        return TOOLS[name].name  # from our table: this reaches a response header, and `model` is the client's text
    raise RequestError(404, f"The model '{model}' does not exist or is not configured.", param='model', code='model_not_found')


def model_ids():
    return [AUTO_MODEL] + [TOOL_PREFIX + name for name in TOOLS if available(name)]


def api_provider(name):
    """Whether this tool answered through an HTTP API, the only path that honours sampling parameters."""
    return name in ('openrouter', 'deepseek') or (name == 'grok' and not installed('grok'))


REQUEST_LOG = logging.getLogger('omni_ai.server.requests')
REQUEST_LOG.setLevel(logging.INFO)
if not REQUEST_LOG.handlers:
    REQUEST_LOG.addHandler(logging.StreamHandler())


def connection_limit(max_concurrent):
    """Enough for every running and queued request, while leaving descriptors for their processes."""
    wanted = max(MIN_CONNECTIONS, 2 * max_concurrent + 64)
    try:
        import resource
        soft = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    except (ImportError, OSError, ValueError):
        return wanted
    return wanted if soft == resource.RLIM_INFINITY else max(8, min(wanted, (soft - 32) // 2))


def has_input(connection):
    """select.select fails for descriptors above 1024, which a busy server reaches."""
    with selectors.DefaultSelector() as selector:
        selector.register(connection, selectors.EVENT_READ)
        return bool(selector.select(0))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    admin_enabled = admin_allow_http = False
    workspaces = None
    tool_emulation = False
    queue_size = queue_wait = 0

    def __init__(self, address, max_concurrent=4, timeout=600, mode='plan', workdir=None):
        host = address[0]
        self.address_family = socket.AF_INET6 if ':' in host else socket.AF_INET
        self.slots = threading.BoundedSemaphore(max_concurrent)
        self.request_timeout = timeout
        self.mode, self.workdir = mode, workdir
        self.running = set()
        self.started = time.monotonic()
        self.logins = Logins()
        self.waiting = 0
        self.waiting_lock = threading.Lock()
        self.running_lock = threading.Lock()
        self.connections = threading.BoundedSemaphore(connection_limit(max_concurrent))
        self.reading = {}  # connection -> moment its request line and headers must have arrived by
        self.reading_lock = threading.Lock()
        self.closed = threading.Event()
        super().__init__(address, Handler)
        threading.Thread(target=self._close_slow_readers, daemon=True).start()

    def process_request(self, request, client_address):
        if not self.connections.acquire(blocking=False):
            try:
                request.setblocking(False)
                request.send(BUSY_RESPONSE)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connections.release()

    def expect_headers(self, connection):
        with self.reading_lock:
            self.reading[connection] = time.monotonic() + HEADER_DEADLINE

    def headers_read(self, connection):
        with self.reading_lock:
            self.reading.pop(connection, None)

    def _close_slow_readers(self):
        while not self.closed.wait(WATCH_INTERVAL):
            now = time.monotonic()
            with self.reading_lock:
                late = [connection for connection, deadline in self.reading.items() if deadline <= now]
            for connection in late:
                self.headers_read(connection)
                try:
                    connection.shutdown(socket.SHUT_RDWR)  # the handler's read returns, and it ends
                except OSError:
                    pass

    def handle_error(self, request, client_address):
        # A client that left is routine, and nobody without a key should be able to fill the log with tracebacks
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)

    def server_close(self):
        self.closed.set()
        super().server_close()

    def take_slot(self):
        """Waits in line when a queue is set. False when the line is full or the wait ran out."""
        if self.slots.acquire(blocking=False):
            return True
        with self.waiting_lock:
            if self.waiting >= self.queue_size:
                return False
            self.waiting += 1
        try:
            return self.slots.acquire(timeout=self.queue_wait)
        finally:
            with self.waiting_lock:
                self.waiting -= 1

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

    def handle_one_request(self):
        self.response_started = False
        self.server.expect_headers(self.connection)
        try:
            super().handle_one_request()
        except OSError:
            raise
        except Exception as error:  # a bug here must not end as a dropped connection with no answer
            self._unexpected(error)
        finally:
            self.server.headers_read(self.connection)

    def parse_request(self):
        self.work_header = None  # a connection can carry several requests
        parsed = super().parse_request()
        self.server.headers_read(self.connection)
        if parsed:
            # A body left unread would be parsed as the next request, so only reading it keeps the connection
            self.close_after_body = self.close_connection
            if self.headers.get('Content-Length', '0') != '0' or self.headers.get('Transfer-Encoding'):
                self.close_connection = True
        return parsed

    def log_message(self, fmt, *args):
        # Request line without its query, and status: never headers or bodies
        line = re.sub(r'[?]\S*', '', fmt % args)
        line = re.sub(r'[\x00-\x1f\x7f-\x9f]', lambda found: f'\\x{ord(found.group()):02x}', line)  # sent before any key
        REQUEST_LOG.info(f'{self.log_date_time_string()} {line}')

    def _unexpected(self, error):
        # The type and the place: the message of an exception can quote the request
        where = traceback.extract_tb(error.__traceback__)[-1]
        REQUEST_LOG.error('Unexpected %s at %s:%s', type(error).__name__, Path(where.filename).name, where.lineno)
        self.close_connection = True
        if not self.response_started:  # too late to answer otherwise: closing tells the client it is incomplete
            self._fail(RequestError(500, 'The server could not process this request.', 'api_error'))

    def send_response(self, code, message=None):
        self.response_started = True
        super().send_response(code, message)

    def send_header(self, keyword, value):
        if UNSAFE_HEADER_VALUE.search(str(value)):
            REQUEST_LOG.warning('Response header %s left out: its value has characters HTTP does not allow', keyword)
            return
        super().send_header(keyword, value)

    def send_error(self, code, message=None, explain=None):
        """Malformed requests get the same error format as everything else."""
        self.close_connection = True
        self._json(code, protocol.error_body(message or self.responses.get(code, ('Error',))[0]))

    def _json(self, status, body, headers=None):
        data = json.dumps(body, ensure_ascii=False).encode('utf-8', 'replace')
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
        if getattr(self, 'work_header', None):
            headers[self.work_header[0]] = self.work_header[1]
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
        # A proxy may disagree with us on where the body ends if it has two possible lengths
        if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) > 1:
            raise RequestError(400, 'Use a single Content-Length, without Transfer-Encoding.')
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
        self.close_connection = self.close_after_body
        try:
            return json.loads(data.decode('utf-8'))
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise RequestError(400, 'The request body is not valid JSON.') from None

    def _admin(self, method):
        """True when this request was for an enabled administration API. Disabled, /admin is just an unknown route."""
        path = self.path.split('?', 1)[0].rstrip('/')
        if not self.server.admin_enabled or not (path == '/admin' or path.startswith('/admin/')):
            return False
        try:
            admin.handle(self, method, path)
        except SystemExit:  # a key file is corrupt: fail closed
            raise RequestError(500, 'The server key file could not be read.', 'api_error') from None
        return True

    def _no_such_method(self, method):
        try:
            if not self._admin(method):
                self._authenticate()
                raise RequestError(404, 'Unknown route.')
        except RequestError as error:
            self._fail(error)

    def do_PUT(self):
        self._no_such_method('PUT')

    def do_DELETE(self):
        self._no_such_method('DELETE')

    def do_GET(self):
        try:
            if self._admin('GET'):
                return
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
            if self._admin('POST'):
                return
            self._authenticate()
            if self.path.split('?', 1)[0].rstrip('/') != '/v1/chat/completions':
                raise RequestError(404, 'Unknown route.')
            request = protocol.parse_request(self._read_body())
            tool = tool_of(request['model'])
            emulate = None
            if request['tools'] and not capable(tool):
                emulate = emulation_target(tool, installed) if self.server.tool_emulation else None
                if not emulate:
                    raise RequestError(400, 'No configured provider supports tools for this model. Use omni/openrouter, '
                                       'omni/deepseek or omni/grok with an API key.', param='tools', code='tools_not_supported')
            request['emulate'] = emulate
            if not self.server.take_slot():
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
                if has_input(self.connection):
                    if not self.connection.recv(65536):
                        execution.stop(DISCONNECTED)
                        return
                    # Pipelined bytes: left in place they would hide the client leaving. They are not answered
                    self.close_connection = True
            except (OSError, ValueError):
                execution.stop(DISCONNECTED)
                return

    def _complete(self, request, tool):
        if request['emulate']:
            self._run(request, Emulated(request['emulate'], request))
            return
        if request['tools']:
            self._run(request, Native(tool, request))  # runs nothing locally, so it needs no folder of its own
            return
        workspaces = self.server.workspaces
        work = workspaces.acquire(self.headers) if workspaces else None
        execution = Execution(tool, request['prompt'], request['last_user'], request['temperature'], request['max_tokens'],
                              self.server.mode, work.path if work else self.server.workdir)
        execution.work = work
        self._run(request, execution)

    def _run(self, request, execution):
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
            work = getattr(execution, 'work', None)
            if work:
                work.finish()  # also after a failure or a timeout: what was done stays available
                self.work_header = work.header
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
        tool_calls = getattr(execution, 'tool_calls', None)
        if tool_calls:
            finish_reason = 'tool_calls'
        if stream:
            stream.finish(outcome, answer, finish_reason, usage, source, tool_calls)
        elif outcome != OK:
            status, message, kind, code = FAILURES.get(outcome, GENERIC_FAILURE)
            raise RequestError(status, message, kind, code=code, retry_after=60 if status == 429 else None)
        else:
            request_id = 'chatcmpl-' + uuid.uuid4().hex
            self._json(200, protocol.completion_body(request_id, int(time.time()), request['model'], answer, finish_reason, usage, tool_calls),
                       result_headers(request, execution, source))


def result_headers(request, execution, source=None):
    ignored = list(request['ignored'])
    if request['temperature'] is not None and not api_provider(execution.provider):
        ignored.insert(0, 'temperature')
    headers = {'X-Omni-Provider': str(execution.provider or execution.tool)}
    work = getattr(execution, 'work', None)
    if work and work.header:
        headers[work.header[0]] = work.header[1]
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

    def finish(self, outcome, answer, finish_reason, usage, source, tool_calls=None):
        if outcome != OK:
            status, message, kind, code = FAILURES.get(outcome, GENERIC_FAILURE)
            if not self.started:
                raise RequestError(status, message, kind, code=code, retry_after=60 if status == 429 else None)
            self._send(protocol.error_body(message, kind, code=code))  # too late to change the status
            self.handler.wfile.write(b'data: [DONE]\n\n')
            self.handler.wfile.flush()
            return
        already_sent = self.started
        self._start()
        if not already_sent and answer:
            self._chunk({'content': answer})
        if tool_calls:
            self._chunk({'tool_calls': [dict(call, index=index) for index, call in enumerate(tool_calls)]})
        self._chunk({}, finish_reason)
        if self.request['include_usage']:
            self._send(dict(protocol.chunk_body(self.id, self.created, self.request['model'], {}), choices=[], usage=usage))
        self.handler.wfile.write(b'data: [DONE]\n\n')
        self.handler.wfile.flush()
