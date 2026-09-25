"""End-to-end tests: a real server on an ephemeral port, real sockets.

    python -m unittest discover -s tests      (from part1-http11-calculator/)
"""

import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server  # noqa: E402


def start_server(idle=5.0):
    server.IDLE_TIMEOUT = idle
    listener = socket.create_server(("127.0.0.1", 0))
    threading.Thread(target=server.serve_forever, args=(listener, False), daemon=True).start()
    return listener, listener.getsockname()[1]


class Client:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.buf = b""

    def send(self, raw):
        self.sock.sendall(raw)

    def _fill(self):
        data = self.sock.recv(65536)
        if not data:
            raise ConnectionError("closed")
        self.buf += data

    def response(self):
        while b"\r\n\r\n" not in self.buf:
            self._fill()
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        lines = head.decode().split("\r\n")
        status = int(lines[0].split()[1])
        headers = {k.lower(): v.strip() for k, _, v in (l.partition(":") for l in lines[1:])}
        n = int(headers["content-length"])
        while len(self.buf) < n:
            self._fill()
        body, self.buf = self.buf[:n], self.buf[n:]
        return status, headers, body.decode()

    def get(self, path, extra=""):
        self.send(f"GET {path} HTTP/1.1\r\nHost: t\r\n{extra}\r\n".encode())
        return self.response()

    def closed_by_peer(self):
        try:
            return self.sock.recv(1) == b""
        except (ConnectionResetError, ConnectionAbortedError):
            return True

    def close(self):
        self.sock.close()


class CalculatorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.listener, cls.port = start_server()

    @classmethod
    def tearDownClass(cls):
        cls.listener.close()

    def setUp(self):
        self.c = Client(self.port)

    def tearDown(self):
        self.c.close()

    def test_brief_table_on_one_socket(self):
        cases = [
            ("/add?a=2&b=3", 200, "5"), ("/sub?a=10&b=4", 200, "6"),
            ("/mul?a=6&b=7", 200, "42"), ("/div?a=9&b=3", 200, "3"),
            ("/div?a=1&b=0", 400, None), ("/add?a=x&b=3", 400, None),
            ("/pow?a=2&b=8", 404, None),
        ]
        for path, want, body in cases:
            with self.subTest(path=path):
                status, headers, got = self.c.get(path)
                self.assertEqual(status, want)
                self.assertEqual(headers["connection"], "keep-alive")
                if body is not None:
                    self.assertEqual(got, body)
        self.c.send(b"POST /add HTTP/1.1\r\nHost: t\r\nContent-Length: 0\r\n\r\n")
        status, headers, _ = self.c.response()
        self.assertEqual(status, 405)
        self.assertIn("GET", headers["allow"])
        self.c.send(b"GET /add HTTP/1.1\r\n\r\n")
        self.assertEqual(self.c.response()[0], 400)
        self.assertEqual(self.c.get("/add?a=1&b=1")[2], "2", "connection must survive all of the above")

    def test_arithmetic_edges(self):
        for path, body in [("/div?a=1&b=4", "0.25"), ("/add?a=-2&b=0.5", "-1.5"),
                           ("/mul?a=1e3&b=2", "2000"), ("/sub?a=%2B5&b=7", "-2"),
                           ("/add?b=3&a=2", "5"), ("/mul?a=123456789012345678901&b=10",
                                                   "1234567890123456789010")]:
            with self.subTest(path=path):
                self.assertEqual(self.c.get(path)[:3:2], (200, body))
        for path in ["/add?a=1", "/add?a=1&b=", "/add?a=1&b=2&b=3", "/add?a=inf&b=1",
                     "/add?a=nan&b=1", "/div?a=0&b=0.0", "/mul?a=1e308&b=10"]:
            with self.subTest(path=path):
                self.assertEqual(self.c.get(path)[0], 400)

    def test_pipelining_answers_in_order(self):
        paths = [f"/add?a={i}&b=1" for i in range(20)]
        self.c.send(b"".join(f"GET {p} HTTP/1.1\r\nHost: t\r\n\r\n".encode() for p in paths))
        self.assertEqual([self.c.response()[2] for _ in paths], [str(i + 1) for i in range(20)])

    def test_request_split_across_many_segments(self):
        raw = b"GET /mul?a=6&b=7 HTTP/1.1\r\nHost: t\r\n\r\n"
        for byte in raw:
            self.c.send(bytes([byte]))
            time.sleep(0.001)
        self.assertEqual(self.c.response()[2], "42")

    def test_content_length_consumed_exactly(self):
        body = b"GET /pow?a=1&b=1 HTTP/1.1\r\nHost: t\r\n\r\n"      # looks like a request; is not
        self.c.send(b"POST /div HTTP/1.1\r\nHost: t\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
        self.assertEqual(self.c.response()[0], 405)
        self.assertEqual(self.c.get("/div?a=8&b=2")[2], "4")

    def test_chunked_body(self):
        self.c.send(b"PUT /sub HTTP/1.1\r\nHost: t\r\nTransfer-Encoding: chunked\r\n\r\n"
                    b"3\r\nabc\r\nA;name=val\r\n0123456789\r\n0\r\nTrailer: 1\r\n\r\n")
        self.assertEqual(self.c.response()[0], 405)
        self.assertEqual(self.c.get("/sub?a=3&b=1")[2], "2")

    def test_head(self):
        self.c.send(b"HEAD /add?a=2&b=3 HTTP/1.1\r\nHost: t\r\n\r\n")
        while b"\r\n\r\n" not in self.c.buf:
            self.c._fill()
        head, rest = self.c.buf.split(b"\r\n\r\n", 1)
        self.assertIn(b"Content-Length: 1", head)
        self.c.buf = rest
        self.assertEqual(self.c.get("/add?a=1&b=1")[2], "2", "HEAD must not have sent a body")

    def test_leading_blank_line_is_tolerated(self):
        self.c.send(b"\r\nGET /add?a=1&b=2 HTTP/1.1\r\nHost: t\r\n\r\n")
        self.assertEqual(self.c.response()[2], "3")

    def test_connection_close(self):
        status, headers, _ = self.c.get("/add?a=1&b=1", "Connection: close\r\n")
        self.assertEqual((status, headers["connection"]), (200, "close"))
        self.assertTrue(self.c.closed_by_peer())

    def test_http10(self):
        self.c.send(b"GET /add?a=1&b=1 HTTP/1.0\r\n\r\n")
        self.assertEqual(self.c.response()[0], 200)
        self.assertTrue(self.c.closed_by_peer())

    def test_http10_keep_alive_opt_in(self):
        self.c.send(b"GET /add?a=1&b=1 HTTP/1.0\r\nConnection: keep-alive\r\n\r\n")
        self.assertEqual(self.c.response()[1]["connection"], "keep-alive")
        self.assertEqual(self.c.get("/add?a=2&b=2")[2], "4")

    def test_malformed_closes(self):
        for raw in [b"NONSENSE\r\n\r\n", b"GET / HTTP/1.1\r\nBad Header\r\n\r\n",
                    b"GET / HTTP/1.1\r\nHost : t\r\n\r\n",
                    b"POST /add HTTP/1.1\r\nHost: t\r\nContent-Length: 3\r\nTransfer-Encoding: chunked\r\n\r\n",
                    b"POST /add HTTP/1.1\r\nHost: t\r\nContent-Length: -1\r\n\r\n",
                    b"POST /add HTTP/1.1\r\nHost: t\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\n"]:
            with self.subTest(raw=raw):
                c = Client(self.port)
                c.send(raw)
                self.assertEqual(c.response()[0], 400)
                self.assertTrue(c.closed_by_peer())
                c.close()

    def test_limits(self):
        self.c.send(b"GET /" + b"a" * 10000 + b" HTTP/1.1\r\nHost: t\r\n\r\n")
        self.assertEqual(self.c.response()[0], 431)
        c = Client(self.port)
        c.send(b"POST /add HTTP/1.1\r\nHost: t\r\nContent-Length: 99999999\r\n\r\n")
        self.assertEqual(c.response()[0], 413)
        c.close()

    def test_http2_rejected(self):
        self.c.send(b"GET / HTTP/2.0\r\nHost: t\r\n\r\n")
        self.assertEqual(self.c.response()[0], 505)

    def test_concurrent_connections(self):
        clients = [Client(self.port) for _ in range(10)]
        for i, c in enumerate(clients):
            c.send(f"GET /mul?a={i}&b={i} HTTP/1.1\r\nHost: t\r\n\r\n".encode())
        self.assertEqual([c.response()[2] for c in clients], [str(i * i) for i in range(10)])
        for c in clients:
            c.close()


class IdleTimeoutTest(unittest.TestCase):
    def test_idle_connection_is_closed(self):
        listener, port = start_server(idle=0.5)
        try:
            c = Client(port)
            self.assertEqual(c.get("/add?a=1&b=1")[2], "2")
            t = time.monotonic()
            self.assertTrue(c.closed_by_peer())
            self.assertLess(time.monotonic() - t, 3)
            c.close()
        finally:
            listener.close()
            server.IDLE_TIMEOUT = 5.0


if __name__ == "__main__":
    unittest.main()
