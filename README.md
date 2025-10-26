# Word Search Server (Project 1)

## Files

- `server_basic.py` - Single client server, exact matching
- `client_basic.py` - Single query client
- `server_threaded.py` - Multi-client server, exact or partial matching
- `client_multi.py` - Multi-query client
- `PROTOCOL_AND_DESIGN.md` - Design document

## Quick Start

1. Put `wordlist.txt` in same folder
2. Start basic server:

   ```powershell
   python server_basic.py --host 127.0.0.1 --port 8080 --wordlist wordlist.txt
   ```

   Then run client:

   ```powershell
   python client_basic.py --host 127.0.0.1 --port 8080 --query "a?t"
   ```

3. Start threaded server:

   ```powershell
   python server_threaded.py --host 127.0.0.1 --port 8081 --wordlist wordlist.txt --mode partial
   ```

   Then run:

   ```powershell
   python client_multi.py --host 127.0.0.1 --port 8081
   ```

## Extra Features

- **Multiple patterns**: `FIND_MULTI h?llo world`
- **Paging**: `FIND pattern RANGE 0 10`
- **Compression**: `FIND pattern --accept-encoding gzip`
- **Stats**: `STATS` command
- **Safety**: Limits on wildcards and pattern size

## Testing

Run all tests:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

## Notes

- Protocol uses lines and ends with `END`
- Use `python` (on some systems `python3`)
- Use different ports if 8080/8081 are busy
