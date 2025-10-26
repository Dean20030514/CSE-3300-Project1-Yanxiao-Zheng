# PROTOCOL_AND_DESIGN

## Description

- A TCP text service that searches a static word list using a simple, line-based protocol.
- Two servers are provided:
	- `server_basic.py`: single-thread, exact matching only; serves one request per connection.
	- `server_threaded.py`: multi-threaded, accepts multiple requests per connection; supports exact and partial modes.
- Protocol (client-facing) requests:
	- `FIND <pattern> [--range <start> <end>] [--gzip] [--mode exact|partial]`
	- `COUNT <pattern> [--mode exact|partial]`
	- Every response: status line `<code> <text> <count>` + zero/more lines + `END`.
- Wildcards: `?` matches exactly one character; in partial mode the pattern matches substrings.

## Trade-offs

- Use linear scan over an in-memory list plus lightweight indexing helpers (see `index.py`).
	- Pros: simple to implement, predictable behavior, low engineering risk for the assignment size.
	- Cons: not optimal for very large datasets; no persistence or advanced ranking.
- Concurrency via threads (thread pool in the threaded server).
	- Pros: straightforward; good enough for I/O-bound requests at class scale.
	- Cons: context-switch overhead; Python GIL limits CPU-bound scaling.

## Extensions (future work ideas)

- Pattern features: `*` multi-char wildcard, character classes, escape sequences.
- Caching: LRU for hot patterns; negative-cache for obvious misses.
- Health/Stats: richer `/health` HTTP or a `STATS` protocol extension for observability.
- Indexing: prefix/suffix maps, trigram index, or a compact automaton for faster search.
- Robustness: rate limits, per-connection timeouts, backpressure, graceful shutdown hooks.

## Test cases

- Core happy paths:
	- `FIND a?t` (exact) returns expected matches and ends with `END`.
	- `COUNT a?t` returns the same count as `FIND` for the same pattern.
- Large output handling:
	- `FIND ?????? --range 0 50` returns a bounded page and still ends with `END`.
	- `FIND ell --gzip` compresses the body (client decodes `GZIP <base64>`).
- Concurrency/integration:
	- Threaded server handles many simultaneous `COUNT` requests without errors.

Screenshots: see `screens/` for the four required cases and passing tests.

Known boundaries:

- Very long patterns and excessive wildcards are rejected with `400 BAD-REQUEST`.
- Counts may differ by mode (`exact` vs `partial`); clients should pick one explicitly.
