# Part 2 — HTTP, in binary

A binary-framed HTTP ("BHTTP/1") with a file server and a client. The two programs share no code.
The only thing that crosses between them is [SPEC.md](SPEC.md).

| Deliverable | File |
|---|---|
| 1. The spec (two pages) | [SPEC.md](SPEC.md) |
| 2. The programs | [bserve.py](bserve.py) (Track 1, server), [bcurl.py](bcurl.py) (Track 2, client) |
| 3. Annotated hexdump of one full request + response | [HEXDUMP.md](HEXDUMP.md) |

## Run

Python 3.8+, standard library only. The `bserve` / `bcurl` wrappers run the `.py` files, so the
slide's commands work as written in Git Bash, Linux and macOS. In PowerShell or cmd use
`.\bserve.cmd` / `.\bcurl.cmd`, or `python bserve.py …`.

```sh
./bserve ./www 9000                          # Track 1
./bcurl -v localhost:9000/index.html         # Track 2: body to stdout, frame hexdumps to stderr
echo $?                                      # 0

./bcurl localhost:9000/nope; echo $?         # 404 body, exit 4
./bcurl localhost:9000/hello.txt /index.html /big.bin    # 3 requests pipelined on ONE connection
./bcurl -I localhost:9000/big.bin            # HEAD
./bcurl -d @www/hello.txt localhost:9000/x   # a request body as DATA frames (-> 405)
./bcurl -H 'connection: close' -v localhost:9000/   # server replies, then sends GOAWAY
```

### bserve

`bserve <root> <port> [--idle SECONDS] [--host ADDR] [--send-unknown] [-q]`

* Maps the path under `<root>`; a directory maps to its `index.html`. A path that escapes the root
  gets 404, and so does a missing file.
* A malformed payload gets **400 on the same stream, and the connection stays open**. Broken
  framing (a frame over 64 KiB, REQUEST on stream 0, …) gets GOAWAY, then close.
* GET and HEAD are supported; other methods get 405, after their DATA frames have been read and
  discarded.
* Files are sent as one RESPONSE frame and then DATA frames of at most 64 KiB each.
* Keeps the connection open. It closes on a client GOAWAY, on `connection: close`, or after 30 s
  idle, and it sends GOAWAY(IDLE_TIMEOUT) before an idle close.
* Unknown frame types are skipped by discarding `Length` octets, even ones larger than the
  frame limit.
* `--send-unknown` puts an unknown-type (0x7E) frame before every response. Use it to check that
  a partner's client obeys the MUST-skip rule.

### bcurl

`bcurl [-v] [-I | -X METHOD] [-H 'name: value']... [-d DATA|@file] [--dump-limit N] host:port/path [/path ...]`

* Opens **one** connection. Extra paths are pipelined on it, and a target on a different host is
  refused rather than opening a second connection.
* `-v` hexdumps every frame, sent (`>`) and received (`<`), with a decoded summary. It goes to
  stderr, so stdout stays exactly the body bytes.
* Checks stream IDs and response order, and checks that DATA octets add up to `content-length`.
  It skips unknown frame types.
* Exit status: **0** for 1xx–3xx, **4** if any response is 4xx, **5** if any is 5xx, **1** on a
  connection or protocol error, **2** on bad usage.

## Tests

```sh
python -m unittest discover -s tests         # 27 tests, ~5 s
```

`tests/test_bhttp.py` builds its frames **by hand from the spec**, so a pass shows each program
agrees with SPEC.md, not just with the other program. Highlights:

* unknown frame types are skipped, including a 100 000-octet one, then the next request is answered
* seven kinds of malformed REQUEST each get a 400, and the connection still serves request 8
* 5 requests pipelined in one `send` are answered in order; requests delivered byte by byte also work
* traversal attempts (`/../`, `..\`, `C:/…`) get 404
* HEAD, 405 with a 70 000-octet body consumed, `connection: close`, client GOAWAY, idle GOAWAY
* frame-level errors (oversized, stream 0, RESPONSE sent to the server) get GOAWAY with the right code
* bcurl: body bytes are exact, exit codes are right, **3 paths make 1 server connection**, it copes
  with `--send-unknown`, and it works against a hand-rolled server that sends a 70 000-octet unknown
  frame, a non-zero reserved octet, an unknown flag bit and reserved header indexes

## Pairing notes

To test interop with a partner's implementation, run `./bserve --send-unknown ./www 9000` against
their client, and `./bcurl -v their-host:9000/` against their server. The `-v` dump is annotated
the same way as HEXDUMP.md, so any disagreement can be traced to a specific octet and a specific
line of the spec.
