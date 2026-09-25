# Part 1 — a calculator that stays on the line

An HTTP/1.1 server written against a bare socket (`socket`, `threading`, no `http.server`).
It serves four operations and **keeps the TCP connection open** between requests.

```
python server.py 8080            # or: python server.py 8080 --idle 15 --host 127.0.0.1 -q
python check.py localhost 8080   # the marking script from the slide, plus the stretch goals
python -m unittest discover -s tests
```

## Behaviour

| Request | Status | Body |
|---|---|---|
| `GET /add?a=2&b=3` | 200 | `5` |
| `GET /sub?a=10&b=4` | 200 | `6` |
| `GET /mul?a=6&b=7` | 200 | `42` |
| `GET /div?a=9&b=3` | 200 | `3` (whole results print as integers, else `0.25`) |
| `GET /div?a=1&b=0` | 400 | `division by zero` |
| `GET /add?a=x&b=3` (also missing, repeated, `inf`, `nan`) | 400 | reason |
| `GET /pow?a=2&b=8` | 404 | unknown operation |
| `POST /add` (any non-GET/HEAD on a known path) | 405 | + `Allow: GET, HEAD` |
| `GET /add` with **no Host** on HTTP/1.1 | 400 | connection kept open |
| unparseable request line / header, TE+CL together, bad chunk | 400 | then **close** |
| head > 8 KiB / body > 1 MiB | 431 / 413 | then close |
| `HTTP/2.0` in a request line | 505 | then close |

Every response carries `Content-Length`, `Content-Type: text/plain; charset=utf-8`,
`Date`, and `Connection: keep-alive` (with `Keep-Alive: timeout=15`) or `Connection: close`.

## Where does one request end and the next begin?

Each connection holds one byte buffer. A reader never takes more from it than it returns:

1. **Head**: everything up to the first `CRLF CRLF`. Up to one leading blank line is ignored (RFC 9112 §2.2).
2. **Body**, chosen by the headers:
   * `Transfer-Encoding: chunked` → read `size[;ext] CRLF`, then *size* bytes, then `CRLF`, repeated
     until a `0` chunk; then trailer lines until an empty line.
   * `Content-Length: N` → exactly **N** bytes. Byte N+1 belongs to the next request.
   * neither → no body.
   * both → rejected with 400 and the connection is closed. This is the request-smuggling shape:
     two parsers could disagree about where the body ends.
3. Whatever is still in the buffer is the start of the next request.

The body of a request we refuse (e.g. `POST /add` → 405) is **still read and discarded**.
If it were skipped, its bytes would be parsed as the next request. When the framing itself is
broken (bad header syntax, bad chunk size), there is no safe place to resume, so the server
answers 400 and hangs up.

## Stretch goals

* **`Connection: close`** — honoured. The response says `Connection: close`, then the server
  half-closes (`shutdown(SHUT_WR)`) and drains its input before `close()`. Without that drain,
  unread client bytes can make the kernel send a RST that destroys the response still in flight.
  HTTP/1.0 requests are closed unless they send `Connection: keep-alive`.
* **Idle timeout: 15 s** (`--idle`). This is the socket timeout on every `recv`, so it covers both
  an idle keep-alive connection and a client that stalls mid-request.
  Why 15 s: a browser or script reuses a connection within milliseconds to a few seconds, so
  15 s keeps that reuse. It is also short enough that idle clients can't pin one thread each
  for long. It is shorter than common client idle pools (e.g. 60–90 s), so the *server* closes
  first, and it says so in `Keep-Alive: timeout=15` so the client can retire the connection
  before racing a close. Nginx defaults to 75 s and Apache to 5 s; 15 s sits between them.
* **Chunked encoding** — request bodies are fully decoded: chunk extensions and trailers are
  accepted and ignored.
* **Pipelining** — falls out of the buffer design. Send all six requests in one `sendall` and the
  one connection thread parses and answers them strictly in order (`check.py` does exactly this).

## Files

* `server.py` — the server. `Stream` holds the buffer, `read_request` does the framing, `handle`
  is the calculator, and `serve_connection` is the keep-alive loop.
* `check.py` — the slide's marking run on one socket, then every stretch goal.
* `tests/test_calculator.py` — 16 end-to-end tests over real sockets. They cover byte-at-a-time
  delivery, 20-deep pipelining, a body that *looks like* a request, limits, and the idle timeout.
