"""BHTTP/1 conformance tests.

Frames here are built by hand from SPEC.md, not with bserve's or bcurl's
encoders, so a pass means the program agrees with the spec, not just with
itself.

    python -m unittest discover -s tests      (from part2-binary-http/)
"""

import os
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WWW = os.path.join(ROOT, "www")
sys.path.insert(0, ROOT)
import bserve  # noqa: E402  (only to start the server in-process)

REQUEST, RESPONSE, DATA, GOAWAY = 1, 2, 3, 4
END = 0x01


# ---- hand-rolled encoding, straight from the spec ------------------------- #

def frame(ftype, flags, stream, payload=b"", reserved=0):
    return len(payload).to_bytes(3, "big") + bytes([ftype, flags, reserved]) \
        + stream.to_bytes(2, "big") + payload


def hdr(ref, value, name=None):
    v = value.encode()
    if ref == 0:
        return b"\x00" + bytes([len(name)]) + name.encode() + len(v).to_bytes(2, "big") + v
    return bytes([ref]) + len(v).to_bytes(2, "big") + v


def get(path, stream=1, method=1, extra=b"", host=True, flags=END):
    p = path.encode()
    payload = bytes([method]) + len(p).to_bytes(2, "big") + p
    if host:
        payload += hdr(1, "localhost")
    return frame(REQUEST, flags, stream, payload + extra)


def parse_headers(buf, pos):
    table = [None, "host", "user-agent", "accept", "content-type", "content-length",
             "connection", "server", "date", "last-modified", "etag"]
    out = {}
    while pos < len(buf):
        ref = buf[pos]; pos += 1
        if ref == 0:
            n = buf[pos]; name = buf[pos + 1:pos + 1 + n].decode(); pos += 1 + n
        else:
            name = table[ref]
        vlen = int.from_bytes(buf[pos:pos + 2], "big")
        out[name] = buf[pos + 2:pos + 2 + vlen].decode()
        pos += 2 + vlen
    return out


class Peer:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.buf = b""

    def send(self, raw):
        self.sock.sendall(raw)

    def exact(self, n):
        while len(self.buf) < n:
            d = self.sock.recv(65536)
            if not d:
                raise EOFError
            self.buf += d
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def frame(self):
        h = self.exact(8)
        length = int.from_bytes(h[:3], "big")
        return h[3], h[4], int.from_bytes(h[6:8], "big"), self.exact(length)

    def response(self):
        """(stream, status, headers, body, data_frame_sizes)"""
        ftype, flags, stream, payload = self.frame()
        assert ftype == RESPONSE, f"expected RESPONSE, got type {ftype}"
        status = int.from_bytes(payload[:2], "big")
        headers = parse_headers(payload, 2)
        body, sizes = b"", []
        while not flags & END:
            ftype, flags, s, payload = self.frame()
            assert ftype == DATA and s == stream
            body += payload
            sizes.append(len(payload))
        return stream, status, headers, body, sizes

    def eof(self):
        try:
            return self.sock.recv(1) == b""
        except (ConnectionResetError, ConnectionAbortedError):
            return True

    def close(self):
        self.sock.close()


class CountingServer(bserve.Server):
    connections = 0

    def serve_connection(self, sock, addr):
        type(self).connections += 1
        super().serve_connection(sock, addr)


def start(idle=5.0, send_unknown=False):
    server = CountingServer(WWW, idle=idle, log=False)
    if send_unknown:
        inner = server.serve_connection
        server.serve_connection = lambda s, a: inner(bserve.UnknownFrameSocket(s), a)
    listener = socket.create_server(("127.0.0.1", 0))
    threading.Thread(target=server.serve_forever, args=(listener,), daemon=True).start()
    return listener, listener.getsockname()[1]


def read_file(name):
    with open(os.path.join(WWW, name), "rb") as f:
        return f.read()


# ---- server conformance ---------------------------------------------------- #

class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.listener, cls.port = start()

    @classmethod
    def tearDownClass(cls):
        cls.listener.close()

    def setUp(self):
        self.p = Peer(self.port)

    def tearDown(self):
        self.p.close()

    def test_get_file(self):
        self.p.send(get("/hello.txt"))
        stream, status, headers, body, _ = self.p.response()
        self.assertEqual((stream, status, body), (1, 200, read_file("hello.txt")))
        self.assertEqual(headers["content-length"], str(len(body)))
        self.assertTrue(headers["content-type"].startswith("text/plain"))
        for name in ("server", "date", "last-modified", "etag"):
            self.assertIn(name, headers)

    def test_directory_maps_to_index(self):
        self.p.send(get("/") + get("/docs/", stream=2))
        self.assertEqual(self.p.response()[3], read_file("index.html"))
        self.assertEqual(self.p.response()[3], read_file(os.path.join("docs", "index.html")))

    def test_binary_file_split_into_data_frames(self):
        self.p.send(get("/big.bin"))
        _, status, headers, body, sizes = self.p.response()
        self.assertEqual(status, 200)
        self.assertEqual(body, read_file("big.bin"))
        self.assertGreater(len(sizes), 1)
        self.assertTrue(all(s <= 65536 for s in sizes))

    def test_404_and_connection_stays_open(self):
        self.p.send(get("/missing.html"))
        self.assertEqual(self.p.response()[1], 404)
        self.p.send(get("/hello.txt", stream=2))
        self.assertEqual(self.p.response()[:2], (2, 200))

    def test_traversal_is_404(self):
        for i, path in enumerate(["/../bserve.py", "/docs/../../SPEC.md", "/..\\bserve.py",
                                  "/C:/Windows/win.ini", "/%2e%2e/bserve.py"], start=1):
            with self.subTest(path=path):
                self.p.send(get(path, stream=i))
                self.assertEqual(self.p.response()[1], 404)

    def test_head(self):
        self.p.send(frame(REQUEST, END, 1, b"\x02\x00\x0a/hello.txt" + hdr(1, "x")))
        ftype, flags, stream, payload = self.p.frame()
        self.assertEqual((ftype, flags & END), (RESPONSE, END))
        self.assertEqual(parse_headers(payload, 2)["content-length"], "21")
        self.p.send(get("/hello.txt", stream=2))
        self.assertEqual(self.p.response()[:2], (2, 200), "HEAD must not have sent DATA")

    def test_pipelining_in_order(self):
        names = ["hello.txt", "index.html", "missing", "pixel.png", "hello.txt"]
        self.p.send(b"".join(get("/" + n, stream=i) for i, n in enumerate(names, start=1)))
        got = [self.p.response()[:2] for _ in names]
        self.assertEqual(got, [(1, 200), (2, 200), (3, 404), (4, 200), (5, 200)])

    def test_frames_split_byte_by_byte(self):
        for b in get("/hello.txt"):
            self.p.send(bytes([b]))
        self.assertEqual(self.p.response()[1], 200)

    def test_unknown_frame_types_are_skipped(self):
        self.p.send(frame(0x7F, 0xFF, 0, b"future stuff")
                    + frame(0x05, 0, 1, b"")                        # empty unknown frame
                    + b"\x01\x86\xa0\x42\x00\x00\x00\x00" + bytes(100000)  # 100000 octets > max
                    + get("/hello.txt"))
        self.assertEqual(self.p.response()[:2], (1, 200))

    def test_reserved_octet_and_flags_ignored(self):
        p = b"\x01\x00\x0a/hello.txt" + hdr(1, "x")
        self.p.send(frame(REQUEST, END | 0xF0, 1, p, reserved=0xAB))
        self.assertEqual(self.p.response()[1], 200)

    def test_literal_and_reserved_header_names(self):
        extra = hdr(0, "localhost", name="host") + hdr(0, "hi", name="x-custom") \
            + b"\x0b\x00\x03abc" + b"\xff\x00\x00"                   # reserved indexes
        self.p.send(get("/hello.txt", host=False, extra=extra))
        self.assertEqual(self.p.response()[1], 200)

    def test_malformed_payload_is_400_and_connection_survives(self):
        bad = [
            frame(REQUEST, END, 1, b"\x01\x00"),                          # truncated path_len
            frame(REQUEST, END, 2, b"\x01\x00\x50/x"),                    # path past payload
            frame(REQUEST, END, 3, b"\x01\x00\x01x" + hdr(1, "h")),       # no leading /
            frame(REQUEST, END, 4, b"\x01\x00\x02/\xff" + hdr(1, "h")),   # bad UTF-8
            get("/hello.txt", stream=5, host=False),                      # no host
            get("/hello.txt", stream=6, extra=b"\x02\x00\x09abc"),        # value past payload
            get("/hello.txt", stream=7, extra=b"\x00\x00\x00\x00"),       # empty literal name
        ]
        self.p.send(b"".join(bad) + get("/hello.txt", stream=8))
        got = [self.p.response()[:2] for _ in range(8)]
        self.assertEqual(got, [(i, 400) for i in range(1, 8)] + [(8, 200)])

    def test_unsupported_method_consumes_body(self):
        body = b"x" * 70000
        self.p.send(get("/hello.txt", method=3, flags=0)
                    + frame(DATA, 0, 1, body[:65536]) + frame(DATA, END, 1, body[65536:])
                    + get("/hello.txt", stream=2))
        self.assertEqual(self.p.response()[:2], (1, 405))
        self.assertEqual(self.p.response()[:2], (2, 200))

    def test_connection_close_header(self):
        self.p.send(get("/hello.txt", extra=hdr(6, "close")))
        self.assertEqual(self.p.response()[1], 200)
        ftype, _, stream, payload = self.p.frame()
        self.assertEqual((ftype, stream), (GOAWAY, 0))
        self.assertEqual(struct.unpack(">HH", payload[:4]), (1, 0))
        self.assertTrue(self.p.eof())

    def test_client_goaway_closes(self):
        self.p.send(frame(GOAWAY, 0, 0, b"\x00\x00\x00\x00"))
        self.assertTrue(self.p.eof())


class ConnectionErrorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.listener, cls.port = start()

    @classmethod
    def tearDownClass(cls):
        cls.listener.close()

    def goaway_code(self, raw):
        p = Peer(self.port)
        p.send(raw)
        ftype, _, stream, payload = p.frame()
        self.assertEqual((ftype, stream), (GOAWAY, 0))
        self.assertTrue(p.eof())
        p.close()
        return struct.unpack(">HH", payload[:4])[1]

    def test_oversized_defined_frame(self):
        self.assertEqual(self.goaway_code(b"\x01\x00\x01\x01\x01\x00\x00\x01"), 2)

    def test_request_on_stream_zero(self):
        self.assertEqual(self.goaway_code(get("/hello.txt", stream=0)), 1)

    def test_server_receives_response(self):
        self.assertEqual(self.goaway_code(frame(RESPONSE, END, 1, b"\x00\xc8")), 1)


class IdleTest(unittest.TestCase):
    def test_idle_timeout_sends_goaway(self):
        listener, port = start(idle=0.5)
        try:
            p = Peer(port)
            p.send(get("/hello.txt"))
            self.assertEqual(p.response()[1], 200)
            ftype, _, _, payload = p.frame()
            self.assertEqual((ftype, struct.unpack(">HH", payload[:4])), (GOAWAY, (1, 3)))
            self.assertTrue(p.eof())
            p.close()
        finally:
            listener.close()


# ---- bcurl ----------------------------------------------------------------- #

def bcurl(*args):
    return subprocess.run([sys.executable, os.path.join(ROOT, "bcurl.py"), *args],
                          capture_output=True, timeout=20)


class BcurlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.listener, cls.port = start()
        cls.odd_listener, cls.odd_port = start(send_unknown=True)

    @classmethod
    def tearDownClass(cls):
        cls.listener.close()
        cls.odd_listener.close()

    def url(self, path, port=None):
        return f"127.0.0.1:{port or self.port}{path}"

    def test_body_to_stdout_exit_zero(self):
        r = bcurl(self.url("/pixel.png"))
        self.assertEqual((r.returncode, r.stdout), (0, read_file("pixel.png")))

    def test_large_file(self):
        r = bcurl(self.url("/big.bin"))
        self.assertEqual((r.returncode, r.stdout), (0, read_file("big.bin")))

    def test_exit_codes(self):
        self.assertEqual(bcurl(self.url("/nope")).returncode, 4)
        self.assertEqual(bcurl("-X", "POST", self.url("/hello.txt")).returncode, 4)
        self.assertEqual(bcurl("127.0.0.1:1/x").returncode, 1)
        self.assertEqual(bcurl(self.url("/a"), "localhost:1/b").returncode, 2)

    def test_many_paths_one_connection(self):
        before = CountingServer.connections
        r = bcurl("-v", self.url("/hello.txt"), "/index.html", "/hello.txt")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, read_file("hello.txt") + read_file("index.html")
                         + read_file("hello.txt"))
        self.assertEqual(CountingServer.connections - before, 1)
        self.assertIn(b"3 response(s) on 1 connection", r.stderr)

    def test_verbose_hexdumps_every_frame(self):
        r = bcurl("-v", self.url("/hello.txt"))
        err = r.stderr.decode()
        for summary in ("> REQUEST len=", "< RESPONSE len=", "< DATA len=21 flags=END_STREAM",
                        "> GOAWAY len=4"):
            self.assertIn(summary, err)
        self.assertIn("  00000000  00 00 15 03 01 00 00 01", err)   # the DATA frame header
        self.assertEqual(r.stdout, read_file("hello.txt"), "hexdump must not leak into stdout")

    def test_head_and_post_body(self):
        r = bcurl("-I", self.url("/hello.txt"))
        self.assertEqual((r.returncode, r.stdout), (0, b""))
        r = bcurl("-d", "@" + os.path.join(WWW, "big.bin"), self.url("/hello.txt"), "/hello.txt")
        self.assertEqual(r.returncode, 4)                     # both 405, body frames consumed

    def test_skips_unknown_frames_from_server(self):
        r = bcurl("-v", self.url("/hello.txt", self.odd_port), "/big.bin")
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertEqual(r.stdout, read_file("hello.txt") + read_file("big.bin"))
        self.assertIn(b"UNKNOWN(0x7e)", r.stderr)

    def test_against_hand_rolled_server(self):
        """A server that is not bserve, sending only what the spec allows."""
        listener = socket.create_server(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        seen = []

        def serve():
            s, _ = listener.accept()
            with s:
                data = b""
                while len(data) < 8 or len(data) < 8 + int.from_bytes(data[:3], "big"):
                    data += s.recv(65536)
                seen.append(data)
                resp = frame(RESPONSE, 0, 1, b"\x00\xc8" + hdr(5, "2") + hdr(0, "v", name="x-y")
                             + b"\x0c\x00\x01z", reserved=7)
                s.sendall(frame(0x40, 0, 9, bytes(70000))          # huge unknown frame
                          + resp + frame(DATA, 0x80, 1, b"o")      # unknown flag bit
                          + frame(DATA, END, 1, b"k"))
                time.sleep(0.5)

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        r = bcurl(f"127.0.0.1:{port}/x")
        t.join(3)
        listener.close()
        self.assertEqual((r.returncode, r.stdout), (0, b"ok"), r.stderr.decode())
        req = seen[0]
        self.assertEqual(req[3:8], b"\x01\x01\x00\x00\x01")        # REQUEST END_STREAM rsvd stream 1
        self.assertEqual(req[8:13], b"\x01\x00\x02/x")             # GET, path_len 2, "/x"
        self.assertEqual(req[13], 1)                               # host by static index


if __name__ == "__main__":
    unittest.main()
