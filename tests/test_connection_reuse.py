"""One connection, several requests: nothing from one request may leak into the next."""
import isolation  # noqa: F401  (first: keeps the tests away from real data)
import socket
import threading
import time
import unittest
from unittest.mock import patch

from omni_ai.server import auth, handler
from omni_ai.server.execution import DISCONNECTED

AUTH = b'Authorization: Bearer k\r\n'
VALID = b'{"model":"omni/none","messages":[{"role":"user","content":"hi"}]}'  # alone, this gets a 404


class Running(unittest.TestCase):
    def setUp(self):
        self.server = handler.Server(('127.0.0.1', 0))
        threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        patcher = patch.object(auth, 'verify', return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def connect(self):
        client = socket.create_connection(('127.0.0.1', self.server.server_address[1]), timeout=5)
        self.addCleanup(client.close)
        return client

    def everything(self, client):
        data = b''
        try:
            while chunk := client.recv(65536):
                data += chunk
        except TimeoutError:
            pass
        return data


class BodiesNobodyReads(Running):
    def test_a_get_body_is_not_read_as_the_next_request(self):
        client = self.connect()
        client.settimeout(1)
        client.sendall(b'GET /v1/models HTTP/1.1\r\nHost: x\r\n' + AUTH + b'Content-Length: 40\r\n\r\n'
                       b'GET /v1/hidden HTTP/1.1\r\nHost: x\r\n\r\n    ')
        answer = self.everything(client)
        self.assertEqual(answer.count(b'HTTP/1.1 '), 1)
        self.assertIn(b'Connection: close', answer)

    def test_get_without_a_body_keeps_the_connection(self):
        client = self.connect()
        client.settimeout(1)
        request = b'GET /v1/models HTTP/1.1\r\nHost: x\r\n' + AUTH + b'\r\n'
        client.sendall(request)
        time.sleep(0.2)
        client.sendall(request)
        self.assertEqual(self.everything(client).count(b'HTTP/1.1 200'), 2)

    def test_chunked_post_is_refused_and_closed(self):
        client = self.connect()
        client.sendall(b'POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n' + AUTH + b'Content-Length: %d\r\n' % len(VALID) +
                       b'Transfer-Encoding: chunked\r\n\r\n' + VALID + b'0\r\n\r\nGET /v1/hidden HTTP/1.1\r\nHost: x\r\n\r\n')
        answer = self.everything(client)
        self.assertEqual(answer.count(b'HTTP/1.1 '), 1)
        self.assertTrue(answer.startswith(b'HTTP/1.1 400'), answer[:60])


class BodiesRead(Running):
    def test_a_post_with_its_body_read_keeps_the_connection(self):
        client = self.connect()
        client.settimeout(1)
        client.sendall(b'POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n' + AUTH + b'Content-Length: %d\r\n\r\n' % len(VALID) + VALID)
        self.assertIn(b'HTTP/1.1 404', self.everything(client))

    def test_two_lengths_are_refused(self):
        client = self.connect()
        client.sendall(b'POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n' + AUTH + b'Content-Length: %d\r\nContent-Length: 4\r\n\r\n' % len(VALID) + VALID)
        answer = self.everything(client)
        self.assertTrue(answer.startswith(b'HTTP/1.1 400'), answer[:60])

    def test_asking_to_close_is_respected_on_delete(self):
        client = self.connect()
        client.sendall(b'DELETE /v1/nothing HTTP/1.1\r\nHost: x\r\nConnection: close\r\n' + AUTH + b'\r\n')
        self.assertIn(b'Connection: close', self.everything(client))


class ClientWatch(unittest.TestCase):
    def watch(self, script):
        """Runs the watcher against a real socket pair. `script` is what the client does."""
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        theirs.settimeout(5)  # with nobody reading, a large send would block forever
        this = handler.Handler.__new__(handler.Handler)
        this.connection = ours
        this.close_connection = False
        stops, finished = [], threading.Event()
        class Execution:
            def stop(self, reason):
                stops.append(reason)
        with patch.object(handler, 'WATCH_INTERVAL', 0.02):
            thread = threading.Thread(target=this._watch, args=(Execution(), finished), daemon=True)
            thread.start()
            script(theirs)
            time.sleep(0.2)
            finished.set()
            thread.join(2)
        return this, stops

    def test_a_client_that_leaves_is_noticed(self):
        _, stops = self.watch(lambda client: client.close())
        self.assertEqual(stops, [DISCONNECTED])

    def test_extra_bytes_do_not_turn_off_the_watch(self):
        def extra_then_leave(client):
            client.send(b'x')
            time.sleep(0.1)
            client.close()
        this, stops = self.watch(extra_then_leave)
        self.assertEqual(stops, [DISCONNECTED])
        self.assertTrue(this.close_connection)

    def test_a_client_that_stays_is_not_stopped(self):
        this, stops = self.watch(lambda client: None)
        self.assertEqual(stops, [])
        self.assertFalse(this.close_connection)

    def test_extra_bytes_from_a_client_that_stays_do_not_stop_it(self):
        this, stops = self.watch(lambda client: client.send(b'x' * 100000))
        self.assertEqual(stops, [])
        self.assertTrue(this.close_connection)


class WorkHeader(Running):
    def test_a_later_failure_does_not_repeat_the_previous_branch(self):
        def complete(this, request, tool):
            this.work_header = ('X-Omni-Branch', 'omni/0123456789abcdef')
            this._json(200, {'ok': True}, {'X-Omni-Branch': 'omni/0123456789abcdef'})
        client = self.connect()
        client.settimeout(1)
        body = b'{"model":"omni/codex","messages":[{"role":"user","content":"hi"}]}'
        request = b'POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n' + AUTH + b'Content-Length: %d\r\n\r\n' % len(body) + body
        with patch.object(handler.Handler, '_complete', complete), patch.object(handler, 'tool_of', return_value='codex'):
            client.sendall(request)
            time.sleep(0.3)
            client.sendall(b'GET /v1/nothing HTTP/1.1\r\nHost: x\r\n' + AUTH + b'\r\n')
            answer = self.everything(client)
        self.assertEqual(answer.count(b'X-Omni-Branch'), 1)
        self.assertIn(b'HTTP/1.1 404', answer)


if __name__ == '__main__':
    unittest.main()
