"""
Threaded TCP word search server (exact and partial modes).

- Role: serve many clients concurrently; allow multiple requests per connection.
- Request: `FIND <pattern> [--range <start> <end>] [--gzip] [--mode exact|partial]` |
    `COUNT <pattern> [--mode exact|partial]` | `QUIT`.
- Response: first line `<code> <text> <count>`, optional body, final `END` line.
- Modes: exact = whole-word match; partial = substring match. `?` = one char.
- Paging & gzip: client flags `--range` and `--gzip` (translated to `RANGE` and
    `--accept-encoding gzip`) reduce output size / bytes over the wire.
- Errors: `400 BAD-REQUEST`, `404 NOT-FOUND`, `503 BUSY`. Every response ends with `END`.

Kept intentionally simple and observable (stats, limits) for grading and tuning.
"""

import argparse
import json
import re
import socket
import sys
import threading
import functools
from typing import List
from index import WordIndex, _compile_regex_body
from concurrent.futures import ThreadPoolExecutor
import time
import os
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
LATENCY_BUCKETS_MS = [1, 5, 10, 50, 100, 500, 1000]
import gzip
import base64
from collections import defaultdict

@functools.lru_cache(maxsize=100)
def compile_pattern(pattern: str, mode: str) -> re.Pattern:
    """Compile a regex for the given pattern and mode.

    - Escape regex meta characters to treat them as plain text.
    - Replace '?' with '.' and '*' with '.*' (when present in the pattern).
    - 'exact' mode anchors the pattern to the whole word; 'partial' adds '.*'
      around the body to match substrings.
    - Case-insensitive.
    """
    esc = []
    for ch in pattern:
        if ch in ".^$+{}[]|()\\":
            esc.append("\\" + ch)
        elif ch == '?':
            esc.append('.')
        elif ch == '*':
            esc.append('.*')
        else:
            esc.append(ch)
    body = ''.join(esc)
    if mode == 'exact':
        regex = '^' + body + '$'
    else:
        regex = '.*' + body + '.*'
    return re.compile(regex, re.IGNORECASE)

def load_wordlist(path: str) -> List[str]:
    """Read words from file; ignore empty lines and decoding errors."""
    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
        return [line.strip() for line in f if line.strip()]

class Stats:
    """Thread-safe counters and latency histogram for observability."""
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
        self.latency_hist = {f"lt{b}": 0 for b in LATENCY_BUCKETS_MS}
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
            placed = False
            for b in LATENCY_BUCKETS_MS:
                if ms < b:
                    key = f"lt{b}"
                    self.latency_hist[key] += 1
                    placed = True
                    break
            if not placed:
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


class EnhancedStats(Stats):
    """Add pattern-complexity histograms and cache metrics to Stats."""
    def __init__(self):
        super().__init__()
        self.pattern_complexity_hist = defaultdict(int)
        self.cache_hit_rates = {}

    def snapshot(self):
        base = super().snapshot()
        # attach shallow copies to avoid mutation during iteration
        base['pattern_complexity_hist'] = dict(self.pattern_complexity_hist)
        base['cache_hit_rates'] = dict(self.cache_hit_rates)
        return base

def json_log(event: str, level: str = "info", **fields):
    """Best-effort JSON log to stderr for debugging/benchmarking."""
    try:
        rec = {"ts": time.time(), "level": level, "event": event}
        rec.update(fields)
        print(json.dumps(rec, ensure_ascii=False), file=sys.stderr)
    except (OSError, ValueError, TypeError):
        pass

class Worker(threading.Thread):
    """Legacy helper thread wrapper (not used by the main loop now).

    We keep it here as a simple adapter, but the main loop uses ThreadPoolExecutor
    and directly calls handle_connection.
    """
    def __init__(self, conn: socket.socket, addr, words: List[str], mode: str, stats: 'Stats', index: WordIndex):
        super().__init__(daemon=True)
        self.conn = conn
        self.addr = addr
        self.words = words
        self.mode = mode
        self.stats = stats
        self.index = index

    def send(self, s: str):
        self.conn.sendall(s.encode('utf-8'))

    def handle_find(self, pattern: str, mode: str) -> List[str]:
        rx = compile_pattern(pattern, mode)
        if mode == 'exact':
            return [w for w in self.words if rx.fullmatch(w) is not None]
        else:
            return [w for w in self.words if rx.search(w) is not None]

    def run(self):
        handle_connection(self.conn, self.addr, self.words, self.mode, self.stats, self.index)

UNDER_MEMORY_PRESSURE = False


def _memory_pressure_handler(stats: Stats | None = None, soft_limit_mb: int | None = None) -> bool:
    """When memory is high, clear regex caches to reduce memory footprint."""
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
                compile_pattern.cache_clear()
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
    """Return wildcard limits, halved under memory pressure."""
    if UNDER_MEMORY_PRESSURE:
        return {
            "max_questions": max(1, int(CFG_COMPLEXITY["max_questions"]) // 2),
            "max_stars": max(1, int(CFG_COMPLEXITY["max_stars"]) // 2),
        }
    return CFG_COMPLEXITY


def handle_connection(conn: socket.socket, addr, words: List[str], default_mode: str, stats: 'Stats',
                      index: WordIndex,
                      request_timeout: float = 30.0, max_pattern_length: int = 1000):
    """Serve one client; loop for multiple requests until QUIT/EOF.

    Input: one-line commands from the socket. Output: status, optional body, `END`.
    Branches: `COUNT` returns only the number; `FIND` returns words (exact/partial).
    Options: `--mode` picks matching mode; `RANGE off lim` and gzip negotiation supported.
    """
    def get_memory_rss_bytes():
        """Return current process RSS in bytes if psutil is available."""
        try:
            if psutil is not None:
                return psutil.Process().memory_info().rss
        except (OSError, AttributeError, RuntimeError):
            pass
        return None

    def is_pattern_too_complex(pat: str, max_q: int, max_s: int):
        """Enforce simple limits on '?' and '*' counts to cap regex work."""
        q = pat.count('?')
        s = pat.count('*')
        if q > max_q:
            return True, f"too many '?' wildcards (> {max_q})"
        if s > max_s:
            return True, f"too many '*' wildcards (> {max_s})"
        return False, ''

    def send(s: str):
        conn.sendall(s.encode('utf-8'))

    try:
        with conn:
            conn.settimeout(float(request_timeout))
            f = conn.makefile('rwb', buffering=0)
            stats.connection_opened()
            while True:
                try:
                    raw = f.readline()
                except socket.timeout:
                    send("400 BAD-REQUEST timeout\nEND\n")
                    stats.inc('bad_request_responses')
                    break
                if not raw:
                    break
                if len(raw) > max_pattern_length:
                    send("400 BAD-REQUEST pattern too long\nEND\n")
                    stats.inc('bad_request_responses')
                    continue
                try:
                    line = raw.decode('utf-8').rstrip('\r\n')
                except UnicodeDecodeError:
                    send("400 BAD-REQUEST non-utf8\nEND\n")
                    stats.inc('bad_request_responses')
                    continue
                if not line:
                    continue
                stats.inc('requests')
                t0 = time.perf_counter()
                parts = line.split(' ', 1)
                request_id = uuid.uuid4().hex
                cmd = parts[0].upper()
                if cmd == 'QUIT':
                    break
                if cmd == 'STATS':
                    stats.inc('stats_requests')
                    try:
                        if isinstance(stats, EnhancedStats):
                            try:
                                ci = compile_pattern.cache_info()
                                stats.cache_hit_rates['compile_pattern'] = getattr(ci, '_asdict', lambda: {
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
                    send(f"200 OK {len(lines)}\n")
                    for s in lines:
                        send(s + "\n")
                    send("END\n")
                    dt = (time.perf_counter() - t0) * 1000
                    stats.record_request_time(dt)
                    json_log("stats", count=len(lines), latency_ms=dt, request_id=request_id, remote=str(addr))
                    continue

                if cmd not in ('FIND', 'FIND_MULTI', 'COUNT', 'BATCH') or len(parts) != 2 or not parts[1]:
                    send("400 BAD-REQUEST expected 'FIND <pattern>' or 'COUNT <pattern>' or 'STATS'\nEND\n")
                    stats.inc('bad_request_responses')
                    dt = (time.perf_counter() - t0) * 1000
                    stats.record_request_time(dt)
                    json_log("bad_request", reason="syntax", cmd=cmd, latency_ms=dt, request_id=request_id, remote=str(addr))
                    continue

                rest = parts[1]
                req_mode = default_mode
                request_gzip = False
                offset = 0
                limit = None

                if ' --accept-encoding ' in rest:
                    # Optional gzip negotiation: server sends compressed body if asked.
                    rest, enc_part = rest.rsplit(' --accept-encoding ', 1)
                    if enc_part.strip().lower() != 'gzip':
                        send("400 BAD-REQUEST invalid encoding\nEND\n")
                        stats.inc('bad_request_responses')
                        dt = (time.perf_counter() - t0) * 1000
                        stats.record_request_time(dt)
                        json_log("bad_request", reason="missing_pattern", cmd=cmd, latency_ms=dt, request_id=request_id, remote=str(addr))
                        continue
                    request_gzip = True

                if ' RANGE ' in rest:
                    # Optional pagination support to limit bytes per response.
                    rest, range_part = rest.rsplit(' RANGE ', 1)
                    rng_tokens = range_part.strip().split()
                    if len(rng_tokens) != 2 or not all(t.isdigit() for t in rng_tokens):
                        send("400 BAD-REQUEST invalid RANGE\nEND\n")
                        stats.inc('bad_request_responses')
                        dt = (time.perf_counter() - t0) * 1000
                        stats.record_request_time(dt)
                        json_log("bad_request", reason="invalid_mode", cmd=cmd, latency_ms=dt, request_id=request_id, remote=str(addr))
                        continue
                    offset = int(rng_tokens[0])
                    limit = int(rng_tokens[1])

                pattern = rest
                if ' --mode ' in rest:
                    # Allow client to pick exact or partial mode per request.
                    pattern_part, mode_part = rest.rsplit(' --mode ', 1)
                    if not pattern_part:
                        send("400 BAD-REQUEST expected 'FIND <pattern>'\nEND\n")
                        stats.inc('bad_request_responses')
                        stats.record_request_time((time.perf_counter() - t0) * 1000)
                        continue
                    mv = mode_part.strip().lower()
                    if mv not in ('exact', 'partial'):
                        send("400 BAD-REQUEST invalid mode\nEND\n")
                        stats.inc('bad_request_responses')
                        stats.record_request_time((time.perf_counter() - t0) * 1000)
                        continue
                    req_mode = mv
                    pattern = pattern_part

                _memory_pressure_handler(stats)
                if len(pattern) > max_pattern_length:
                    send("400 BAD-REQUEST pattern too long\nEND\n")
                    stats.inc('bad_request_responses')
                    dt = (time.perf_counter() - t0) * 1000
                    stats.record_request_time(dt)
                    json_log("bad_request", reason="pattern_too_long", cmd=cmd, latency_ms=dt, request_id=request_id, remote=str(addr))
                    continue

                limits = _effective_complexity_limits()
                too_complex, reason = is_pattern_too_complex(
                    pattern,
                    int(limits['max_questions']),
                    int(limits['max_stars'])
                )
                if too_complex:
                    send(f"400 BAD-REQUEST pattern too complex: {reason}\nEND\n")
                    stats.inc('bad_request_responses')
                    dt = (time.perf_counter() - t0) * 1000
                    stats.record_request_time(dt)
                    json_log("bad_request", reason="pattern_too_complex", cmd=cmd, latency_ms=dt, request_id=request_id, remote=str(addr))
                    continue

                try:
                    if isinstance(stats, EnhancedStats):
                        q = pattern.count('?')
                        s = pattern.count('*')
                        stats.pattern_complexity_hist[f"q_{q}"] += 1
                        stats.pattern_complexity_hist[f"s_{s}"] += 1
                except Exception:
                    pass

                if cmd == 'BATCH':
                    # Expect JSON array of patterns; return COUNT lines for each.
                    try:
                        pats = json.loads(pattern)
                        if not isinstance(pats, list) or not all(isinstance(x, str) for x in pats):
                            raise ValueError("batch expects JSON array of strings")
                    except Exception:
                        send("400 BAD-REQUEST invalid batch payload\nEND\n")
                        stats.inc('bad_request_responses')
                        dt = (time.perf_counter() - t0) * 1000
                        stats.record_request_time(dt)
                        json_log("bad_request", reason="invalid_batch", cmd=cmd, latency_ms=dt, request_id=request_id, remote=str(addr))
                        continue
                    counts: List[int] = []
                    for p in pats:
                        limits = _effective_complexity_limits()
                        tc, _ = is_pattern_too_complex(p, int(limits['max_questions']), int(limits['max_stars']))
                        if tc:
                            counts.append(0)
                            continue
                        if req_mode == 'exact':
                            counts.append(index.count_exact(p))
                        else:
                            counts.append(index.count_partial(p))
                    send(f"200 OK {len(counts)}\n")
                    for i, c in enumerate(counts):
                        send(f"COUNT {i} {c}\n")
                    send("END\n")
                    dt = (time.perf_counter() - t0) * 1000
                    stats.record_request_time(dt)
                    json_log("batch", n=len(counts), mode=req_mode, latency_ms=dt, request_id=request_id, remote=str(addr))
                    continue

                if cmd == 'COUNT':
                    # Only return the count (no list of words in the body).
                    stats.inc('count_requests')
                    if req_mode == 'exact':
                        count = index.count_exact(pattern)
                    else:
                        count = index.count_partial(pattern)
                    if count == 0:
                        send("404 NOT-FOUND 0\nEND\n")
                        stats.inc('not_found_responses')
                    else:
                        send(f"200 OK {count}\nEND\n")
                        stats.inc('ok_responses')
                    dt = (time.perf_counter() - t0) * 1000
                    stats.record_request_time(dt)
                    json_log("count", mode=req_mode, pattern=pattern, count=count, latency_ms=dt, request_id=request_id, remote=str(addr))
                    continue

                stats.inc('find_requests')
                if cmd == 'FIND_MULTI':
                    # Merge matches of multiple patterns and de-duplicate words.
                    tokens = pattern.strip().split()
                    if not tokens:
                        send("400 BAD-REQUEST expected 'FIND_MULTI <pattern1> <pattern2> ...'\nEND\n")
                        stats.inc('bad_request_responses')
                        stats.record_request_time((time.perf_counter() - t0) * 1000)
                        continue
                    seen = set()
                    matches_list = []
                    for pat in tokens:
                        if req_mode == 'exact':
                            ms = index.find_exact(pat)
                        else:
                            ms = index.find_partial(pat)
                        for w in ms:
                            if w not in seen:
                                seen.add(w)
                                matches_list.append(w)
                    matches = matches_list
                else:
                    if req_mode == 'exact':
                        matches = index.find_exact(pattern)
                    else:
                        matches = index.find_partial(pattern)

                if limit is not None:
                    if offset < 0:
                        offset = 0
                    if limit < 0:
                        limit = 0
                    matches = matches[offset: offset + limit]
                count = len(matches)
                print(f"[THREADED:{req_mode}] {addr} FIND {pattern!r} -> {count} matches (rid={request_id})", file=sys.stderr)
                if count == 0:
                    send("404 NOT-FOUND 0\nEND\n")
                    stats.inc('not_found_responses')
                else:
                    if request_gzip:
                        # Send compressed body in one line to reduce transfer size.
                        payload = '\n'.join(matches).encode('utf-8')
                        gz = gzip.compress(payload)
                        b64 = base64.b64encode(gz).decode('ascii')
                        send("200 OK 1\n")
                        send("GZIP " + b64 + "\n")
                        send("END\n")
                    else:
                        send(f"200 OK {count}\n")
                        for w in matches:
                            send(w + "\n")
                        send("END\n")
                    stats.inc('ok_responses')
                dt = (time.perf_counter() - t0) * 1000
                stats.record_request_time(dt)
                json_log("find", cmd=cmd, mode=req_mode, pattern=pattern, count=count, latency_ms=dt, gzip=request_gzip, offset=offset, limit=limit, request_id=request_id, remote=str(addr))
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as e:
        print(f"[THREADED] Connection error for {addr}: {e}", file=sys.stderr)
    except OSError as e:
        print(f"[THREADED] OS error for {addr}: {e}", file=sys.stderr)
    finally:
        stats.connection_closed()

def main():
    """Parse flags, load word list, and serve concurrently with a thread pool."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--wordlist", required=True)
    ap.add_argument("--mode", choices=["exact", "partial"], default="partial",
                    help="Mode: exact or partial. Default: partial.")
    ap.add_argument("--config", help="Path to JSON config.")
    ap.add_argument("--health-port", type=int, default=0, help="HTTP health port. Use >0 to enable.")
    args = ap.parse_args()

    cfg = {
        "max_workers": 50,
        "request_timeout": 30,
        "max_pattern_length": 1000,
        "cache_size": 100,
        "max_concurrent_connections": 1000,
    }
    def _validate(cfg_in: dict) -> dict:
        """Clamp config values to safe ranges to avoid misuse."""
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
        out['max_workers'] = _clampi(out.get('max_workers', 50), 1, 10_000)
        out['max_concurrent_connections'] = _clampi(out.get('max_concurrent_connections', 1000), 1, 1_000_000)
        return out

    def _apply_env_overrides(base: dict):
        """Allow environment variables to override config values."""
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
        """Load JSON config from disk and apply; also resize caches."""
        nonlocal cfg, last_cfg_mtime
        file_cfg = {}
        if args.config:
            try:
                with open(args.config, 'r', encoding='utf-8') as f:
                    file_cfg = json.load(f)
                last_cfg_mtime = os.path.getmtime(args.config)
            except (OSError, ValueError) as e:
                print(f"[THREADED] Failed to load config {args.config}: {e}", file=sys.stderr)
        merged = dict(cfg)
        for k in merged.keys():
            if k in file_cfg:
                merged[k] = file_cfg[k]
        for k in list(CFG_COMPLEXITY.keys()):
            if k in file_cfg:
                CFG_COMPLEXITY[k] = file_cfg[k]
        _apply_env_overrides(merged)
        merged = _validate(merged)
        cfg.update(merged)
        try:
            base_fn = compile_pattern.__wrapped__
            globals()['compile_pattern'] = functools.lru_cache(maxsize=int(cfg['cache_size']))(base_fn)
        except (AttributeError, TypeError, ValueError) as e:
            print(f"[THREADED] Cache not reconfigurable: {e}", file=sys.stderr)

    _load_from_file()

    words = load_wordlist(args.wordlist)
    index = WordIndex(words)
    stats = EnhancedStats()
    start_time = time.time()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.host, args.port))
    sock.listen(20)
    sock.settimeout(1.0)
    print(f"[THREADED:{args.mode}] Listening on {args.host}:{args.port}, words={len(words)}")
    httpd = None
    if int(args.health_port) > 0:
        class HealthHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass
            def do_GET(self):
                # Minimal /health endpoint to check liveness and stats.
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
            print(f"[THREADED] Health at http://{args.host}:{int(args.health_port)}/health")
        except OSError as e:
            print(f"[THREADED] Health server failed: {e}", file=sys.stderr)
            httpd = None
    shutdown_evt = threading.Event()
    def _sig_handler(_signum, _frame):
        shutdown_evt.set()
    try:
        signal.signal(signal.SIGINT, _sig_handler)
        signal.signal(signal.SIGTERM, _sig_handler)
    except (ValueError, OSError, RuntimeError, AttributeError):
        pass
    executor = ThreadPoolExecutor(max_workers=int(cfg['max_workers']))
    try:
        while not shutdown_evt.is_set():
            if args.config:
                try:
                    mtime = os.path.getmtime(args.config)
                    if last_cfg_mtime is None or mtime > last_cfg_mtime:
                        _load_from_file()
                except OSError:
                    pass
            try:
                conn, addr = sock.accept()
            except socket.timeout:
                continue
            try:
                # Backpressure: drop new connection when too many are active.
                if int(cfg['max_concurrent_connections']) > 0 and stats.snapshot()['active_connections'] >= int(cfg['max_concurrent_connections']):
                    try:
                        conn.sendall(b"503 BUSY 0\nEND\n")
                    except OSError:
                        pass
                    conn.close()
                    continue
            except (OSError, ValueError, KeyError):
                pass
            executor.submit(handle_connection, conn, addr, words, args.mode, stats, index,
                            float(cfg['request_timeout']), int(cfg['max_pattern_length']))
    except KeyboardInterrupt:
        print("\n[THREADED] Shutting down.")
    finally:
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except (OSError, RuntimeError):
                pass
        executor.shutdown(wait=True)
        sock.close()

if __name__ == "__main__":
    main()
