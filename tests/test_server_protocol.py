import os
import sys
import socket
import time
import tempfile
import subprocess
import unittest
from pathlib import Path

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
    # Start server subprocess; pipe stderr/stdout for debugging if needed
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
    # Close any open pipes to avoid ResourceWarning on Windows/Python 3.12
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
        # Read status line
        status = f.readline()
        if not status:
            raise RuntimeError('no status line from server')
        status_s = status.decode('utf-8').rstrip('\r\n')
        # Expect format: "<code> <text> <count>"
        parts = status_s.split(' ', 2)
        code = int(parts[0]) if parts and parts[0].isdigit() else None
        count = None
        if len(parts) == 3 and parts[2].isdigit():
            count = int(parts[2])
        lines = []
        # If 200 OK and count is provided, read exactly count lines (for FIND) or possibly none (for COUNT)
        if code == 200 and count is not None:
            # Peek next line; if it's END, then it's COUNT-style
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
                # After data lines, expect END
                end_line = f.readline().decode('utf-8').rstrip('\r\n')
                if end_line != 'END':
                    raise RuntimeError('missing END terminator')
                return code, count, lines
        else:
            # For 404 or 400, read until END
            while True:
                t = f.readline()
                if not t:
                    break
                if t.decode('utf-8').rstrip('\r\n') == 'END':
                    break
            return code, count, lines


class TestServerProtocol(unittest.TestCase):
    def setUp(self):
        # Create a temp wordlist for deterministic tests
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

    def test_exact_matching_basic_find_and_count(self):
        port = find_free_port()
        proc = start_server(BASIC_SERVER, port, self.wordlist_path)
        try:
            wait_until_ready(HOST, port)
            # FIND exact pattern: h?llo -> hello/hallo/hxllo/heLLo (4 matches, case-insensitive)
            code, cnt, lines = send_command(HOST, port, 'FIND h?llo')
            self.assertEqual(code, 200)
            self.assertEqual(cnt, 4)
            self.assertCountEqual(lines, ['hello', 'hallo', 'hxllo', 'heLLo'])

            # COUNT h?llo -> 4, no lines
            code, cnt, lines = send_command(HOST, port, 'COUNT h?llo')
            self.assertEqual(code, 200)
            self.assertEqual(cnt, 4)
            self.assertEqual(lines, [])

            # basic 不支持 partial 覆盖
            code, cnt, _ = send_command(HOST, port, 'FIND h?llo --mode partial')
            self.assertEqual(code, 400)
        finally:
            stop_server(proc)

    def test_partial_matching_threaded_and_override(self):
        port = find_free_port()
        proc = start_server(THREADED_SERVER, port, self.wordlist_path, mode='partial')
        try:
            wait_until_ready(HOST, port)
            # 默认 partial：FIND ell -> 包含 ell 子串的单词
            code, cnt, lines = send_command(HOST, port, 'FIND ell')
            self.assertEqual(code, 200)
            # expected: hello, heLLo, hell, shell
            self.assertEqual(cnt, 4)
            self.assertCountEqual(lines, ['hello', 'heLLo', 'hell', 'shell'])

            # 覆盖 exact：FIND h?llo --mode exact -> 与 basic 相同 4 条
            code, cnt, lines = send_command(HOST, port, 'FIND h?llo --mode exact')
            self.assertEqual(code, 200)
            self.assertEqual(cnt, 4)
            self.assertCountEqual(lines, ['hello', 'hallo', 'hxllo', 'heLLo'])

            # COUNT 覆盖 exact
            code, cnt, lines = send_command(HOST, port, 'COUNT h?llo --mode exact')
            self.assertEqual(code, 200)
            self.assertEqual(cnt, 4)
            self.assertEqual(lines, [])
        finally:
            stop_server(proc)

    def test_stats_and_concurrent_clients(self):
        port = find_free_port()
        proc = start_server(THREADED_SERVER, port, self.wordlist_path, mode='partial')
        try:
            wait_until_ready(HOST, port)
            # 先调用若干 COUNT
            for _ in range(5):
                code, cnt, _ = send_command(HOST, port, 'COUNT h?llo --mode exact')
                self.assertEqual(code, 200)
                self.assertEqual(cnt, 4)

            # STATS
            code, cnt, lines = send_command(HOST, port, 'STATS')
            self.assertEqual(code, 200)
            self.assertGreaterEqual(cnt, 1)
            keys = {line.split(' ')[0] for line in lines}
            for k in ['connections', 'requests', 'count_requests', 'stats_requests', 'ok_responses', 'words_total']:
                self.assertIn(k, keys)

            # 并发客户端：10 个同时 COUNT
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
                futs = [ex.submit(send_command, HOST, port, 'COUNT h?llo --mode exact') for _ in range(10)]
                results = [f.result(timeout=5) for f in futs]
            for code, cnt, lines in results:
                self.assertEqual(code, 200)
                self.assertEqual(cnt, 4)
                self.assertEqual(lines, [])
        finally:
            stop_server(proc)


if __name__ == '__main__':
    unittest.main()
