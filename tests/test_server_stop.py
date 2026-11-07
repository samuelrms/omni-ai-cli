"""The server stops when asked, whatever it was doing at that moment."""
import isolation  # noqa: F401  (first: keeps the tests away from real data)
import json
import os
from pathlib import Path
import re
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from omni_ai.server import handler

ROOT = Path(__file__).resolve().parent.parent


class Slots(unittest.TestCase):
    def setUp(self):
        with patch.object(handler, 'connection_limit', return_value=4):
            self.server = handler.Server(('127.0.0.1', 0))
        self.addCleanup(self.server.server_close)
        self.pair = socket.socketpair()
        self.addCleanup(self.pair[1].close)
        self.addCleanup(self.pair[0].close)

    def free_slots(self):
        return self.server.connections._value

    def test_a_thread_that_could_not_start_gives_its_slot_back(self):
        before = self.free_slots()
        with patch.object(socketserver.ThreadingMixIn, 'process_request', side_effect=RuntimeError("can't start new thread")), \
                self.assertRaises(RuntimeError):
            self.server.process_request(self.pair[0], ('127.0.0.1', 1))
        self.assertEqual(self.free_slots(), before)

    def test_an_interruption_stays_an_interruption(self):
        # The thread already runs here and gives the slot back itself: releasing it again raised another error
        def started_then_interrupted(_server, request, address):
            thread = threading.Thread(target=self.server.process_request_thread, args=(request, address), daemon=True)
            thread.start()
            thread.join(5)
            raise KeyboardInterrupt
        with patch.object(socketserver.ThreadingMixIn, 'process_request', started_then_interrupted), \
                patch.object(handler.Server, 'finish_request'), self.assertRaises(KeyboardInterrupt):
            self.server.process_request(self.pair[0], ('127.0.0.1', 1))


@unittest.skipIf(os.name == 'nt', 'POSIX signals')
class RealServer(unittest.TestCase):
    def start(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / 'profiles.json').write_text(json.dumps({'profiles': {'main': {'priority': 1, 'enabled': True}}}))
        env = {**os.environ, 'HOME': str(root), 'PYTHONPATH': str(ROOT), 'SMART_AI_HOME': str(root / 'cfg'),
               'SMART_AI_CONFIG': str(root / 'profiles.json'), 'SMART_AI_STATE': str(root / 'state'),
               'SMART_AI_LOG': str(root / 'usage.log'), 'SMART_AI_SERVER_KEY': 'k'}
        process = subprocess.Popen([sys.executable, '-m', 'omni_ai.cli', 'serve', '--port', '0'], env=env, cwd=root,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(process.stdout.close)
        self.addCleanup(lambda: (process.kill(), process.wait()))
        return process, int(re.search(r':(\d+)/v1', process.stdout.readline()).group(1))

    def test_it_stops_while_connections_keep_arriving(self):
        for _ in range(6):
            process, port = self.start()
            stop = threading.Event()

            def hammer():
                while not stop.is_set():
                    try:
                        socket.create_connection(('127.0.0.1', port), timeout=1).close()
                    except OSError:
                        return
            threads = [threading.Thread(target=hammer, daemon=True) for _ in range(6)]
            for thread in threads:
                thread.start()
            time.sleep(0.15)
            process.terminate()
            try:
                self.assertEqual(process.wait(10), 0)
            finally:
                stop.set()
                for thread in threads:
                    thread.join(3)

    def test_it_stops_with_a_connection_left_open(self):
        process, port = self.start()
        client = socket.create_connection(('127.0.0.1', port), timeout=5)
        self.addCleanup(client.close)
        client.sendall(b'GET /v1/models HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer k\r\n\r\n')
        client.recv(4096)
        process.terminate()
        self.assertEqual(process.wait(10), 0)


if __name__ == '__main__':
    unittest.main()
