import os
import sys
import socket
import time
import tempfile
import subprocess
import unittest
from pathlib import Path
import base64
import gzip

ROOT = Path(__file__).resolve().parent.parent
BASIC_SERVER = str(ROOT / 'server_basic.py')
THREADED_SERVER = str(ROOT / 'server_threaded.py')

HOST = '127.0.0.1'

WORDS_FIXTURE = [
    'hello',
    'hallo',
    'hxllo',
    'heLLo',
    'world',
    'hell',
    'shell',
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


def start_server(script: str, port: int, wordlist_path: str, mode: str | None = None) -> subprocess.Popen:
    cmd = [sys.executable, script, '--host', HOST, '--port', str(port), '--wordlist', wordlist_path]
    if mode is not None:
        cmd.extend(['--mode', mode])
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
        status = f.readline()
        if not status:
            raise RuntimeError('no status line from server')
        status_s = status.decode('utf-8').rstrip('\r\n')
        parts = status_s.split(' ', 2)
        code = int(parts[0]) if parts and parts[0].isdigit() else None
        count = None
        if len(parts) == 3 and parts[2].isdigit():
            count = int(parts[2])
        lines = []
        if code == 200 and count is not None:
            s_next = f.readline()
            if not s_next:
                raise RuntimeError('unexpected EOF waiting for END')
            t = s_next.decode('utf-8').rstrip('\r\n')
            if t == 'END':
                return code, count, lines
            else:
                lines.append(t)
                while len(lines) < count:
                    t2 = f.readline()
                    if not t2:
                        raise RuntimeError('unexpected EOF reading data lines')
                    lines.append(t2.decode('utf-8').rstrip('\r\n'))
                end_line = f.readline().decode('utf-8').rstrip('\r\n')
                if end_line != 'END':
                    raise RuntimeError('missing END terminator')
                return code, count, lines
        else:
            while True:
                t = f.readline()
                if not t:
                    break
                if t.decode('utf-8').rstrip('\r\n') == 'END':
                    break
            return code, count, lines


def _decode_gzip_lines(lines: list[str]) -> list[str]:
    if len(lines) != 1 or not lines[0].startswith('GZIP '):
        raise AssertionError('expected single GZIP payload line')
    b64 = lines[0][5:]
    gz = base64.b64decode(b64)
    data = gzip.decompress(gz).decode('utf-8')
    return data.split('\n') if data else []


class TestExtendedProtocol(unittest.TestCase):
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

    def test_find_multi_and_range_basic(self):
        port = find_free_port()
        proc = start_server(BASIC_SERVER, port, self.wordlist_path)
        try:
            wait_until_ready(HOST, port)
            code, cnt, lines = send_command(HOST, port, 'FIND_MULTI h?llo world RANGE 0 2')
            self.assertEqual(code, 200)
            self.assertEqual(cnt, 2)
            # Expected union in original order, then paginated
            self.assertEqual(lines, ['hello', 'hallo'])
        finally:
            stop_server(proc)

    def test_find_multi_threaded_exact(self):
        port = find_free_port()
        proc = start_server(THREADED_SERVER, port, self.wordlist_path, mode='exact')
        try:
            wait_until_ready(HOST, port)
            code, cnt, lines = send_command(HOST, port, 'FIND_MULTI h?llo world')
            self.assertEqual(code, 200)
            self.assertEqual(cnt, 5)
            self.assertEqual(lines, ['hello', 'hallo', 'hxllo', 'heLLo', 'world'])
        finally:
            stop_server(proc)

    def test_gzip_response_threaded_partial(self):
        port = find_free_port()
        proc = start_server(THREADED_SERVER, port, self.wordlist_path, mode='partial')
        try:
            wait_until_ready(HOST, port)
            code, cnt, lines = send_command(HOST, port, 'FIND ell --accept-encoding gzip')
            self.assertEqual(code, 200)
            self.assertEqual(cnt, 1)
            decoded = _decode_gzip_lines(lines)
            self.assertCountEqual(decoded, ['hello', 'heLLo', 'hell', 'shell'])
        finally:
            stop_server(proc)

    def test_count_ignores_range_threaded(self):
        port = find_free_port()
        proc = start_server(THREADED_SERVER, port, self.wordlist_path, mode='partial')
        try:
            wait_until_ready(HOST, port)
            code, cnt, lines = send_command(HOST, port, 'COUNT ell RANGE 0 1')
            self.assertEqual(code, 200)
            # COUNT result should not be affected by RANGE pagination
            self.assertEqual(cnt, 4)
            self.assertEqual(lines, [])
        finally:
            stop_server(proc)


if __name__ == '__main__':
    unittest.main()
