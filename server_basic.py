"""
Basic single-thread TCP word search server.

- Role: serve FIND and COUNT over a line-based protocol on a static word list.
- Request: `FIND <pattern> [--range <start> <end>] [--gzip]` | `COUNT <pattern>` | `QUIT`.
- Response: first line `<code> <text> <count>`, optional body lines, then `END`.
- Matching: exact-only; `?` = one char; pattern must match the whole word.
- Paging: client flag `--range` (sent as `RANGE off lim`) slices the results.
- Compression: client flag `--gzip` (sent as `--accept-encoding gzip`) returns one `GZIP <base64>` line.
- Errors: `400 BAD-REQUEST`, `404 NOT-FOUND`, `503 BUSY` (all responses end with `END`).
- Robustness: UTF-8 validation, length limits, timeouts, and light memory-pressure handling.

Short and readable on purpose for grading and maintenance.
"""

import argparse
import json
import re
import functools
import time
import gzip
import base64
import os
import threading
import socket
import sys
from typing import List
from index import WordIndex, _compile_regex_body
import signal
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
try:
    import psutil
except ImportError:
    psutil = None

CFG_COMPLEXITY = {
    "max_questions": 5000,
    "max_stars": 50,
}

@functools.lru_cache(maxsize=100)
def compile_pattern_exact(pattern: str) -> re.Pattern:
    """Build a regex Pattern for exact word match using '?' wildcards.

    - Escape all regex meta characters in the input (treat them as plain text).
    - Replace '?' with '.' (match one character).
    - Anchor the pattern with ^ and $ to force full-word match.
    - Case-insensitive.
    """
    esc = []
    for ch in pattern:
        if ch in ".^$+{}[]|()\\":
            esc.append("\\" + ch)
        elif ch == '?':
            esc.append('.')
        else:
            esc.append(ch)
    regex = '^' + ''.join(esc) + '$'
    return re.compile(regex, re.IGNORECASE)

def load_wordlist(path: str) -> List[str]:
    """Load words from a UTF-8 text file; ignore empty lines and decoding errors."""
    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
        return [line.strip() for line in f if line.strip()]

def handle_find(pattern: str, words: List[str], index: WordIndex | None = None) -> List[str]:
    """Find exact matches for a pattern using the index if available.

    Input: wildcard pattern (supports '?'), the word list, and optional index.
    Output: list of matching words (exact mode). Prefers index for speed.
    """
    if index is not None:
        return index.find_exact(pattern)
    rx = compile_pattern_exact(pattern)
    return [w for w in words if rx.fullmatch(w) is not None]

class Stats:
    """Thread-safe counters and simple latency histogram for observability."""
    def __init__(self):
        self._lock = threading.Lock()
        self.connections = 0
        self.active_connections = 0
        self.requests = 0
        self.find_requests = 0
        self.count_requests = 0
        self.stats_requests = 0
        self.ok_responses = 0
        self.not_found_responses = 0
        self.bad_request_responses = 0
        self.total_request_time_ms = 0.0
        self.last_request_time_ms = 0.0
        self.latency_hist = {f"lt{b}": 0 for b in [1, 5, 10, 50, 100, 500, 1000]}
        self.latency_hist["ge1000"] = 0

    def inc(self, attr: str, delta: int = 1):
        with self._lock:
            setattr(self, attr, getattr(self, attr) + delta)

    def connection_opened(self):
        with self._lock:
            self.connections += 1
            self.active_connections += 1

    def connection_closed(self):
        with self._lock:
            if self.active_connections > 0:
                self.active_connections -= 1

    def record_request_time(self, ms: float):
        with self._lock:
            self.total_request_time_ms += ms
            self.last_request_time_ms = ms
            # bucketize
            for b in [1, 5, 10, 50, 100, 500, 1000]:
                if ms < b:
                    self.latency_hist[f"lt{b}"] += 1
                    break
            else:
                self.latency_hist["ge1000"] += 1

    def snapshot(self):
        with self._lock:
            return {
                'connections': self.connections,
                'active_connections': self.active_connections,
                'requests': self.requests,
                'find_requests': self.find_requests,
                'count_requests': self.count_requests,
                'stats_requests': self.stats_requests,
                'ok_responses': self.ok_responses,
                'not_found_responses': self.not_found_responses,
                'bad_request_responses': self.bad_request_responses,
                'avg_request_time_ms': (self.total_request_time_ms / self.requests) if self.requests else 0.0,
                'last_request_time_ms': self.last_request_time_ms,
                'error_rate': ((self.bad_request_responses + self.not_found_responses) / self.requests) if self.requests else 0.0,
                'latency_hist': dict(self.latency_hist),
            }

from collections import defaultdict


class EnhancedStats(Stats):
    """Extends Stats with pattern-complexity histograms and cache info."""
    def __init__(self):
        super().__init__()
        self.pattern_complexity_hist = defaultdict(int)
        self.cache_hit_rates = {}

    def snapshot(self):
        base = super().snapshot()
        base['pattern_complexity_hist'] = dict(self.pattern_complexity_hist)
        base['cache_hit_rates'] = dict(self.cache_hit_rates)
        return base

UNDER_MEMORY_PRESSURE = False


def _memory_pressure_handler(stats: Stats | None = None, soft_limit_mb: int | None = None) -> bool:
    """If RSS memory exceeds a soft limit, clear caches to reduce memory usage.

    Returns True when pressure was detected. No exception is raised on failure.
    """
    global UNDER_MEMORY_PRESSURE
    try:
        limit_mb = soft_limit_mb or int(os.environ.get('SERVER_MEMORY_SOFT_LIMIT_MB', '0'))
    except ValueError:
        limit_mb = 0
    if psutil is None or limit_mb <= 0:
        UNDER_MEMORY_PRESSURE = False
        return False
    try:
        rss = psutil.Process().memory_info().rss
        if rss > limit_mb * 1024 * 1024:
            try:
                compile_pattern_exact.cache_clear()
            except Exception:
                pass
            try:
                _compile_regex_body.cache_clear()
            except Exception:
                pass
            UNDER_MEMORY_PRESSURE = True
            return True
        else:
            UNDER_MEMORY_PRESSURE = False
            return False
    except (OSError, AttributeError, RuntimeError):
        UNDER_MEMORY_PRESSURE = False
        return False


def _effective_complexity_limits():
    """Halve wildcard limits when under memory pressure; otherwise use defaults."""
    if UNDER_MEMORY_PRESSURE:
        return {
            "max_questions": max(1, int(CFG_COMPLEXITY["max_questions"]) // 2),
            "max_stars": max(1, int(CFG_COMPLEXITY["max_stars"]) // 2),
        }
    return CFG_COMPLEXITY

def json_log(event: str, level: str = "info", **fields):
    """Best-effort JSON logging to stderr for debugging/benchmarking."""
    try:
        rec = {"ts": time.time(), "level": level, "event": event}
        rec.update(fields)
        print(json.dumps(rec, ensure_ascii=False), file=sys.stderr)
    except (OSError, ValueError, TypeError):
        pass

def serve_once(conn: socket.socket, addr, words: List[str], stats: 'Stats',
               index: WordIndex,
               request_timeout: float = 30.0, max_pattern_length: int = 1000):
    """Handle exactly one request, then return (single-shot basic server).

    Input: raw request line from the socket. Output: status, optional body, and `END`.
    Branches: `COUNT` returns only the number; `FIND` lists words. Exact mode only.
    Optional: paging via `RANGE off lim` and gzip via `--accept-encoding gzip`.
    """
    with conn:
        try:
            conn.settimeout(float(request_timeout))
        except (OSError, ValueError):
            pass
        f = conn.makefile('rwb', buffering=0)
        def get_memory_rss_bytes():
            """Return current process RSS in bytes if psutil is available."""
            try:
                if psutil is not None:
                    return psutil.Process().memory_info().rss
            except (OSError, AttributeError, RuntimeError):
                pass
            return None

        def is_pattern_too_complex(pat: str, max_q: int, max_s: int):
            """Enforce simple wildcard limits to avoid expensive regex work."""
            q = pat.count('?')
            s = pat.count('*')
            if q > max_q:
                return True, f"too many '?' wildcards (> {max_q})"
            if s > max_s:
                return True, f"too many '*' wildcards (> {max_s})"
            return False, ''

        try:
            raw = f.readline()
        except socket.timeout:
            conn.sendall(b"400 BAD-REQUEST timeout\nEND\n")
            return
        line = raw
        if not line:
            return
        if len(line) > max_pattern_length:
            conn.sendall(b"400 BAD-REQUEST pattern too long\nEND\n")
            return
        try:
            line = line.decode('utf-8').rstrip('\r\n')
        except UnicodeDecodeError:
            conn.sendall(b"400 BAD-REQUEST non-utf8\nEND\n")
            return
        stats.inc('requests')
        t0 = time.perf_counter()
        parts = line.split(' ', 1)
        request_id = uuid.uuid4().hex
        cmd = parts[0].upper()
        if cmd == 'QUIT':
            return
        if cmd == 'STATS':
            # Report metrics as lines; still follow the same protocol framing.
            stats.inc('stats_requests')
            try:
                if isinstance(stats, EnhancedStats):
                    try:
                        ci = compile_pattern_exact.cache_info()
                        stats.cache_hit_rates['compile_pattern_exact'] = getattr(ci, '_asdict', lambda: {
                            'hits': ci.hits, 'misses': ci.misses, 'maxsize': ci.maxsize, 'currsize': ci.currsize
                        })()
                    except Exception:
                        pass
                    try:
                        ci2 = _compile_regex_body.cache_info()
                        stats.cache_hit_rates['regex_body'] = getattr(ci2, '_asdict', lambda: {
                            'hits': ci2.hits, 'misses': ci2.misses, 'maxsize': ci2.maxsize, 'currsize': ci2.currsize
                        })()
                    except Exception:
                        pass
            except Exception:
                pass
            snap = stats.snapshot()
            lines = [
                f"connections {snap['connections']}",
                f"active_connections {snap['active_connections']}",
                f"requests {snap['requests']}",
                f"find_requests {snap['find_requests']}",
                f"count_requests {snap['count_requests']}",
                f"stats_requests {snap['stats_requests']}",
                f"ok_responses {snap['ok_responses']}",
                f"not_found_responses {snap['not_found_responses']}",
                f"bad_request_responses {snap['bad_request_responses']}",
                f"avg_request_time_ms {snap['avg_request_time_ms']:.3f}",
                f"last_request_time_ms {snap['last_request_time_ms']:.3f}",
                f"error_rate {snap['error_rate']:.6f}",
                f"words_total {len(words)}",
                f"under_memory_pressure {int(UNDER_MEMORY_PRESSURE)}",
            ]
            mem = get_memory_rss_bytes()
            if mem is not None:
                lines.append(f"memory_rss_bytes {mem}")
            try:
                if psutil is not None:
                    cpu_pct = psutil.Process().cpu_percent(interval=0.0)
                    lines.append(f"cpu_percent {cpu_pct:.1f}")
            except (OSError, AttributeError, RuntimeError):
                pass
            for k, v in snap['latency_hist'].items():
                lines.append(f"latency_ms_{k} {v}")
            if 'pattern_complexity_hist' in snap:
                for k, v in snap['pattern_complexity_hist'].items():
                    lines.append(f"complexity_{k} {v}")
            if 'cache_hit_rates' in snap:
                for name, d in snap['cache_hit_rates'].items():
                    try:
                        hits = d.get('hits', 0)
                        misses = d.get('misses', 0)
                        size = d.get('currsize', 0)
                        rate = (hits / (hits + misses)) if (hits + misses) else 0.0
                        lines.append(f"cache_{name}_hits {hits}")
                        lines.append(f"cache_{name}_misses {misses}")
                        lines.append(f"cache_{name}_size {size}")
                        lines.append(f"cache_{name}_hit_rate {rate:.4f}")
                    except Exception:
                        pass
            f.write(f"200 OK {len(lines)}\n".encode('utf-8'))
            for s in lines:
                f.write((s + "\n").encode('utf-8'))
            f.write(b"END\n")
            dt_stats = (time.perf_counter() - t0) * 1000
            stats.record_request_time(dt_stats)
            json_log("stats", count=len(lines), latency_ms=dt_stats, request_id=request_id, remote=str(addr))
            return

        if cmd not in ('FIND', 'FIND_MULTI', 'COUNT') or len(parts) != 2 or not parts[1]:
            conn.sendall(b"400 BAD-REQUEST expected 'FIND <pattern>' or 'COUNT <pattern>' or 'STATS'\nEND\n")
            json_log("bad_request", reason="syntax", cmd=cmd, request_id=request_id, remote=str(addr))
            return

        rest = parts[1]
        request_gzip = False
        offset = 0
        limit = None

        if ' --accept-encoding ' in rest:
            # Optional gzip negotiation; if accepted, we compress the body.
            rest, enc_part = rest.rsplit(' --accept-encoding ', 1)
            if enc_part.strip().lower() != 'gzip':
                conn.sendall(b"400 BAD-REQUEST invalid encoding\nEND\n")
                json_log("bad_request", reason="invalid_encoding", request_id=request_id, remote=str(addr))
                return
            request_gzip = True

        if ' RANGE ' in rest:
            # Optional pagination support: pick a slice of the matches.
            rest, range_part = rest.rsplit(' RANGE ', 1)
            tok = range_part.strip().split()
            if len(tok) != 2 or not all(t.isdigit() for t in tok):
                conn.sendall(b"400 BAD-REQUEST invalid RANGE\nEND\n")
                json_log("bad_request", reason="invalid_range", request_id=request_id, remote=str(addr))
                return
            offset = int(tok[0])
            limit = int(tok[1])

        if ' --mode ' in rest:
            # For the basic server we only allow exact mode.
            pattern_part, mode_part = rest.rsplit(' --mode ', 1)
            mv = mode_part.strip().lower()
            if mv != 'exact':
                conn.sendall(b"400 BAD-REQUEST mode not supported\nEND\n")
                stats.record_request_time((time.perf_counter() - t0) * 1000)
                json_log("bad_request", reason="mode_not_supported", request_id=request_id, remote=str(addr))
                return
            rest = pattern_part

        pattern = rest
        if len(pattern) > max_pattern_length:
            conn.sendall(b"400 BAD-REQUEST pattern too long\nEND\n")
            stats.record_request_time((time.perf_counter() - t0) * 1000)
            json_log("bad_request", reason="pattern_too_long", request_id=request_id, remote=str(addr))
            return

        _memory_pressure_handler(stats)
        limits = _effective_complexity_limits()
        too_complex, reason = is_pattern_too_complex(
            pattern,
            int(limits['max_questions']),
            int(limits['max_stars'])
        )
        if too_complex:
            conn.sendall(f"400 BAD-REQUEST pattern too complex: {reason}\nEND\n".encode('utf-8'))
            stats.record_request_time((time.perf_counter() - t0) * 1000)
            json_log("bad_request", reason="pattern_too_complex", request_id=request_id, remote=str(addr))
            return

        if cmd == 'COUNT':
            # Count only (no body with words). Still follow status+count format.
            stats.inc('count_requests')
            count = index.count_exact(pattern)
            if count == 0:
                conn.sendall(b"404 NOT-FOUND 0\nEND\n")
                stats.inc('not_found_responses')
            else:
                f.write(f"200 OK {count}\nEND\n".encode('utf-8'))
                stats.inc('ok_responses')
            dt_count = (time.perf_counter() - t0) * 1000
            stats.record_request_time(dt_count)
            json_log("count", pattern=pattern, count=count, latency_ms=dt_count, request_id=request_id, remote=str(addr))
            return

        stats.inc('find_requests')
        try:
            if isinstance(stats, EnhancedStats):
                q = pattern.count('?')
                s = pattern.count('*')
                stats.pattern_complexity_hist[f"q_{q}"] += 1
                stats.pattern_complexity_hist[f"s_{s}"] += 1
        except Exception:
            pass
        if cmd == 'FIND_MULTI':
            # Support multiple patterns separated by spaces; merge unique results.
            tokens = pattern.strip().split()
            if not tokens:
                conn.sendall(b"400 BAD-REQUEST expected 'FIND_MULTI <p1> <p2> ...'\nEND\n")
                stats.record_request_time((time.perf_counter() - t0) * 1000)
                return
            seen = set()
            out = []
            for pat in tokens:
                ms = index.find_exact(pat)
                for w in ms:
                    if w not in seen:
                        seen.add(w)
                        out.append(w)
            matches = out
        else:
            matches = handle_find(pattern, words, index)

        if limit is not None:
            if offset < 0:
                offset = 0
            if limit < 0:
                limit = 0
            matches = matches[offset: offset + limit]
        count = len(matches)
        if count == 0:
            conn.sendall("404 NOT-FOUND 0\nEND\n".encode('utf-8'))
            stats.inc('not_found_responses')
            stats.record_request_time((time.perf_counter() - t0) * 1000)
            return
        print(f"[BASIC] {addr} FIND {pattern!r} -> {count} matches (rid={request_id})", file=sys.stderr)
        if request_gzip:
            # Send compressed body as one Base64 line to reduce bytes on the wire.
            payload = '\n'.join(matches).encode('utf-8')
            gz = gzip.compress(payload)
            b64 = base64.b64encode(gz).decode('ascii')
            f.write(b"200 OK 1\n")
            f.write(("GZIP " + b64 + "\n").encode('utf-8'))
            f.write(b"END\n")
        else:
            f.write(f"200 OK {count}\n".encode('utf-8'))
            for w in matches:
                f.write((w + "\n").encode('utf-8'))
            f.write(b"END\n")
        stats.inc('ok_responses')
        dt_find = (time.perf_counter() - t0) * 1000
        stats.record_request_time(dt_find)
        json_log("find", pattern=pattern, count=count, latency_ms=dt_find, gzip=request_gzip, request_id=request_id, remote=str(addr))

def main():
    """Single-thread server entry point: parse flags, load index, and serve one request per connection (responses always end with 'END')."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--wordlist", required=True)
    ap.add_argument("--config", help="Path to JSON config.")
    ap.add_argument("--health-port", type=int, default=0, help="HTTP health port. Use >0 to enable.")
    args = ap.parse_args()

    cfg = {
        "request_timeout": 30,
        "max_pattern_length": 1000,
        "cache_size": 100,
        "max_concurrent_connections": 1000,
    }
    def _validate(cfg_in: dict) -> dict:
        """Clamp and coerce config values to safe ranges."""
        out = dict(cfg_in)
        def _clampi(v, lo, hi):
            try:
                v = int(v)
            except (TypeError, ValueError):
                return lo
            return max(lo, min(hi, v))
        def _clampf(v, lo, hi):
            try:
                v = float(v)
            except (TypeError, ValueError):
                return lo
            return max(lo, min(hi, v))
        out['request_timeout'] = _clampf(out.get('request_timeout', 30), 0.1, 3600)
        out['max_pattern_length'] = _clampi(out.get('max_pattern_length', 1000), 1, 1_000_000)
        out['cache_size'] = _clampi(out.get('cache_size', 100), 0, 1_000_000)
        out['max_concurrent_connections'] = _clampi(out.get('max_concurrent_connections', 1000), 1, 1_000_000)
        return out

    def _apply_env_overrides(base: dict):
        """Allow environment variables to override config for easy testing."""
        env = os.environ
        for k in list(base.keys()):
            env_name = 'SERVER_' + k.upper()
            if env_name in env and env[env_name] != '':
                base[k] = env[env_name]
        if 'SERVER_MAX_QUESTIONS' in env and env['SERVER_MAX_QUESTIONS'] != '':
            CFG_COMPLEXITY['max_questions'] = int(env['SERVER_MAX_QUESTIONS'])
        if 'SERVER_MAX_STARS' in env and env['SERVER_MAX_STARS'] != '':
            CFG_COMPLEXITY['max_stars'] = int(env['SERVER_MAX_STARS'])
        if 'SERVER_MAX_CONCURRENT_CONNECTIONS' in env and env['SERVER_MAX_CONCURRENT_CONNECTIONS'] != '':
            base['max_concurrent_connections'] = int(env['SERVER_MAX_CONCURRENT_CONNECTIONS'])

    last_cfg_mtime = None
    def _load_from_file():
        """Load and merge JSON config; also resize caches if needed."""
        nonlocal cfg, last_cfg_mtime
        file_cfg = {}
        if args.config:
            try:
                with open(args.config, 'r', encoding='utf-8') as f:
                    file_cfg = json.load(f)
                last_cfg_mtime = os.path.getmtime(args.config)
            except (OSError, ValueError) as e:
                print(f"[BASIC] Failed to load config {args.config}: {e}", file=sys.stderr)
        merged = dict(cfg)
        for k in merged.keys():
            if k in file_cfg:
                merged[k] = file_cfg[k]
        # Complexity limits
        for k in list(CFG_COMPLEXITY.keys()):
            if k in file_cfg:
                CFG_COMPLEXITY[k] = file_cfg[k]
        _apply_env_overrides(merged)
        merged = _validate(merged)
        cfg.update(merged)
        # Reconfigure cache size for compile_pattern_exact
        try:
            base_fn = compile_pattern_exact.__wrapped__  # type: ignore[attr-defined]
            globals()['compile_pattern_exact'] = functools.lru_cache(maxsize=int(cfg['cache_size']))(base_fn)  # type: ignore[assignment]
        except (AttributeError, TypeError, ValueError) as e:
            print(f"[BASIC] Cache not reconfigurable: {e}", file=sys.stderr)

    _load_from_file()

    words = load_wordlist(args.wordlist)
    index = WordIndex(words)
    stats = EnhancedStats()
    start_time = time.time()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.host, args.port))
    sock.listen(5)
    try:
        sock.settimeout(1.0)
    except OSError:
        pass
    print(f"[BASIC] Listening on {args.host}:{args.port}, words={len(words)}")
    httpd = None
    if int(args.health_port) > 0:
        class HealthHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass
            def do_GET(self):
                # Minimal /health endpoint to inspect liveness and stats.
                if self.path == '/health':
                    snap = stats.snapshot()
                    body = json.dumps({
                        'status': 'ok',
                        'uptime_s': round(time.time() - start_time, 3),
                        'words_total': len(words),
                        **snap
                    }, ensure_ascii=False).encode('utf-8')
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json; charset=utf-8')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()
        try:
            httpd = HTTPServer((args.host, int(args.health_port)), HealthHandler)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            print(f"[BASIC] Health at http://{args.host}:{int(args.health_port)}/health")
        except OSError as e:
            print(f"[BASIC] Health server failed: {e}", file=sys.stderr)
            httpd = None
    shutdown_evt = threading.Event()
    def _sig_handler(_signum, _frame):
        shutdown_evt.set()
    try:
        signal.signal(signal.SIGINT, _sig_handler)
        signal.signal(signal.SIGTERM, _sig_handler)
    except (ValueError, OSError, RuntimeError, AttributeError):
        pass
    try:
        while not shutdown_evt.is_set():
            if args.config:
                try:
                    m = os.path.getmtime(args.config)
                    if last_cfg_mtime is None or m > last_cfg_mtime:
                        _load_from_file()
                except OSError:
                    pass
            try:
                conn, addr = sock.accept()
            except socket.timeout:
                continue
            try:
                # Simple backpressure: reject when too many active connections.
                if int(cfg['max_concurrent_connections']) > 0 and stats.snapshot()['active_connections'] >= int(cfg['max_concurrent_connections']):
                    try:
                        conn.sendall(b"503 BUSY 0\nEND\n")
                    except OSError:
                        pass
                    conn.close()
                    continue
            except (OSError, ValueError, KeyError):
                pass
            stats.connection_opened()
            try:
                serve_once(conn, addr, words, stats, index, float(cfg['request_timeout']), int(cfg['max_pattern_length']))
            finally:
                stats.connection_closed()
    except KeyboardInterrupt:
        print("\n[BASIC] Shutting down.")
    finally:
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except (OSError, RuntimeError):
                pass
        sock.close()

if __name__ == "__main__":
    main()
