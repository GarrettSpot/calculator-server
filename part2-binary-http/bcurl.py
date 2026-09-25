#!/usr/bin/env python3
"""bcurl: a BHTTP/1 client. Implements SPEC.md, and shares no code with bserve.

    bcurl [-v] [-I | -X METHOD] [-H 'name: value']... [-d DATA|@file] host:port/path [path ...]

Builds binary request frames, prints response bodies to stdout, and with -v
hexdumps every frame, in both directions, to stderr. Several paths are
pipelined down the same connection. bcurl never opens a second one.

Exit status: 0 if every response was 1xx-3xx, 4 if any was 4xx, 5 if any was
5xx, 1 on a connection or protocol error, 2 on bad usage.
"""

import argparse
import socket
import struct
import sys

HEADER_LEN = 8
MAX_FRAME = 65536

T_REQUEST, T_RESPONSE, T_DATA, T_GOAWAY = 1, 2, 3, 4
TYPE_NAMES = {1: "REQUEST", 2: "RESPONSE", 3: "DATA", 4: "GOAWAY"}
END_STREAM = 0x01
GOAWAY_CODES = {0: "NO_ERROR", 1: "PROTOCOL_ERROR", 2: "FRAME_TOO_LARGE", 3: "IDLE_TIMEOUT"}

METHOD_CODES = {"GET": 1, "HEAD": 2, "POST": 3, "PUT": 4, "DELETE": 5, "OPTIONS": 6}
TABLE = [None, "host", "user-agent", "accept", "content-type", "content-length",
         "connection", "server", "date", "last-modified", "etag"]      # SPEC 4, 1-based

DEFAULT_PORT = 9000


class ProtocolError(Exception):
    pass


# ---- building frames ------------------------------------------------------- #

def frame(ftype, flags, stream, payload=b""):
    if len(payload) > MAX_FRAME:
        raise ValueError("frame too large")
    return (len(payload).to_bytes(3, "big") + bytes([ftype, flags, 0])
            + stream.to_bytes(2, "big") + payload)


def header_block(headers):
    out = bytearray()
    for name, value in headers:
        v = value.encode("utf-8")
        if name in TABLE:
            out.append(TABLE.index(name))
        else:
            n = name.encode("ascii")
            out += b"\x00" + bytes([len(n)]) + n
        out += len(v).to_bytes(2, "big") + v
    return bytes(out)


def request_frames(stream, method, path, headers, body):
    p = path.encode("utf-8")
    payload = bytes([METHOD_CODES[method]]) + len(p).to_bytes(2, "big") + p + header_block(headers)
    if not body:
        return [frame(T_REQUEST, END_STREAM, stream, payload)]
    frames = [frame(T_REQUEST, 0, stream, payload)]
    for i in range(0, len(body), MAX_FRAME):
        last = i + MAX_FRAME >= len(body)
        frames.append(frame(T_DATA, END_STREAM if last else 0, stream, body[i:i + MAX_FRAME]))
    return frames


# ---- parsing frames -------------------------------------------------------- #

def parse_header_block(buf, pos):
    headers = []
    while pos < len(buf):
        ref = buf[pos]
        pos += 1
        if ref == 0:
            if pos >= len(buf):
                raise ProtocolError("truncated header name")
            n = buf[pos]
            name = buf[pos + 1:pos + 1 + n].decode("ascii", "replace")
            pos += 1 + n
        else:
            name = TABLE[ref] if ref < len(TABLE) else None       # None: reserved, skip
        if pos + 2 > len(buf):
            raise ProtocolError("truncated header value length")
        vlen = int.from_bytes(buf[pos:pos + 2], "big")
        value = buf[pos + 2:pos + 2 + vlen]
        if len(value) != vlen:
            raise ProtocolError("header value runs past the frame")
        pos += 2 + vlen
        if name is not None:
            headers.append((name, value.decode("utf-8", "replace")))
    return headers


def describe(ftype, flags, stream, payload):
    """Human lines explaining a frame, for -v. The first line is the summary."""
    name = TYPE_NAMES.get(ftype, f"UNKNOWN(0x{ftype:02x})")
    fl = "END_STREAM" if flags & END_STREAM else "-"
    if flags & ~END_STREAM:
        fl += f"|0x{flags & ~END_STREAM:02x}"
    lines = [f"{name} len={len(payload)} flags={fl} stream={stream}"]
    try:
        if ftype == T_REQUEST:
            m = {v: k for k, v in METHOD_CODES.items()}.get(payload[0], payload[0])
            plen = int.from_bytes(payload[1:3], "big")
            lines.append(f" {m} {payload[3:3 + plen].decode('utf-8', 'replace')}")
            lines += [f" {k}: {v}" for k, v in parse_header_block(payload, 3 + plen)]
        elif ftype == T_RESPONSE:
            lines.append(f" status {int.from_bytes(payload[:2], 'big')}")
            lines += [f" {k}: {v}" for k, v in parse_header_block(payload, 2)]
        elif ftype == T_GOAWAY:
            last, code = struct.unpack(">HH", payload[:4])
            lines.append(f" last_stream={last} code={GOAWAY_CODES.get(code, code)} "
                         f"{payload[4:].decode('utf-8', 'replace')!r}")
        elif ftype not in TYPE_NAMES:
            lines.append(" unknown frame type: skipped (SPEC 6)")
    except (IndexError, struct.error, ProtocolError) as e:
        lines.append(f" (undecodable: {e})")
    return lines


def hexdump(data, limit=0):
    shown = data[:limit] if limit else data
    out = []
    for off in range(0, len(shown), 16):
        row = shown[off:off + 16]
        hx = " ".join(f"{b:02x}" for b in row[:8]) + "  " + " ".join(f"{b:02x}" for b in row[8:])
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        out.append(f"  {off:08x}  {hx:<49} |{text}|")
    if len(shown) < len(data):
        out.append(f"  ... {len(data) - len(shown)} more octets not shown (--dump-limit)")
    return out


class Connection:
    def __init__(self, sock, verbose, dump_limit):
        self.sock = sock
        self.buf = bytearray()
        self.verbose = verbose
        self.dump_limit = dump_limit

    def log(self, direction, raw, ftype, flags, stream, payload):
        if not self.verbose:
            return
        desc = describe(ftype, flags, stream, payload)
        print(f"{direction} {desc[0]}", file=sys.stderr)
        for line in hexdump(raw, self.dump_limit):
            print(line, file=sys.stderr)
        for line in desc[1:]:
            print(f"{direction} {line}", file=sys.stderr)
        sys.stderr.flush()

    def send(self, raw_frames):
        for raw in raw_frames:
            length = int.from_bytes(raw[:3], "big")
            self.log(">", raw, raw[3], raw[4], int.from_bytes(raw[6:8], "big"), raw[8:8 + length])
        self.sock.sendall(b"".join(raw_frames))

    def _exact(self, n):
        while len(self.buf) < n:
            data = self.sock.recv(65536)
            if not data:
                raise ProtocolError("server closed the connection mid-frame"
                                    if self.buf else "server closed the connection")
            self.buf += data
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def recv_frame(self):
        """Next frame of a defined type. Unknown types are read, logged and skipped."""
        while True:
            head = self._exact(HEADER_LEN)
            length = int.from_bytes(head[:3], "big")
            ftype, flags = head[3], head[4]              # head[5]: reserved, ignored
            stream = int.from_bytes(head[6:8], "big")
            known = ftype in TYPE_NAMES
            if known and length > MAX_FRAME:
                raise ProtocolError(f"{TYPE_NAMES[ftype]} frame of {length} octets "
                                    f"exceeds {MAX_FRAME}")
            if not known and length > MAX_FRAME:
                self.log("<", head, ftype, flags, stream, b"")
                left = length                            # discard without buffering it all
                while left:
                    left -= len(self._exact(min(left, 65536)))
                continue
            payload = self._exact(length)
            self.log("<", head + payload, ftype, flags, stream, payload)
            if known:
                return ftype, flags, stream, payload


# ---- command line ---------------------------------------------------------- #

def split_target(text):
    """'localhost:9000/x' -> ('localhost', 9000, '/x'). Also bhttp:// and [::1]:9000.

    A bare '/path' returns (None, None, '/path').
    """
    for scheme in ("bhttp://", "http://"):
        if text.startswith(scheme):
            text = text[len(scheme):]
    if text.startswith("/"):
        return None, None, text
    authority, _, path = text.partition("/")
    path = "/" + path
    if authority.startswith("["):
        host, _, rest = authority[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    elif authority.count(":") == 1:
        host, _, port = authority.partition(":")
    else:
        host, port = authority, ""
    if not host or (port and not port.isdigit()):
        raise ValueError(f"cannot parse target {text!r}")
    return host, int(port) if port else DEFAULT_PORT, path


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bcurl", description="BHTTP/1 client")
    ap.add_argument("targets", nargs="+", metavar="host:port/path",
                    help="the first target names the server; later ones may be bare /paths")
    ap.add_argument("-v", "--verbose", action="store_true", help="hexdump every frame to stderr")
    ap.add_argument("-X", "--request", metavar="METHOD", type=str.upper,
                    choices=sorted(METHOD_CODES))
    ap.add_argument("-I", "--head", action="store_true", help="send HEAD")
    ap.add_argument("-H", "--header", action="append", default=[], metavar="'name: value'")
    ap.add_argument("-d", "--data", help="request body, sent as DATA frames; @file reads a file")
    ap.add_argument("--timeout", type=float, default=10.0, help="socket timeout, seconds")
    ap.add_argument("--dump-limit", type=int, default=0, metavar="N",
                    help="with -v, show at most N octets of each frame (default: all)")
    args = ap.parse_args(argv)

    method = "HEAD" if args.head else args.request or ("POST" if args.data is not None else "GET")
    body = b""
    if args.data is not None and args.data.startswith("@"):
        try:
            with open(args.data[1:], "rb") as f:
                body = f.read()
        except OSError as e:
            ap.error(f"cannot read {args.data[1:]}: {e}")
    elif args.data is not None:
        body = args.data.encode("utf-8")

    try:
        host, port, first = split_target(args.targets[0])
        if host is None:
            raise ValueError("the first target must name a host, e.g. localhost:9000/")
        paths = [first]
        for t in args.targets[1:]:
            h, p, path = split_target(t)
            if h is not None and (h, p) != (host, port):
                raise ValueError(f"{t}: bcurl never opens a second connection, "
                                 f"so every target must be on {host}:{port}")
            paths.append(path)
    except ValueError as e:
        ap.error(str(e))

    extra = []
    for h in args.header:
        name, colon, value = h.partition(":")
        if not colon or not name.strip() or not name.strip().isascii():
            ap.error(f"bad header {h!r}; expected 'name: value'")
        extra.append((name.strip().lower(), value.strip()))
    overridden = {n for n, _ in extra}
    authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    headers = [(n, v) for n, v in [("host", authority), ("user-agent", "bcurl/1"),
                                   ("accept", "*/*")] if n not in overridden]
    if body and "content-length" not in overridden:
        headers.append(("content-length", str(len(body))))
    headers += extra

    try:
        sock = socket.create_connection((host, port), timeout=args.timeout)
    except OSError as e:
        print(f"bcurl: cannot connect to {host}:{port}: {e}", file=sys.stderr)
        return 1
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    conn = Connection(sock, args.verbose, args.dump_limit)
    if args.verbose:
        peer = sock.getpeername()
        print(f"* connected to {peer[0]} port {peer[1]}: one connection, "
              f"{len(paths)} request(s)", file=sys.stderr)

    out = sys.stdout.buffer
    statuses = []
    try:
        # Pipeline: every request goes out before the first response is read.
        for i, path in enumerate(paths, start=1):
            conn.send(request_frames(i, method, path, headers, body))

        for expect in range(1, len(paths) + 1):
            ftype, flags, stream, payload = conn.recv_frame()
            if ftype == T_GOAWAY:
                last, code = struct.unpack(">HH", payload[:4])
                raise ProtocolError(f"server sent GOAWAY ({GOAWAY_CODES.get(code, code)}) "
                                    f"after stream {last}, before answering stream {expect}")
            if ftype != T_RESPONSE or stream != expect:
                raise ProtocolError(f"expected RESPONSE on stream {expect}, got "
                                    f"{TYPE_NAMES[ftype]} on stream {stream}")
            if len(payload) < 2:
                raise ProtocolError("RESPONSE shorter than its status field")
            status = int.from_bytes(payload[:2], "big")
            resp_headers = dict(parse_header_block(payload, 2))
            statuses.append(status)
            received = 0
            while not flags & END_STREAM:
                ftype, flags, stream, payload = conn.recv_frame()
                if ftype != T_DATA or stream != expect:
                    raise ProtocolError(f"expected DATA on stream {expect}, got "
                                        f"{TYPE_NAMES[ftype]} on stream {stream}")
                out.write(payload)
                received += len(payload)
            out.flush()
            cl = resp_headers.get("content-length", "")
            if method != "HEAD" and cl.isdigit() and int(cl) != received:
                raise ProtocolError(f"content-length {cl} but {received} octets of DATA")

        # Done: close properly, at a frame boundary, on the same connection.
        conn.send([frame(T_GOAWAY, 0, 0, struct.pack(">HH", len(paths), 0))])
        if args.verbose:
            print(f"* {len(paths)} response(s) on 1 connection, statuses {statuses}",
                  file=sys.stderr)
    except ProtocolError as e:
        print(f"bcurl: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"bcurl: connection error: {e}", file=sys.stderr)
        return 1
    finally:
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        sock.close()

    if any(s >= 500 for s in statuses):
        return 5
    if any(s >= 400 for s in statuses):
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
