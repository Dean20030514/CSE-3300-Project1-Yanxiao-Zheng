import os
import sys
import socket
import time
import tempfile
import subprocess
import unittest
from pathlib import Path
import concurrent.futures

ROOT = Path(__file__).resolve().parent.parent
THREADED_SERVER = str(ROOT / 'server_threaded.py')
HOST = '127.0.0.1'

WORDS_FIXTURE = [
    'hello', 'hallo', 'hxllo', 'heLLo', 'world', 'hell', 'shell'
]


def find_free_port() -> int:
    s = socket.socket()
    s.bind((HOST, 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_until_ready(host: str, port: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError as e:
            last_err = e
            time.sleep(0.05)
    raise RuntimeError(f'server {host}:{port} not ready: {last_err}')


def start_server(port: int, wordlist_path: str) -> subprocess.Popen:
    cmd = [sys.executable, THREADED_SERVER, '--host', HOST, '--port', str(port), '--wordlist', wordlist_path, '--mode', 'exact']
    return subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=0)


def stop_server(proc: subprocess.Popen):
    if proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except OSError:
                pass
    for stream in (getattr(proc, 'stdout', None), getattr(proc, 'stderr', None)):
        try:
            if stream is not None:
                stream.close()
        except (OSError, ValueError):
            pass


def send_command(host: str, port: int, command: str):
    with socket.create_connection((host, port), timeout=2.0) as s:
        f = s.makefile('rwb', buffering=0)
        f.write((command + '\n').encode('utf-8'))
        status = f.readline().decode('utf-8').rstrip('\r\n')
        parts = status.split(' ', 2)
        code = int(parts[0]) if parts and parts[0].isdigit() else None
        count = None
        if len(parts) == 3 and parts[2].isdigit():
            count = int(parts[2])
        # drain to END
        while True:
            t = f.readline()
            if not t:
                break
            if t.decode('utf-8').rstrip('\r\n') == 'END':
                break
        return code, count


class TestConcurrencyLoad(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(prefix='wordlist_', suffix='.txt')
        os.close(fd)
        with open(path, 'w', encoding='utf-8') as f:
            for w in WORDS_FIXTURE:
                f.write(w + '\n')
        self.wordlist_path = path

    def tearDown(self):
        try:
            os.remove(self.wordlist_path)
        except FileNotFoundError:
            pass

    def test_many_concurrent_count_requests(self):
        port = find_free_port()
        proc = start_server(port, self.wordlist_path)
        try:
            wait_until_ready(HOST, port)
            N = 50
            with concurrent.futures.ThreadPoolExecutor(max_workers=N) as ex:
                futs = [ex.submit(send_command, HOST, port, 'COUNT h?llo --mode exact') for _ in range(N)]
                results = [f.result(timeout=5) for f in futs]
            for code, cnt in results:
                self.assertEqual(code, 200)
                self.assertEqual(cnt, 4)
        finally:
            stop_server(proc)


if __name__ == '__main__':
    unittest.main()
