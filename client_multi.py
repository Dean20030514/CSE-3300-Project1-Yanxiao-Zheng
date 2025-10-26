"""
Interactive multi-query TCP client for the multi-threaded server.

This client keeps the TCP connection open and lets the user enter many
queries. Type 'quit' to end the session. It follows the assignment
protocol and also supports extended features exposed by the threaded server:

- Commands we send: "FIND <pattern>" and "QUIT".
- Status line from server includes a status code and a count of words.
- Body lists matching words, terminated by line 'END'.
- --mode exact|partial selects matching mode on server side.
- --gzip asks for gzip-compressed body; we auto-decode it if present.
- --range OFFSET LIMIT requests a page (slice) of results.

Wildcards:
- '?' matches exactly one character.
- In partial mode, the server treats the whole pattern as if wrapped by '*'
  (matches any substring), and '*' in the pattern can match any string.

All comments are simple English as required.
"""

import argparse
import socket
import base64
import gzip

def recv_until_end(sock: socket.socket) -> list[str]:
    """Read response until the 'END' line, decoding gzip if used.

    If the server sends a single line starting with 'GZIP ', we base64-decode
    and gunzip the payload to get the list of words.
    """
    lines: list[str] = []
    f = sock.makefile('rb', buffering=0)
    while True:
        line = f.readline()
        if not line:
            break
        sline = line.decode('utf-8', errors='ignore')
        if sline.strip() == "END":
            break
        lines.append(sline.rstrip('\r\n'))
    if len(lines) == 1 and lines[0].startswith('GZIP '):
        try:
            raw = base64.b64decode(lines[0][5:])
            data = gzip.decompress(raw).decode('utf-8')
            return data.split('\n') if data else []
        except (OSError, ValueError):
            return lines
    return lines

def main():
    """Open one TCP connection and serve user input until 'quit'."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--mode", choices=["exact", "partial"], help="Default mode.")
    ap.add_argument("--gzip", action="store_true", help="Ask for gzip response.")
    ap.add_argument("--range", nargs=2, metavar=("OFFSET", "LIMIT"), type=int, help="Page results.")
    args = ap.parse_args()

    print("(client) connecting...")
    with socket.create_connection((args.host, args.port)) as s:
        print("(client) connected. Type patterns (? is one-char wildcard). Type 'quit' to exit.")
        while True:
            try:
                q = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                q = "quit"
            if not q:
                continue
            if q.lower() == "quit":
                s.sendall(b"QUIT\n")
                break
            # Build optional flags for server: mode, range, gzip.
            suffix = ""
            if args.mode:
                suffix += f" --mode {args.mode}"
            if args.range is not None:
                off, lim = args.range
                suffix += f" RANGE {off} {lim}"
            if args.gzip:
                suffix += " --accept-encoding gzip"
            # Send a single FIND request and read both status and body.
            s.sendall(f"FIND {q}{suffix}\n".encode('utf-8'))
            f = s.makefile('rwb', buffering=0)
            status = f.readline().decode('utf-8', errors='ignore').rstrip('\r\n')
            body_lines = recv_until_end(s)
            print(status)
            toks = status.split()
            if len(toks) >= 3 and toks[0].isdigit():
                try:
                    count = int(toks[-1])
                    # Print the words the server returned (up to <count>), then END line.
                    for ln in body_lines[:count]:
                        print(ln)
                    print("END")
                    # Extra client-side count print as requested by assignment extension.
                    print(f"(client) total words: {count}")
                except ValueError:
                    pass
    print("(client) done.")

if __name__ == "__main__":
    main()
