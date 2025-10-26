import os
import sys
import socket
import time
import tempfile
import subprocess
import unittest
from pathlib import Path

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


def start_server(port: int, wordlist_path: str, env: dict | None = None) -> subprocess.Popen:
    cmd = [sys.executable, THREADED_SERVER, '--host', HOST, '--port', str(port), '--wordlist', wordlist_path]
    env_full = os.environ.copy()
    if env:
        env_full.update(env)
    return subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env_full, creationflags=0)


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


def read_status_and_drain(f):
    status = f.readline()
    if not status:
        return None, None
    s = status.decode('utf-8', errors='replace').rstrip('\r\n')
    parts = s.split(' ', 2)
    code = int(parts[0]) if parts and parts[0].isdigit() else None
    count = None
    if len(parts) == 3 and parts[2].isdigit():
        count = int(parts[2])
    # drain
    while True:
        t = f.readline()
        if not t:
            break
        if t.decode('utf-8', errors='replace').rstrip('\r\n') == 'END':
            break
    return code, count


class TestFuzzInputs(unittest.TestCase):
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

    def test_invalid_encoding_flag(self):
        port = find_free_port()
        proc = start_server(port, self.wordlist_path)
        try:
            wait_until_ready(HOST, port)
            with socket.create_connection((HOST, port), timeout=2.0) as s:
                f = s.makefile('rwb', buffering=0)
                f.write(b"FIND ell --accept-encoding brotli\n")
                code, _ = read_status_and_drain(f)
                self.assertEqual(code, 400)
        finally:
            stop_server(proc)

    def test_invalid_range(self):
        port = find_free_port()
        proc = start_server(port, self.wordlist_path)
        try:
            wait_until_ready(HOST, port)
            with socket.create_connection((HOST, port), timeout=2.0) as s:
                f = s.makefile('rwb', buffering=0)
                f.write(b"FIND ell RANGE a b\n")
                code, _ = read_status_and_drain(f)
                self.assertEqual(code, 400)
        finally:
            stop_server(proc)

    def test_non_utf8(self):
        port = find_free_port()
        proc = start_server(port, self.wordlist_path)
        try:
            wait_until_ready(HOST, port)
            with socket.create_connection((HOST, port), timeout=2.0) as s:
                f = s.makefile('rwb', buffering=0)
                # invalid UTF-8 in pattern
                f.write(b"FIND \xff\xfe\n")
                status = f.readline()
                self.assertTrue(status)
                self.assertIn(b"400 BAD-REQUEST", status)
        finally:
            stop_server(proc)

    def test_too_long_pattern(self):
        port = find_free_port()
        proc = start_server(port, self.wordlist_path)
        try:
            wait_until_ready(HOST, port)
            with socket.create_connection((HOST, port), timeout=2.0) as s:
                f = s.makefile('rwb', buffering=0)
                long_pat = 'a' * 5000
                f.write(("FIND " + long_pat + "\n").encode('utf-8'))
                code, _ = read_status_and_drain(f)
                self.assertEqual(code, 400)
        finally:
            stop_server(proc)

    def test_complexity_guard_questions(self):
        # Lower the question mark threshold via env so we can trigger within max_pattern_length
        port = find_free_port()
        env = {'SERVER_MAX_QUESTIONS': '50'}
        proc = start_server(port, self.wordlist_path, env=env)
        try:
            wait_until_ready(HOST, port)
            with socket.create_connection((HOST, port), timeout=2.0) as s:
                f = s.makefile('rwb', buffering=0)
                pat = '?' * 100
                f.write(("FIND " + pat + "\n").encode('utf-8'))
                status = f.readline()
                self.assertTrue(status)
                self.assertIn(b"400 BAD-REQUEST", status)
        finally:
            stop_server(proc)

    def test_invalid_command(self):
        port = find_free_port()
        proc = start_server(port, self.wordlist_path)
        try:
            wait_until_ready(HOST, port)
            with socket.create_connection((HOST, port), timeout=2.0) as s:
                f = s.makefile('rwb', buffering=0)
                f.write(b"PING\n")
                status = f.readline()
                self.assertTrue(status)
                self.assertIn(b"400 BAD-REQUEST", status)
        finally:
            stop_server(proc)


if __name__ == '__main__':
    unittest.main()
