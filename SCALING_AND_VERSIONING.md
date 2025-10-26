# Scaling and Versioning Plan

## Async Server Idea

- Add `server_asyncio.py` using async I/O
- Same protocol (FIND, COUNT, STATS, QUIT)
- Reuse word index (read-only, safe)
- Add timeouts and clean shutdown

Why: Better for many clients, less memory.

## Scaling to Many Servers

- Run many servers behind load balancer
- Start with full copy of data
- Later, split data by key if needed
- Add health checks and logging
- Config from file + environment

## Protocol Versioning

- Optional `HELLO` with version number
- `CAPA` shows supported features
- Keep old clients working
- New features are optional

## Benchmark

- Use `benchmark.py` to test speed
- Compare requests per second
- Measure response times

## Next Steps

1. Build async server
2. Add version handshake
3. Test load balancing
