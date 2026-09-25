#!/usr/bin/env python3
"""Mark the calculator the way the slide says it will be marked.

One socket. Every request. Then the stretch goals, each on its own socket.

    python check.py [host] [port]
"""

import socket
import sys
import time

HOST = sys.argv[1] if len(sys.argv) > 1 else "localhost"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8080


class Reader:
    """Read HTTP/1.1 responses off one socket without ever reading past one."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""

    def _fill(self):
        data = self.sock.recv(65536)
        if not data:
            raise ConnectionError("server closed the connection")
        self.buf += data

    def response(self):
        while b"\r\n\r\n" not in self.buf:
            self._fill()
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        status = int(lines[0].split(" ")[1])
        headers = {}
        for line in lines[1:]:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        n = int(headers.get("content-length", 0))
        while len(self.buf) < n:
            self._fill()
        body, self.buf = self.buf[:n], self.buf[n:]
        return status, headers, body.decode()


def still_open(sock):
    """True if the peer has not closed: a non-blocking peek finds no EOF."""
    sock.setblocking(False)
    try:
        return sock.recv(1, socket.MSG_PEEK) != b""
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        sock.setblocking(True)


def get(path, extra=""):
    return f"GET {path} HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n{extra}\r\n".encode()


MARKED = [
    ("GET /add?a=2&b=3",  get("/add?a=2&b=3"),  200, "5"),
    ("GET /sub?a=10&b=4", get("/sub?a=10&b=4"), 200, "6"),
    ("GET /mul?a=6&b=7",  get("/mul?a=6&b=7"),  200, "42"),
    ("GET /div?a=1&b=0",  get("/div?a=1&b=0"),  400, None),
    ("GET /pow?a=2&b=8",  get("/pow?a=2&b=8"),  404, None),
    ("POST /add",         f"POST /add HTTP/1.1\r\nHost: {HOST}\r\nContent-Length: 0\r\n\r\n".encode(), 405, None),
]

failures = 0


def check(label, ok, detail=""):
    global failures
    failures += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")


def marked_run():
    print("How I will mark it - one socket, every request")
    s = socket.create_connection((HOST, PORT))
    r = Reader(s)
    for label, raw, want_status, want_body in MARKED:
        s.sendall(raw)
        status, _, body = r.response()
        shown = f"{status}   {body}" if status == 200 else str(status)
        print(f"  {label:<22} -> {shown}")
        check(label, status == want_status and (want_body is None or body == want_body))
    alive = still_open(s)
    print(f"\n  socket still open: {alive}")
    print(f"  1 TCP handshake, {len(MARKED)} responses\n")
    check("socket still open", alive)
    s.close()


def stretch():
    print("The task, rest of the table")
    s = socket.create_connection((HOST, PORT))
    r = Reader(s)
    for raw, want in [
        (get("/div?a=9&b=3"), (200, "3")),
        (get("/add?a=x&b=3"), (400, None)),
        (b"GET /add HTTP/1.1\r\n\r\n", (400, None)),          # no Host
    ]:
        s.sendall(raw)
        status, _, body = r.response()
        check(raw.split(b"\r\n")[0].decode() + (" (no Host)" if b"Host" not in raw else ""),
              status == want[0] and (want[1] is None or body == want[1]), f"-> {status} {body!r}")
    check("socket still open after no-Host 400", still_open(s))
    s.close()

    print("\nStretch: pipelining - all six in one sendall, answered in order")
    s = socket.create_connection((HOST, PORT))
    s.sendall(b"".join(raw for _, raw, _, _ in MARKED))
    r = Reader(s)
    got = [r.response()[0] for _ in MARKED]
    check("six responses, in order", got == [w for _, _, w, _ in MARKED], str(got))
    check("socket still open", still_open(s))
    s.close()

    print("\nStretch: a request body must be consumed exactly (Content-Length)")
    s = socket.create_connection((HOST, PORT))
    s.sendall(f"POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: 11\r\n\r\nGET /pow HT".encode()
              + get("/add?a=1&b=1"))
    r = Reader(s)
    a, b = r.response()[0], r.response()
    check("body bytes were not mistaken for a request", a == 405 and b[0] == 200 and b[2] == "2",
          f"{a}, {b[0]} {b[2]!r}")
    s.close()

    print("\nStretch: chunked request body")
    s = socket.create_connection((HOST, PORT))
    s.sendall(b"POST /mul HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
              b"5;ext=1\r\nhello\r\n6\r\n world\r\n0\r\nX-Trailer: yes\r\n\r\n" + get("/mul?a=3&b=3"))
    r = Reader(s)
    a, b = r.response()[0], r.response()
    check("chunked body skipped cleanly, next request answered", a == 405 and b[2] == "9", f"{a}, {b[2]!r}")
    s.close()

    print("\nStretch: Connection: close is honoured")
    s = socket.create_connection((HOST, PORT))
    s.sendall(get("/add?a=2&b=2", "Connection: close\r\n"))
    r = Reader(s)
    status, headers, body = r.response()
    s.settimeout(3)
    try:
        eof = s.recv(1) == b""
    except OSError:
        eof = True
    check("200 then server closes", status == 200 and headers.get("connection") == "close" and eof)
    s.close()

    print("\nStretch: HTTP/1.0 without keep-alive is closed, as 1.0 always was")
    s = socket.create_connection((HOST, PORT))
    s.sendall(b"GET /sub?a=1&b=2 HTTP/1.0\r\n\r\n")
    r = Reader(s)
    status, headers, body = r.response()
    s.settimeout(3)
    check("200 -1, then close", status == 200 and body == "-1" and s.recv(1) == b"")
    s.close()

    print("\nStretch: a malformed request gets a 400 and the line is hung up")
    s = socket.create_connection((HOST, PORT))
    s.sendall(b"THIS IS NOT HTTP\r\n\r\n")
    r = Reader(s)
    status = r.response()[0]
    s.settimeout(3)
    check("400 then close", status == 400 and s.recv(1) == b"")
    s.close()


if __name__ == "__main__":
    t = time.perf_counter()
    marked_run()
    stretch()
    print(f"\n{'all checks passed' if not failures else f'{failures} check(s) FAILED'}"
          f" in {time.perf_counter() - t:.2f}s")
    sys.exit(1 if failures else 0)
