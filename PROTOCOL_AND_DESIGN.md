# Project 1: Word Search Server

## How It Works

We made a TCP client-server app to search words from a list. You can search with wildcards.

Two server types:

- **Basic server** (`server_basic.py`): Handles one client at a time. Uses exact matching - pattern must match whole word.
- **Threaded server** (`server_threaded.py`): Handles many clients at once. Uses partial matching by default - pattern can match any part of word.

Both servers load the word list at start. Clients connect and send text commands.

## Protocol

**Client sends:**

```text
FIND <pattern>
QUIT
```

**Server replies:**

```text
<code> <text> <count>
<word1>
<word2>
...
END
```

- Codes: `200 OK` (found), `404 NOT-FOUND` (none), `400 BAD-REQUEST` (error)
- Server prints the count to console for grading

## Design Choices

- **Simple protocol**: Easy to read and write. Uses status codes like HTTP.
- **Threads**: Each client gets a thread. Good for small number of clients.
- **Regex matching**: Fast enough for our word list. For bigger lists, we could use indexes.

## Extra Features

- **Timeout**: Close dead connections
- **Paging**: Get parts of big results
- **Compression**: Send less data over network
- **Auth**: Add login if needed
- **Async**: Better for many clients

## Testing

Run threaded server in partial mode:

```powershell
python server_threaded.py --host 127.0.0.1 --port 8081 --wordlist wordlist.txt --mode partial
python client_multi.py --host 127.0.0.1 --port 8081
```

Test queries and expected counts:

- `??????????` → 24071
- `?` → 69903
- `?(a)` → 414
- `-?-` → 13

Take screenshots of client and server windows.

## Code Sources

- Socket code patterns from Python docs
- Threading from sample code
- No copied code from other students
