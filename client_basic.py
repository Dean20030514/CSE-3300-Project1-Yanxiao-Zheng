"""
Basic, single-query TCP client for the word search server.

This client connects to the basic (single-threaded) server and sends exactly
one pattern query, then prints the server response and exits. It follows the
assignment protocol requirements:

- Client request contains a command: we send "FIND <pattern>".
- Server response contains a status code and the number of matching words.
- Body lists the matching words, followed by a line with just "END".

Options supported (simple extensions for testing):
- --gzip: ask the server to gzip-compress the body (we auto-decode it).
- --range OFFSET LIMIT: request a page (slice) of the matches.

Notes on wildcards:
- '?' matches exactly one character (exact mode is used by the basic server).

All comments are in simple English as required.
"""

import argparse
import socket
import base64
import gzip

def recv_until_end(s: socket.socket) -> list[str]:
    """Read response lines from the socket until an 'END' line.

    The server always terminates the response body with a line 'END'.
    If the body is gzip-compressed, the server sends a single line
    starting with 'GZIP ' followed by Base64 data.
    We detect that case and return the decompressed list of words.
    """
    lines: list[str] = []
    f = s.makefile('rb', buffering=0)
    while True:
        line = f.readline()
        if not line:
            break
        try:
            sline = line.decode('utf-8')
        except UnicodeDecodeError:
            sline = ''
        if sline.strip() == "END":
            break
        lines.append(sline.rstrip('\r\n'))
    if len(lines) == 1 and lines[0].startswith('GZIP '):
        b64 = lines[0][5:]
        try:
            raw = base64.b64decode(b64)
            data = gzip.decompress(raw).decode('utf-8')
            return data.split('\n') if data else []
        except (OSError, ValueError):
            return lines
    return lines

def main():
    """Parse CLI args, send one FIND query, print the response, and exit."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--query", required=True, help="Pattern. Use ? for one character.")
    ap.add_argument("--gzip", action="store_true", help="Ask for gzip response.")
    ap.add_argument("--range", nargs=2, metavar=("OFFSET", "LIMIT"), type=int, help="Page results.")
    args = ap.parse_args()

    with socket.create_connection((args.host, args.port)) as s:
        # Build optional suffix for paging and compression negotiation.
        suffix = ""
        if args.range is not None:
            off, lim = args.range
            suffix += f" RANGE {off} {lim}"
        if args.gzip:
            suffix += " --accept-encoding gzip"
        # Send the request line: command + space + pattern + optional suffix.
        req = f"FIND {args.query}{suffix}\n".encode('utf-8')
        s.sendall(req)
        f = s.makefile('rwb', buffering=0)
        # Read the status line: e.g., "200 OK <count>" or "404 NOT-FOUND 0".
        status = f.readline().decode('utf-8').rstrip('\r\n')
        # Read body lines (may be empty) until 'END'.
        lines = recv_until_end(s)

    print(status)
    toks = status.split()
    if len(toks) >= 3 and toks[0].isdigit():
        try:
            count = int(toks[-1])
            # Print at most <count> lines (the server promises to send exactly that many).
            if lines:
                for ln in lines[:count]:
                    print(ln)
            print("END")
            # Extra client-side count print, per assignment extension.
            print(f"(client) total words: {count}")
        except ValueError:
            pass

if __name__ == "__main__":
    main()
