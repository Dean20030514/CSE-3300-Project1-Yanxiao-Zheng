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


def start_server(script: str, port: int, wordlist_path: str, mode: str | None = None, env: dict | None = None) -> subprocess.Popen:
    cmd = [sys.executable, script, '--host', HOST, '--port', str(port), '--wordlist', wordlist_path]
    if mode is not None:
        cmd.extend(['--mode', mode])
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


def send_command(host: str, port: int, command: str):
    with socket.create_connection((host, port), timeout=2.0) as s:
        f = s.makefile('rwb', buffering=0)
        f.write((command + '\n').encode('utf-8'))
        status = f.readline()
        if not status:
            return None, None, []
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
                return code, count, lines
            t = s_next.decode('utf-8').rstrip('\r\n')
            if t == 'END':
                return code, count, lines
            else:
                lines.append(t)
                while len(lines) < count:
                    t2 = f.readline()
                    if not t2:
                        break
                    lines.append(t2.decode('utf-8').rstrip('\r\n'))
                # consume END terminator
                _ = f.readline()
                return code, count, lines
        else:
            while True:
                t = f.readline()
                if not t:
                    break
                if t.decode('utf-8').rstrip('\r\n') == 'END':
                    break
            return code, count, lines


class TestIntegrationMore(unittest.TestCase):
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

    def test_star_in_partial_threaded(self):
        port = find_free_port()
        proc = start_server(THREADED_SERVER, port, self.wordlist_path, mode='partial')
        try:
            wait_until_ready(HOST, port)
            code, cnt, lines = send_command(HOST, port, 'FIND h*o')
            self.assertEqual(code, 200)
            self.assertEqual(cnt, 4)
            self.assertCountEqual(lines, ['hello', 'hallo', 'hxllo', 'heLLo'])
        finally:
            stop_server(proc)


if __name__ == '__main__':
    unittest.main()
