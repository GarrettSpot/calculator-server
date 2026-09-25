#!/usr/bin/env python3
"""A calculator that stays on the line.

HTTP/1.1 over a bare socket: no http.server, no framework. The arithmetic is
trivial; the point is framing. Under HTTP/1.0 a request ended at EOF. Here the
connection stays open, so every request must be delimited by its own bytes:

    head   ends at the first CRLFCRLF
    body   is exactly Content-Length bytes, or a chunked stream ending in a
           zero-size chunk, or (for a request with neither) empty

Whatever is left in the buffer after that belongs to the next request, and
that is what makes keep-alive and pipelining work.

    python server.py [port] [--host HOST] [--idle SECONDS]
"""

import argparse
import math
import re
import socket
import sys
import threading
from email.utils import formatdate
from urllib.parse import unquote_plus

MAX_HEAD = 8 * 1024          # request line + headers
MAX_BODY = 1024 * 1024       # we are a calculator; nobody needs to send us a megabyte
MAX_CHUNK_LINE = 1024
IDLE_TIMEOUT = 15.0          # seconds; see README "Idle timeout"

REASONS = {
    100: "Continue", 200: "OK", 400: "Bad Request", 404: "Not Found",
    405: "Method Not Allowed", 413: "Content Too Large",
    431: "Request Header Fields Too Large", 501: "Not Implemented",
    505: "HTTP Version Not Supported",
}

TOKEN = re.compile(rb"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
NUMBER = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?")


class ClientGone(Exception):
    """Peer closed or went idle. Nothing to answer; just close."""


class Malformed(Exception):
    """The byte stream can no longer be trusted: answer, then hang up."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- #
# Byte stream                                                                 #
# --------------------------------------------------------------------------- #

class Stream:
    """A socket plus a buffer. Reads never consume more than they return."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = bytearray()

    def _fill(self):
        try:
            data = self.sock.recv(65536)
        except (socket.timeout, TimeoutError):
            raise ClientGone("idle timeout")
        except OSError as e:
            raise ClientGone(str(e))
        if not data:
            raise ClientGone("eof")
        self.buf += data

    def read_until(self, delim, limit, status=400):
        """Return bytes before `delim` and consume `delim` as well."""
        start = 0
        while True:
            i = self.buf.find(delim, start)
            if i > limit:
                raise Malformed(status, "line or head too long")
            if i != -1:
                out = bytes(self.buf[:i])
                del self.buf[:i + len(delim)]
                return out
            if len(self.buf) > limit:
                raise Malformed(status, "line or head too long")
            start = max(0, len(self.buf) - len(delim) + 1)
            self._fill()

    def read_exact(self, n):
        while len(self.buf) < n:
            self._fill()
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def skip_blank_lines(self):
        # RFC 9112 2.2: ignore at least one empty line before a request-line.
        # EOF while waiting here is the clean end of a keep-alive connection.
        while True:
            if not self.buf or self.buf == b"\r":
                self._fill()
            elif self.buf.startswith(b"\r\n"):
                del self.buf[:2]
            else:
                return


# --------------------------------------------------------------------------- #
# Parsing one request off the stream                                          #
# --------------------------------------------------------------------------- #

class Request:
    def __init__(self, method, target, version, headers, body):
        self.method = method
        self.target = target
        self.version = version
        self.headers = headers      # lower-case name -> list of values
        self.body = body

    def header(self, name):
        values = self.headers.get(name)
        return values[-1] if values else None

    def tokens(self, name):
        out = set()
        for v in self.headers.get(name, []):
            out.update(t.strip().lower() for t in v.split(",") if t.strip())
        return out


def read_request(stream):
    stream.skip_blank_lines()
    head = stream.read_until(b"\r\n\r\n", MAX_HEAD, status=431)
    lines = head.split(b"\r\n")

    parts = lines[0].split(b" ")
    if len(parts) != 3 or not TOKEN.fullmatch(parts[0]) or not parts[1]:
        raise Malformed(400, "bad request line")
    method, target, version = (p.decode("latin-1") for p in parts)
    if not re.fullmatch(r"HTTP/\d\.\d", version):
        raise Malformed(400, "bad HTTP version")
    if version[5] != "1":
        raise Malformed(505, "only HTTP/1.x is spoken here")

    headers = {}
    for line in lines[1:]:
        name, colon, value = line.partition(b":")
        if not colon or not TOKEN.fullmatch(name):   # also rejects "Name :"
            raise Malformed(400, "bad header line")
        headers.setdefault(name.decode("latin-1").lower(), []).append(
            value.strip(b" \t").decode("latin-1"))

    req = Request(method, target, version, headers, b"")
    req.body = read_body(stream, req)
    return req


def read_body(stream, req):
    te = req.headers.get("transfer-encoding")
    cl = req.headers.get("content-length")

    if te is not None:
        if cl is not None:
            # Both present is the classic request-smuggling shape. Refuse.
            raise Malformed(400, "both Transfer-Encoding and Content-Length")
        codings = [c.strip().lower() for v in te for c in v.split(",") if c.strip()]
        if codings != ["chunked"]:
            raise Malformed(501, "only Transfer-Encoding: chunked is supported")
        maybe_continue(stream, req)
        return read_chunked(stream)

    if cl is not None:
        values = {v.strip() for v in ",".join(cl).split(",")}
        if len(values) != 1 or not next(iter(values)).isdigit():
            raise Malformed(400, "bad Content-Length")
        n = int(values.pop())
        if n > MAX_BODY:
            raise Malformed(413, "body too large")
        if n:
            maybe_continue(stream, req)
        return stream.read_exact(n)   # exactly n. Byte n+1 is somebody else's.

    return b""


def maybe_continue(stream, req):
    if req.version == "HTTP/1.1" and "100-continue" in req.tokens("expect") and not stream.buf:
        stream.sock.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")


def read_chunked(stream):
    body = bytearray()
    while True:
        line = stream.read_until(b"\r\n", MAX_CHUNK_LINE)
        size_text = line.split(b";", 1)[0].strip()      # drop chunk extensions
        if not size_text or not re.fullmatch(rb"[0-9A-Fa-f]+", size_text):
            raise Malformed(400, "bad chunk size")
        size = int(size_text, 16)
        if size == 0:
            break
        if len(body) + size > MAX_BODY:
            raise Malformed(413, "body too large")
        body += stream.read_exact(size)
        if stream.read_exact(2) != b"\r\n":
            raise Malformed(400, "chunk not followed by CRLF")
    # Trailer section: header lines until an empty line. We read and ignore them.
    trailer_bytes = 0
    while True:
        line = stream.read_until(b"\r\n", MAX_HEAD)
        if not line:
            return bytes(body)
        trailer_bytes += len(line)
        if trailer_bytes > MAX_HEAD:
            raise Malformed(431, "trailers too large")


# --------------------------------------------------------------------------- #
# The calculator                                                              #
# --------------------------------------------------------------------------- #

class BadInput(Exception):
    pass


def parse_query(query):
    params = {}
    for pair in query.split("&"):
        if not pair:
            continue
        key, _, value = pair.partition("=")
        params.setdefault(unquote_plus(key), []).append(unquote_plus(value))
    return params


def number(params, name):
    values = params.get(name)
    if not values:
        raise BadInput(f"missing parameter '{name}'")
    if len(values) > 1:
        raise BadInput(f"parameter '{name}' given more than once")
    text = values[0]
    if not NUMBER.fullmatch(text):
        raise BadInput(f"parameter '{name}' is not a number: {text!r}")
    if re.fullmatch(r"[+-]?\d+", text):
        return int(text)
    return float(text)


def divide(a, b):
    if b == 0:
        raise BadInput("division by zero")
    if isinstance(a, int) and isinstance(b, int) and a % b == 0:
        return a // b
    return a / b


OPS = {
    "/add": lambda a, b: a + b,
    "/sub": lambda a, b: a - b,
    "/mul": lambda a, b: a * b,
    "/div": divide,
}


def fmt(x):
    if isinstance(x, float):
        if not math.isfinite(x):
            raise BadInput("result out of range")
        if x.is_integer() and abs(x) < 2 ** 53:
            return str(int(x))
    return str(x)


def handle(req):
    """Return (status, body text, extra headers)."""
    if req.version == "HTTP/1.1" and len(req.headers.get("host", [])) != 1:
        return 400, "HTTP/1.1 requires exactly one Host header", {}

    target = req.target
    if target.startswith(("http://", "https://")):          # absolute-form
        target = "/" + target.split("//", 1)[1].partition("/")[2]
    path, _, query = target.partition("?")

    op = OPS.get(path)
    if op is None:
        return 404, f"no such operation: {path}", {}
    if req.method not in ("GET", "HEAD"):
        return 405, f"{req.method} not allowed on {path}", {"Allow": "GET, HEAD"}

    try:
        params = parse_query(query)
        return 200, fmt(op(number(params, "a"), number(params, "b"))), {}
    except BadInput as e:
        return 400, str(e), {}


# --------------------------------------------------------------------------- #
# Connection loop                                                             #
# --------------------------------------------------------------------------- #

def respond(sock, status, body, keep_alive, head_only=False, extra=None):
    payload = body.encode("utf-8")
    lines = [
        f"HTTP/1.1 {status} {REASONS[status]}",
        f"Date: {formatdate(usegmt=True)}",
        "Server: calc/1.1",
        "Content-Type: text/plain; charset=utf-8",
        f"Content-Length: {len(payload)}",
        f"Connection: {'keep-alive' if keep_alive else 'close'}",
    ]
    if keep_alive:
        lines.append(f"Keep-Alive: timeout={int(IDLE_TIMEOUT)}")
    for k, v in (extra or {}).items():
        lines.append(f"{k}: {v}")
    raw = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
    sock.sendall(raw if head_only else raw + payload)


def wants_keep_alive(req):
    conn = req.tokens("connection")
    if req.version == "HTTP/1.1":
        return "close" not in conn
    return "keep-alive" in conn                               # HTTP/1.0 opt-in


def lingering_close(sock):
    """Half-close, then drain, so unread input cannot RST away our last response."""
    try:
        sock.shutdown(socket.SHUT_WR)
        sock.settimeout(1.0)
        while sock.recv(65536):
            pass
    except OSError:
        pass
    finally:
        sock.close()


def serve_connection(sock, addr, log=True):
    sock.settimeout(IDLE_TIMEOUT)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    stream = Stream(sock)
    served = 0
    try:
        while True:
            try:
                req = read_request(stream)
            except Malformed as e:
                respond(sock, e.status, str(e), keep_alive=False)
                if log:
                    print(f"{addr} !! {e.status} {e}", file=sys.stderr)
                break
            status, body, extra = handle(req)
            keep = wants_keep_alive(req)
            respond(sock, status, body, keep, head_only=req.method == "HEAD", extra=extra)
            served += 1
            if log:
                print(f"{addr} #{served} {req.method} {req.target} -> {status}", file=sys.stderr)
            if not keep:
                break
    except ClientGone:
        pass
    except OSError:
        pass
    finally:
        lingering_close(sock)
        if log:
            print(f"{addr} closed after {served} response(s)", file=sys.stderr)


def make_listener(host, port):
    if not host and socket.has_dualstack_ipv6():
        # "localhost" may resolve to ::1 first; listen on both so nobody waits.
        return socket.create_server(("", port), family=socket.AF_INET6, dualstack_ipv6=True)
    return socket.create_server((host or "0.0.0.0", port))


def serve_forever(listener, log=True):
    while True:
        try:
            sock, addr = listener.accept()
        except OSError:
            return          # listener closed
        name = f"{addr[0]}:{addr[1]}"
        threading.Thread(target=serve_connection, args=(sock, name, log), daemon=True).start()


def main(argv=None):
    global IDLE_TIMEOUT
    ap = argparse.ArgumentParser(description="HTTP/1.1 keep-alive calculator")
    ap.add_argument("port", nargs="?", type=int, default=8080)
    ap.add_argument("--host", default="", help="bind address (default: all, IPv4+IPv6)")
    ap.add_argument("--idle", type=float, default=IDLE_TIMEOUT, help="idle timeout, seconds")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)
    IDLE_TIMEOUT = args.idle

    listener = make_listener(args.host, args.port)
    print(f"calculator listening on port {args.port} (idle timeout {IDLE_TIMEOUT:g}s)", file=sys.stderr)
    try:
        serve_forever(listener, log=not args.quiet)
    except KeyboardInterrupt:
        pass
    finally:
        listener.close()


if __name__ == "__main__":
    main()
