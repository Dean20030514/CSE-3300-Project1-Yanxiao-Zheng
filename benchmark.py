import argparse
import concurrent.futures
import socket
import time
from statistics import mean


def send_cmd(host: str, port: int, cmd: str, timeout: float = 2.0):
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            f = s.makefile('rwb', buffering=0)
            f.write((cmd + "\n").encode('utf-8'))
            # drain quickly
            while True:
                line = f.readline()
                if not line:
                    break
                if line.strip() == b'END':
                    break
        ok = True
    except OSError:
        ok = False
    dt = (time.perf_counter() - t0)
    return ok, dt


def run_benchmark(host: str, port: int, cmd: str, concurrency: int, duration_s: float):
    latencies = []
    successes = 0
    total = 0
    stop_t = time.time() + duration_s
    def worker():
        nonlocal successes, total
        while time.time() < stop_t:
            ok, dt = send_cmd(host, port, cmd)
            total += 1
            if ok:
                successes += 1
                latencies.append(dt)
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = [ex.submit(worker) for _ in range(concurrency)]
        for f in futs:
            f.result()
    if latencies:
        p50 = sorted(latencies)[int(0.5*len(latencies))]
        p95 = sorted(latencies)[int(0.95*len(latencies))]
        p99 = sorted(latencies)[int(0.99*len(latencies))]
    else:
        p50 = p95 = p99 = 0.0
    qps = successes / duration_s if duration_s > 0 else 0.0
    return {
        'total_requests': total,
        'successes': successes,
        'qps': qps,
        'avg_latency_ms': (mean(latencies) * 1000.0) if latencies else 0.0,
        'p50_ms': p50 * 1000.0,
        'p95_ms': p95 * 1000.0,
        'p99_ms': p99 * 1000.0,
    }


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, required=True)
    ap.add_argument('--cmd', default='COUNT h?llo --mode exact')
    ap.add_argument('--concurrency', type=int, default=50)
    ap.add_argument('--duration', type=float, default=5.0)
    args = ap.parse_args()
    res = run_benchmark(args.host, args.port, args.cmd, args.concurrency, args.duration)
    print(res)
