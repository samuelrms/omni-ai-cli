import isolation  # noqa: F401  (first: keeps the tests away from real data)
import http.client
import os
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from omni_ai.server import handler  # noqa: E402


class Running(unittest.TestCase):
    limit = 4

    def setUp(self):
        with patch.object(handler, 'connection_limit', return_value=self.limit):
            self.server = handler.Server(('127.0.0.1', 0))
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.opened = []
        self.addCleanup(lambda: [item.close() for item in self.opened])

    def connect(self):
        connection = socket.create_connection(('127.0.0.1', self.port), timeout=10)
        self.opened.append(connection)
        return connection

    def status(self):
        client = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        self.addCleanup(client.close)
        client.request('GET', '/v1/models')
        return client.getresponse().status


class Limit(Running):
    def test_over_the_limit_is_refused_and_it_recovers(self):
        for _ in range(self.limit):
            self.connect().send(b'GET / HTTP/1.1\r\nX-A: ')
        time.sleep(0.2)
        self.assertEqual(self.status(), 503)
        for item in self.opened:
            item.close()
        self.opened.clear()
        deadline = time.time() + 10
        while self.status() != 401:
            self.assertLess(time.time(), deadline)
            time.sleep(0.05)


class SlowClients(Running):
    def setUp(self):
        for name, value in (('HEADER_DEADLINE', 1), ('WATCH_INTERVAL', 0.05)):
            patcher = patch.object(handler, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        super().setUp()

    def test_headers_sent_byte_by_byte_do_not_keep_the_connection(self):
        connection = self.connect()
        started = time.time()
        connection.settimeout(0.2)
        closed = False
        while time.time() - started < 6:
            try:
                connection.send(b'X')
                if connection.recv(1, socket.MSG_PEEK) == b'':
                    closed = True
                    break
            except socket.timeout:
                pass
            except OSError:
                closed = True
                break
        self.assertTrue(closed)
        self.assertLess(time.time() - started, 5)
        self.assertEqual(self.server.reading, {})

    def test_requests_within_the_deadline_still_work(self):
        self.assertEqual(self.status(), 401)
        self.assertEqual(self.server.reading, {})


class Limits(unittest.TestCase):
    def test_keeps_room_for_requests_and_descriptors(self):
        import resource
        with patch.object(resource, 'getrlimit', return_value=(100000, 100000)):
            self.assertEqual(handler.connection_limit(4), handler.MIN_CONNECTIONS)
            self.assertEqual(handler.connection_limit(100), 264)
        with patch.object(resource, 'getrlimit', return_value=(256, 256)):
            self.assertEqual(handler.connection_limit(4), 112)
        with patch.object(resource, 'getrlimit', return_value=(16, 16)):
            self.assertEqual(handler.connection_limit(4), 8)


class HighDescriptors(unittest.TestCase):
    def test_watching_a_client_above_descriptor_1024(self):
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        wanted = 1400
        if hard != resource.RLIM_INFINITY and hard < wanted:
            self.skipTest('the system does not allow this many descriptors')
        resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, wanted), hard))
        self.addCleanup(resource.setrlimit, resource.RLIMIT_NOFILE, (soft, hard))
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        high = socket.socket(fileno=os.dup2(right.fileno(), 1300))
        self.addCleanup(high.close)
        self.assertFalse(handler.has_input(high))
        left.send(b'x')
        self.assertTrue(handler.has_input(high))


if __name__ == '__main__':
    unittest.main()
