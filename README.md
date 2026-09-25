# calculator-server

Network Applications coursework. Two parts, one repository, written in Python 3 using only the
standard library (`socket`, `struct`, `threading`): no frameworks, no `http.server`.

| Part | Folder | What it is |
|---|---|---|
| 1 | [part1-http11-calculator/](part1-http11-calculator/) | An HTTP/1.1 calculator that stays on the line: keep-alive, exact Content-Length framing, chunked bodies, pipelining, `Connection: close`, idle timeout. |
| 2 | [part2-binary-http/](part2-binary-http/) | HTTP in binary: the [BHTTP/1 spec](part2-binary-http/SPEC.md), `bserve` (file server), `bcurl` (client) and an [annotated hexdump](part2-binary-http/HEXDUMP.md). |

## Quick start

```sh
# Part 1
cd part1-http11-calculator
python server.py 8080 &
python check.py localhost 8080      # the slide's marking run: 6 responses, 1 handshake, socket still open
python -m unittest discover -s tests

# Part 2
cd ../part2-binary-http
./bserve ./www 9000 &
./bcurl -v localhost:9000/index.html
python -m unittest discover -s tests
```

On Windows PowerShell use `python bserve.py ./www 9000` and `.\bcurl.cmd -v localhost:9000/index.html`.
