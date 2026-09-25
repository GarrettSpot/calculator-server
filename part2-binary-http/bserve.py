#!/usr/bin/env python3
"""bserve: a BHTTP/1 file server. Implements SPEC.md, and shares no code with bcurl.

    bserve <root> <port> [--idle SECONDS] [--send-unknown] [-q]

Accept a TCP connection, read binary request frames, map each path to a file
under <root>, reply with a RESPONSE frame and DATA frames, and keep the
connection open for the next request.

--send-unknown puts an extension frame (type 0x7E) in front of every response.
A correct client must skip it (SPEC 6); this is how you test that one does.
"""

import argparse
import mimetypes
import os
import socket
import struct
import sys
import threading
from email.utils import formatdate

# ---- constants from the spec ---------------------------------------------- #

HEADER = struct.Struct(">BHBBBH")     # length is u24: packed as u8 + u16, see pack_header
HEADER_LEN = 8
MAX_FRAME = 65536                     # SPEC 2: v1 limit for defined frame types

REQUEST, RESPONSE, DATA, GOAWAY = 0x01, 0x02, 0x03, 0x04
END_STREAM = 0x01

NO_ERROR, PROTOCOL_ERROR, FRAME_TOO_LARGE, IDLE_TIMEOUT = 0, 1, 2, 3

METHODS = {1: "GET", 2: "HEAD", 3: "POST", 4: "PUT", 5: "DELETE", 6: "OPTIONS"}

STATIC = ["host", "user-agent", "accept", "content-type", "content-length",
          "connection", "server", "date", "last-modified", "etag"]   # indices 1..10
STATIC_INDEX = {name: i + 1 for i, name in enumerate(STATIC)}

EXTENSION_TYPE = 0x7E
SERVER_NAME = "bserve/1"
REASONS = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed",
           500: "Internal Server Error"}


# ---- frames ---------------------------------------------------------------- #

def pack_frame(ftype, flags, stream, payload=b""):
    n = len(payload)
    return HEADER.pack(n >> 16, n & 0xFFFF, ftype, flags, 0, stream) + payload


def unpack_header(raw):
    hi, lo, ftype, flags, reserved, stream = HEADER.unpack(raw)
    return (hi << 16) | lo, ftype, flags, reserved, stream


def encode_headers(pairs):
    out = bytearray()
    for name, value in pairs:
        name = name.lower()
        value = value.encode("utf-8")
        idx = STATIC_INDEX.get(name)
        if idx:
            out.append(idx)
        else:
            raw = name.encode("ascii")
            out += bytes([0, len(raw)]) + raw
        out += struct.pack(">H", len(value)) + value
    return bytes(out)


class StreamError(Exception):
    """Payload is wrong but the frame was well delimited: answer 400, keep going."""


class Fatal(Exception):
    """Framing can't be trusted: GOAWAY and close."""

    def __init__(self, code, text):
        super().__init__(text)
        self.code = code


class PeerGone(Exception):
    pass


def decode_headers(buf, pos):
    """Parse a header block from buf[pos:] to the end. Returns {name: value}."""
    headers = {}
    end = len(buf)
    while pos < end:
        ref = buf[pos]
        pos += 1
        name = None
        if ref == 0:
            if pos >= end:
                raise StreamError("truncated literal name length")
            nlen = buf[pos]
            pos += 1
            if nlen == 0 or pos + nlen > end:
                raise StreamError("bad literal name length")
            try:
                name = buf[pos:pos + nlen].decode("ascii").lower()
            except UnicodeDecodeError:
                raise StreamError("non-ASCII header name")
            pos += nlen
        elif ref <= len(STATIC):
            name = STATIC[ref - 1]
        # else: reserved index (SPEC 4) - skip the entry, value still follows
        if pos + 2 > end:
            raise StreamError("truncated value length")
        (vlen,) = struct.unpack_from(">H", buf, pos)
        pos += 2
        if pos + vlen > end:
            raise StreamError("value runs past the end of the frame")
        if name is not None:
            try:
                headers[name] = buf[pos:pos + vlen].decode("utf-8")
            except UnicodeDecodeError:
                raise StreamError("header value is not UTF-8")
        pos += vlen
    return headers


def decode_request(payload):
    if len(payload) < 3:
        raise StreamError("REQUEST payload shorter than method + path_len")
    method = payload[0]
    (plen,) = struct.unpack_from(">H", payload, 1)
    if 3 + plen > len(payload):
        raise StreamError("path runs past the end of the frame")
    try:
        path = payload[3:3 + plen].decode("utf-8")
    except UnicodeDecodeError:
        raise StreamError("path is not UTF-8")
    if not path.startswith("/"):
        raise StreamError("path must start with /")
    headers = decode_headers(payload, 3 + plen)
    if "host" not in headers:
        raise StreamError("missing host")
    return method, path, headers


# ---- socket reading --------------------------------------------------------- #

class Reader:
    def __init__(self, sock):
        self.sock = sock
        self.buf = bytearray()

    def _fill(self):
        try:
            data = self.sock.recv(65536)
        except (socket.timeout, TimeoutError):
            raise
        except OSError as e:
            raise PeerGone(str(e))
        if not data:
            raise PeerGone("eof")
        self.buf += data

    def exact(self, n):
        while len(self.buf) < n:
            self._fill()
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def discard(self, n):
        """Throw away n octets without buffering them all (SPEC 6)."""
        while n:
            if not self.buf:
                self._fill()
            k = min(n, len(self.buf))
            del self.buf[:k]
            n -= k

    def frame(self):
        """Next frame of a *defined* type as (type, flags, stream, payload).

        Unknown types are skipped here, so callers never see them.
        Returns None on a clean EOF between frames.
        """
        while True:
            if not self.buf:
                try:
                    self._fill()
                except PeerGone:
                    return None
            try:
                raw = self.exact(HEADER_LEN)
            except PeerGone:
                raise Fatal(PROTOCOL_ERROR, "EOF inside a frame header")
            length, ftype, flags, _reserved, stream = unpack_header(raw)
            if ftype not in (REQUEST, RESPONSE, DATA, GOAWAY):
                try:
                    self.discard(length)
                except PeerGone:
                    raise Fatal(PROTOCOL_ERROR, "EOF inside an unknown frame")
                continue
            if length > MAX_FRAME:
                raise Fatal(FRAME_TOO_LARGE, f"frame of {length} octets")
            try:
                payload = self.exact(length)
            except PeerGone:
                raise Fatal(PROTOCOL_ERROR, "EOF inside a frame payload")
            return ftype, flags, stream, payload


# ---- the file server ------------------------------------------------------- #

class Server:
    def __init__(self, root, idle=30.0, send_unknown=False, log=True):
        self.root = os.path.realpath(root)
        self.idle = idle
        self.send_unknown = send_unknown
        self.log = log

    def resolve(self, path):
        """Map a request path to a file under root, or None."""
        rel = path.split("?", 1)[0].split("#", 1)[0]
        if "\x00" in rel:
            return None
        full = os.path.realpath(os.path.join(self.root, rel.lstrip("/")))
        try:
            if os.path.commonpath([self.root, full]) != self.root:
                return None                                   # escaped the root
        except ValueError:
            return None                                       # different drive on Windows
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        return full if os.path.isfile(full) else None

    def respond(self, sock, stream, method, path, headers):
        """Send one full response. Returns the status code."""
        if method not in (1, 2):
            return self.error(sock, stream, 405, f"{METHODS.get(method, method)} not supported")
        full = self.resolve(path)
        if full is None:
            return self.error(sock, stream, 404, f"{path} not found")
        try:
            st = os.stat(full)
            f = open(full, "rb")
        except OSError:
            return self.error(sock, stream, 404, f"{path} not found")
        with f:
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                ctype += "; charset=utf-8"
            head = encode_headers([
                ("content-type", ctype),
                ("content-length", str(st.st_size)),
                ("server", SERVER_NAME),
                ("date", formatdate(usegmt=True)),
                ("last-modified", formatdate(st.st_mtime, usegmt=True)),
                ("etag", f'"{st.st_size:x}-{st.st_mtime_ns:x}"'),
            ])
            status = struct.pack(">H", 200)
            if method == 2 or st.st_size == 0:                # HEAD, or nothing to send
                sock.sendall(pack_frame(RESPONSE, END_STREAM, stream, status + head))
                return 200
            sock.sendall(pack_frame(RESPONSE, 0, stream, status + head))
            left = st.st_size
            while left:
                chunk = f.read(min(MAX_FRAME, left))
                if not chunk:                                  # file shrank under us
                    raise PeerGone("file truncated while sending")
                left -= len(chunk)
                sock.sendall(pack_frame(DATA, END_STREAM if not left else 0, stream, chunk))
        return 200

    def error(self, sock, stream, status, text):
        body = f"{status} {REASONS.get(status, '')}: {text}\n".encode("utf-8")
        head = encode_headers([
            ("content-type", "text/plain; charset=utf-8"),
            ("content-length", str(len(body))),
            ("server", SERVER_NAME),
            ("date", formatdate(usegmt=True)),
        ])
        sock.sendall(pack_frame(RESPONSE, 0, stream, struct.pack(">H", status) + head)
                     + pack_frame(DATA, END_STREAM, stream, body))
        return status

    def goaway(self, sock, last, code, text=""):
        try:
            sock.sendall(pack_frame(GOAWAY, 0, 0, struct.pack(">HH", last, code) + text.encode()))
        except OSError:
            pass

    def say(self, addr, msg):
        if self.log:
            print(f"{addr} {msg}", file=sys.stderr, flush=True)

    def serve_connection(self, sock, addr):
        sock.settimeout(self.idle)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        reader = Reader(sock)
        last = 0             # last stream fully answered
        pending = None       # (stream, request or StreamError) while DATA frames still to come
        served = 0
        try:
            while True:
                try:
                    frame = reader.frame()
                except (socket.timeout, TimeoutError):
                    self.goaway(sock, last, IDLE_TIMEOUT, "idle")
                    self.say(addr, "idle timeout")
                    break
                if frame is None:
                    break
                ftype, flags, stream, payload = frame

                if ftype == GOAWAY:
                    self.say(addr, "client sent GOAWAY")
                    break
                if ftype == RESPONSE:
                    raise Fatal(PROTOCOL_ERROR, "server received RESPONSE")
                if ftype == REQUEST:
                    if stream == 0:
                        raise Fatal(PROTOCOL_ERROR, "REQUEST on stream 0")
                    if pending is not None:
                        raise Fatal(PROTOCOL_ERROR, "new REQUEST before END_STREAM")
                    try:
                        pending = (stream, decode_request(payload))
                    except StreamError as e:
                        pending = (stream, e)
                elif ftype == DATA:
                    if pending is None or stream != pending[0]:
                        continue                   # DATA for nothing we're reading: ignore
                    # Request bodies are read and discarded; we serve files, not uploads.
                if not flags & END_STREAM or pending is None:
                    continue

                stream, req = pending
                pending = None
                if isinstance(req, StreamError):
                    status = self.error(sock, stream, 400, str(req))
                    desc = f"malformed ({req})"
                    close = False
                else:
                    method, path, headers = req
                    status = self.respond(sock, stream, method, path, headers)
                    desc = f"{METHODS.get(method, method)} {path}"
                    close = headers.get("connection", "").lower() == "close"
                last = stream
                served += 1
                self.say(addr, f"#{served} stream={stream} {desc} -> {status}")
                if close:
                    self.goaway(sock, last, NO_ERROR, "connection: close")
                    break
        except Fatal as e:
            self.goaway(sock, last, e.code, str(e))
            self.say(addr, f"connection error: {e}")
        except (PeerGone, OSError):
            pass
        finally:
            try:
                sock.shutdown(socket.SHUT_WR)
                sock.settimeout(1.0)
                while sock.recv(65536):
                    pass
            except OSError:
                pass
            sock.close()
            self.say(addr, f"closed after {served} response(s)")

    def serve_forever(self, listener):
        while True:
            try:
                sock, addr = listener.accept()
            except OSError:
                return
            name = f"{addr[0]}:{addr[1]}"
            self.say(name, "connected")
            threading.Thread(target=self.serve_connection, args=(sock, name), daemon=True).start()


# The --send-unknown hook: wrap sendall so every RESPONSE frame is preceded by an
# extension frame that a v1 client has never heard of.
class UnknownFrameSocket:
    def __init__(self, sock):
        self._sock = sock

    def sendall(self, data):
        if len(data) > 3 and data[3] == RESPONSE:
            junk = b"v2 extension frame: skip me"
            data = pack_frame(EXTENSION_TYPE, 0xFF, 0, junk) + data
        return self._sock.sendall(data)

    def __getattr__(self, name):
        return getattr(self._sock, name)


def make_listener(port, host=""):
    if not host and socket.has_dualstack_ipv6():
        return socket.create_server(("", port), family=socket.AF_INET6, dualstack_ipv6=True)
    return socket.create_server((host or "0.0.0.0", port))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bserve", description="BHTTP/1 file server")
    ap.add_argument("root", help="directory to serve")
    ap.add_argument("port", type=int)
    ap.add_argument("--host", default="", help="bind address (default: all, IPv4+IPv6)")
    ap.add_argument("--idle", type=float, default=30.0, help="idle timeout in seconds (default 30)")
    ap.add_argument("--send-unknown", action="store_true",
                    help="precede every response with an unknown-type frame (interop test)")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.root):
        ap.error(f"{args.root} is not a directory")
    server = Server(args.root, args.idle, args.send_unknown, log=not args.quiet)
    if args.send_unknown:
        inner = server.serve_connection
        server.serve_connection = lambda sock, addr: inner(UnknownFrameSocket(sock), addr)
    listener = make_listener(args.port, args.host)
    print(f"bserve: serving {server.root} on port {args.port} (idle {args.idle:g}s)",
          file=sys.stderr, flush=True)
    try:
        server.serve_forever(listener)
    except KeyboardInterrupt:
        pass
    finally:
        listener.close()


if __name__ == "__main__":
    main()
