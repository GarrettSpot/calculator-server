# BHTTP/1 — HTTP semantics in binary frames

*Status: version 1. Everything a stranger needs to write an interoperable client or server.*
The keywords MUST, MUST NOT, SHOULD and MAY are used as in RFC 2119.
All integers are **unsigned, big-endian** (network byte order). "u8/u16/u24" means an unsigned
integer of 1/2/3 octets.

## 1. Connection

A client opens one TCP connection and sends requests. The server answers each request **in the
order it was received**, and the connection stays open. A client MAY pipeline: it may send
request N+1 before response N arrives. There is no preface or handshake. The first octet the
client sends is the first octet of a frame. Everything on the wire, in both directions, is a
sequence of frames.

## 2. Frame header — fixed 8 octets

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-----------------------------------------------+---------------+
|                  Length (24)                  |   Type (8)    |
+---------------+---------------+---------------+---------------+
|   Flags (8)   | Reserved (8)  |         Stream ID (16)        |
+---------------+---------------+-------------------------------+
|                  Payload (Length octets) ...                  |
```

| Field | Width | Meaning, and why this width |
|---|---|---|
| Length | 24 | Octets of payload after the header (the header itself is not counted). Length comes **first**, so a receiver that understands nothing else can still find the next frame. 16 bits would cap every frame at 65 535 forever. 24 bits is HTTP/2's choice: v1 uses at most 64 KiB, and a v2 can raise the limit to 16 MiB−1 without changing the header layout. |
| Type | 8 | What the payload is (§3). v1 defines 4 types, so 252 are free for extensions, and §6 makes unknown types safe to send. |
| Flags | 8 | Bit 0x01 = **END_STREAM**: this is the last frame of this request or response. Other bits are reserved: send 0, ignore on receipt. |
| Reserved | 8 | MUST be sent as 0 and MUST be ignored on receipt. HTTP/2 spent 1 reserved bit (the R bit) and was left with a 31-bit stream ID. We spend a whole octet instead: it keeps every field byte-aligned and makes the header exactly 64 bits. It also gives v2 a place for a version number or priority. |
| Stream ID | 16 | Ties a response to its request. HTTP/2 needs 31 bits because it multiplexes and never reuses an ID. We answer in order, so an ID only has to be unique among the requests in flight: 65 535 of them is far more than any client pipelines. 0 means "the connection itself" (GOAWAY). |

The header is 8 octets against HTTP/2's 9, and every field sits on a byte boundary.
The only multi-octet read that is not 16 bits is the 24-bit length.

**Frame size.** In v1 a sender MUST NOT send a frame of a defined type (§3) with Length > 65 536.
A receiver MUST accept frames up to that size. A larger frame of a *defined* type is a connection
error (FRAME_TOO_LARGE). A frame of an *unknown* type may have any length (§6).

## 3. Frame types

| Type | Name | Stream | Payload |
|---|---|---|---|
| 0x01 | REQUEST | ≥ 1 | `method u8`, `path_len u16`, `path` (path_len octets, UTF-8), then a header block (§4) that fills the rest of the payload |
| 0x02 | RESPONSE | = request's | `status u16` (an HTTP status code), then a header block that fills the rest |
| 0x03 | DATA | = its message's | body octets (0 or more) |
| 0x04 | GOAWAY | 0 | `last_stream u16` (the last request processed, or 0), `code u16`, then optional UTF-8 debug text |

**Methods:** 0x01 GET, 0x02 HEAD, 0x03 POST, 0x04 PUT, 0x05 DELETE, 0x06 OPTIONS. Any other value is
a method the server does not support (405).
**Path:** raw UTF-8 and MUST begin with `/`. It is *not* percent-encoded, because the length prefix
already delimits it. A `?query` suffix MAY be present.
**GOAWAY codes:** 0 NO_ERROR, 1 PROTOCOL_ERROR, 2 FRAME_TOO_LARGE, 3 IDLE_TIMEOUT. Unknown codes are
treated as PROTOCOL_ERROR.

## 4. Header block — ten numbered names, everything else length-prefixed

A header block is a sequence of *entries* that runs to the end of the frame payload. It needs no
count, because the frame length already bounds it. Each entry is:

```
name_ref u8 ─┬─ 0x00        → name_len u8, name (name_len octets, lower-case ASCII, 1..255)
             ├─ 0x01..0x0A  → a name from the static table below
             └─ 0x0B..0xFF  → reserved: the receiver MUST skip this entry (see value)
value_len u16, value (value_len octets)          ← always present, whatever name_ref was
```

| # | name | # | name |
|---|---|---|---|
| 1 | host | 6 | connection |
| 2 | user-agent | 7 | server |
| 3 | accept | 8 | date |
| 4 | content-type | 9 | last-modified |
| 5 | content-length | 10 | etag |

These are the ten names our programs actually send, so a typical message carries no name strings
at all. Every entry has the same value format, whatever its name_ref, so a receiver can always
step over an entry it does not understand. That lets a v2 extend the table without breaking v1
peers. A sender SHOULD use the index when a name is in the table. A receiver MUST accept a
table name sent as a literal. Header names are case-insensitive, and literal names MUST be sent
lower-case.

## 5. Messages

* **Request** = one REQUEST frame, then zero or more DATA frames. END_STREAM is set on the last of
  them, so a GET with no body is a single REQUEST frame with END_STREAM. A request MUST include
  `host`. In v1 a client MUST send all the frames of one request before starting the next. Frames
  of different streams are never interleaved.
* **Response** = one RESPONSE frame, then zero or more DATA frames, with END_STREAM on the last.
  If `content-length` is present, the DATA payloads MUST add up to exactly that many octets. A
  response to HEAD is a RESPONSE frame with END_STREAM whose `content-length` describes the body
  it would have sent.
* **Order.** The server MUST send response frames in request order and MUST NOT interleave two
  responses. A client SHOULD check that each response's stream ID is the one it expects.
* **Stream IDs** start at 1 and go up by 1 for each request. After 65 535 they wrap to 1. An ID
  MUST NOT be reused while its request is still in flight.

## 6. Errors, unknown things, and closing

* **Unknown frame type: skip it.** A receiver that meets a frame type it does not know MUST read
  and discard exactly `Length` octets of payload, and then carry on with the next frame as if
  nothing had happened. It MUST NOT treat the frame as an error. The size limit does not apply,
  so a receiver SHOULD discard the payload as it arrives rather than buffer it. This rule is what
  leaves room for a version 2.
* **Unknown flags, a non-zero Reserved octet, an unknown header index** → ignored, as above.
* **Stream error.** The frame is well delimited but its payload is wrong: fields are truncated,
  `path_len` runs past the payload, the path does not start with `/`, the UTF-8 is invalid, or
  `host` is missing. The server answers **400** on that stream, and the **connection stays
  open**, because Length told us exactly where the next frame starts. A missing file is **404**.
  A path that escapes the served root is also 404, so we don't reveal what is outside. An
  unsupported method is **405**.
* **Connection error.** The frame boundaries themselves can no longer be trusted: a defined type
  is over the size limit, REQUEST has stream 0, a new REQUEST arrives before the previous one's
  END_STREAM, or a server receives RESPONSE. The detector sends
  `GOAWAY(last_stream, code)` and closes the connection.
* **Closing.** Either side MAY close at a frame boundary. It SHOULD first send `GOAWAY(NO_ERROR)`,
  after which it MUST NOT send any further requests or responses. If a request carries the header
  `connection: close`, the server sends the response, then GOAWAY, and closes. A server SHOULD
  close a connection that has been idle for a while. It sends `GOAWAY(IDLE_TIMEOUT)` first; our
  server waits 30 s. EOF in the middle of a frame is a connection error, and EOF between frames
  is a normal close.

## 7. Worked example (the full annotated capture is in HEXDUMP.md)

`GET /hello.txt` with host `localhost:9000`, stream 1:
```
00 00 1e 01 01 00 00 01   len=30 REQUEST END_STREAM rsvd=0 stream=1
01 00 0a 2f 68 65 6c 6c 6f 2e 74 78 74    GET, path_len=10, "/hello.txt"
01 00 0e 6c 6f 63 61 6c 68 6f 73 74 3a 39 30 30 30    [1]host, len=14, "localhost:9000"
```
