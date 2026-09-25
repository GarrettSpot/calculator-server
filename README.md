# calculator-server

Network Applications coursework. Two parts, one repository, written in Python 3 using only the
standard library (`socket`, `struct`, `threading`): no frameworks, no `http.server`.

| Part | Folder | What it is |
|---|---|---|
| 1 | [part1-http11-calculator/](part1-http11-calculator/) | An HTTP/1.1 calculator that stays on the line: keep-alive, exact Content-Length framing, chunked bodies, pipelining, `Connection: close`, idle timeout. |
| 2 | [part2-binary-http/](part2-binary-http/) | HTTP in binary: the [BHTTP/1 spec](part2-binary-http/SPEC.md), `bserve` (file server), `bcurl` (client) and an [annotated hexdump](part2-binary-http/HEXDUMP.md). |

## Requirements

Python 3.8 or newer. Nothing needs installing: there is no `pip install` and no virtualenv.
Check your version with `python3 --version` (macOS/Linux) or `python --version` (Windows).

## Quick start — macOS and Linux

On macOS and most Linux distributions the interpreter is called `python3`; plain `python` may
not exist or may be Python 2. Run everything from the repository root, in a terminal:

```sh
# Part 1: the calculator
cd part1-http11-calculator
python3 server.py 8080 &                # start the server in the background
python3 check.py localhost 8080         # the slide's marking run: 6 responses, 1 handshake, socket still open
python3 -m unittest discover -s tests   # 16 tests
kill %1                                 # stop the background server

# Part 2: binary HTTP
cd ../part2-binary-http
chmod +x bserve bcurl                   # only needed once, if the files lost their execute bit (e.g. from a .zip)
./bserve ./www 9000 &                   # Track 1: the server
./bcurl -v localhost:9000/index.html    # Track 2: body to stdout, frame hexdumps to stderr
echo $?                                 # 0 on success; 4 on a 4xx, 5 on a 5xx
python3 -m unittest discover -s tests   # 27 tests
kill %1
```

The `./bserve` and `./bcurl` wrappers pick `python3` automatically. You can also call the scripts
directly: `python3 bserve.py ./www 9000` and `python3 bcurl.py -v localhost:9000/index.html`.

Notes:
* **macOS firewall**: the first time a server starts, macOS may ask whether Python may accept
  incoming connections. Local tests work either way; allow it if a partner connects from another
  machine. To stay local-only, pass `--host 127.0.0.1` to either server.
* **"Address already in use"**: something is already on that port. Find it with `lsof -i :9000`,
  or choose another port (`./bserve ./www 9001`, then `./bcurl localhost:9001/`).
* To keep the server logs in a separate window, run the server in one terminal *without* the
  `&`, run the client in a second terminal, and stop the server with **Ctrl+C**.

## Quick start — Windows

**Git Bash**: the commands above work as written, but use `python` instead of `python3`.

**PowerShell / cmd**:

```powershell
# Part 1
cd part1-http11-calculator
python server.py 8080                   # leave this running; open a second terminal for the rest
python check.py localhost 8080
python -m unittest discover -s tests

# Part 2
cd ..\part2-binary-http
python bserve.py ./www 9000             # leave running (Ctrl+C to stop)
.\bcurl.cmd -v localhost:9000/index.html
echo $LASTEXITCODE                      # 0 on success; 4 on a 4xx, 5 on a 5xx
python -m unittest discover -s tests
```
