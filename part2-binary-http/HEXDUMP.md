# Annotated hexdump — one complete request and response

Captured from a real run, bytes unedited:

```
$ ./bserve ./www 9000 &
$ ./bcurl -v localhost:9000/hello.txt
```

One TCP connection carries four frames: REQUEST →, RESPONSE ←, DATA ←, then bcurl's GOAWAY →.
Offsets are from the start of each frame. Section numbers (§) refer to [SPEC.md](SPEC.md).

---

## 1. Client → server: REQUEST (54 octets = 8 header + 46 payload)

```
00000000  00 00 2e 01 01 00 00 01  01 00 0a 2f 68 65 6c 6c  |.........../hell|
00000010  6f 2e 74 78 74 01 00 0e  6c 6f 63 61 6c 68 6f 73  |o.txt...localhos|
00000020  74 3a 39 30 30 30 02 00  07 62 63 75 72 6c 2f 31  |t:9000...bcurl/1|
00000030  03 00 03 2a 2f 2a                                 |...*/*|
```

| Offset | Bytes | Field | Meaning |
|---|---|---|---|
| **00–07** | | **frame header (§2)** | |
| 00–02 | `00 00 2e` | Length u24 | 46 octets of payload follow the header |
| 03 | `01` | Type | REQUEST |
| 04 | `01` | Flags | END_STREAM: there is no request body, so this one frame is the whole request |
| 05 | `00` | Reserved | always 0 |
| 06–07 | `00 01` | Stream ID u16 | stream 1, the first request on this connection |
| **08–35** | | **REQUEST payload (§3)** | |
| 08 | `01` | method u8 | GET |
| 09–0a | `00 0a` | path_len u16 | 10 |
| 0b–14 | `2f 68 65 6c 6c 6f 2e 74 78 74` | path | `/hello.txt` |
| | | **header block (§4)**, runs to the end of the payload | |
| 15 | `01` | name_ref | static #1 = `host` |
| 16–17 | `00 0e` | value_len | 14 |
| 18–25 | `6c 6f … 30 30` | value | `localhost:9000` |
| 26 | `02` | name_ref | static #2 = `user-agent` |
| 27–28 | `00 07` | value_len | 7 |
| 29–2f | `62 63 75 72 6c 2f 31` | value | `bcurl/1` |
| 30 | `03` | name_ref | static #3 = `accept` |
| 31–32 | `00 03` | value_len | 3 |
| 33–35 | `2a 2f 2a` | value | `*/*` |

Check: 1 + 2 + 10 (method, path) + 17 + 10 + 6 (three headers) = **46** = `0x2e`. ✓
The header block has no count and no terminator: the server stops at octet 0x35 because
Length says the payload ends there.

---

## 2. Server → client: RESPONSE (142 octets = 8 + 134)

```
00000000  00 00 86 02 00 00 00 01  00 c8 04 00 19 74 65 78  |.............tex|
00000010  74 2f 70 6c 61 69 6e 3b  20 63 68 61 72 73 65 74  |t/plain; charset|
00000020  3d 75 74 66 2d 38 05 00  02 32 31 07 00 08 62 73  |=utf-8...21...bs|
00000030  65 72 76 65 2f 31 08 00  1d 46 72 69 2c 20 32 35  |erve/1...Fri, 25|
00000040  20 53 65 70 20 32 30 32  36 20 31 35 3a 33 37 3a  | Sep 2026 15:37:|
00000050  31 39 20 47 4d 54 09 00  1d 46 72 69 2c 20 32 35  |19 GMT...Fri, 25|
00000060  20 53 65 70 20 32 30 32  36 20 31 34 3a 34 31 3a  | Sep 2026 14:41:|
00000070  33 36 20 47 4d 54 0a 00  15 22 31 35 2d 31 38 64  |36 GMT..."15-18d|
00000080  38 39 37 36 31 36 66 63  62 64 36 35 34 22        |897616fcbd654"|
```

| Offset | Bytes | Field | Meaning |
|---|---|---|---|
| 00–02 | `00 00 86` | Length | 134 |
| 03 | `02` | Type | RESPONSE |
| 04 | `00` | Flags | END_STREAM **not** set: DATA frames follow |
| 05 | `00` | Reserved | 0 |
| 06–07 | `00 01` | Stream ID | 1, the answer to the request above |
| 08–09 | `00 c8` | status u16 | 200 |
| 0a | `04` | name_ref | #4 `content-type` |
| 0b–0c | `00 19` | value_len | 25 |
| 0d–25 | `74 65 … 2d 38` | value | `text/plain; charset=utf-8` |
| 26 | `05` | name_ref | #5 `content-length` |
| 27–28 | `00 02` | value_len | 2 |
| 29–2a | `32 31` | value | `21`, so the client expects exactly 21 octets of DATA |
| 2b | `07` | name_ref | #7 `server` |
| 2c–2d | `00 08` | value_len | 8 |
| 2e–35 | `62 73 … 2f 31` | value | `bserve/1` |
| 36 | `08` | name_ref | #8 `date` |
| 37–38 | `00 1d` | value_len | 29 |
| 39–55 | `46 72 … 4d 54` | value | `Fri, 25 Sep 2026 15:37:19 GMT` |
| 56 | `09` | name_ref | #9 `last-modified` |
| 57–58 | `00 1d` | value_len | 29 |
| 59–75 | `46 72 … 4d 54` | value | `Fri, 25 Sep 2026 14:41:36 GMT` |
| 76 | `0a` | name_ref | #10 `etag` |
| 77–78 | `00 15` | value_len | 21 |
| 79–8d | `22 31 … 34 22` | value | `"15-18d897616fcbd654"`: size 0x15 = 21, then the file mtime in ns (hex) |

Check: 2 + (3+25) + (3+2) + (3+8) + (3+29) + (3+29) + (3+21) = **134** = `0x86`. ✓
Six header names, and not one name string sent: every name came from the static table.

---

## 3. Server → client: DATA (29 octets = 8 + 21)

```
00000000  00 00 15 03 01 00 00 01  48 65 6c 6c 6f 2c 20 62  |........Hello, b|
00000010  69 6e 61 72 79 20 77 6f  72 6c 64 21 0a           |inary world!.|
```

| Offset | Bytes | Field | Meaning |
|---|---|---|---|
| 00–02 | `00 00 15` | Length | 21, which matches `content-length: 21` |
| 03 | `03` | Type | DATA |
| 04 | `01` | Flags | END_STREAM: the response is complete |
| 05 | `00` | Reserved | 0 |
| 06–07 | `00 01` | Stream ID | 1 |
| 08–1c | `48 65 … 21 0a` | body | `Hello, binary world!\n`, the file's bytes, which bcurl writes to stdout |

**Where does this response end and the next begin?** At octet 0x1c of this frame. The client
knows that without reading ahead, because Length (0x15) says how many octets follow and
END_STREAM says no more frames belong to stream 1. The next octet on the wire belongs to the
next frame, and the connection is still open.

---

## 4. Client → server: GOAWAY (12 octets = 8 + 4) — closing cleanly

```
00000000  00 00 04 04 00 00 00 00  00 01 00 00              |............|
```

| Offset | Bytes | Field | Meaning |
|---|---|---|---|
| 00–02 | `00 00 04` | Length | 4 |
| 03 | `04` | Type | GOAWAY |
| 04–05 | `00 00` | Flags, Reserved | none, 0 |
| 06–07 | `00 00` | Stream ID | 0, a connection-level frame |
| 08–09 | `00 01` | last_stream | 1, the last response bcurl consumed |
| 0a–0b | `00 00` | code | NO_ERROR |

The server log for the connection confirms it: `#1 stream=1 GET /hello.txt -> 200`, then
`client sent GOAWAY`, then `closed after 1 response(s)`.

---

## Size compared with the same exchange in HTTP/1.1

| | BHTTP/1 | HTTP/1.1 text |
|---|---|---|
| request | 54 octets | 83 octets |
| response (headers + body) | 142 + 29 = 171 octets | 231 octets |

The saving is small for one request. What the binary form really buys is that every boundary
is announced before it arrives: no scanning for `CRLF CRLF`, no chunk-size parsing, and one
rule for skipping anything unknown.
