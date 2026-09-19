"""Extract message text from Messages' `attributedBody` column.

On modern macOS `message.text` is frequently NULL and the text only exists in
`attributedBody`: an NSAttributedString serialized with NSArchiver's legacy
"typedstream" format. The layout of the first object is:

    \\x04\\x0bstreamtyped ... NSAttributedString ... NSString \\x01 \\x94 \\x84 \\x01 +  <len> <utf-8 bytes>

`+` is the typedstream type code for a byte string. <len> uses typedstream's
integer encoding:

    0x00-0x7F  -> the value itself (one byte)
    0x81 (-127) -> next 2 bytes, little-endian signed int16
    0x82 (-126) -> next 4 bytes, little-endian signed int32

The length counts UTF-8 *bytes*, so emoji / non-ASCII are handled by slicing
bytes first and decoding afterwards. SQL `CAST(attributedBody AS TEXT)` is not
used anywhere: it stops at the first NUL byte.
"""

from __future__ import annotations

import re
import struct

_HEADER = b"streamtyped"
_STRING_CLASS = re.compile(rb"NS(?:Mutable)?String")
_STRING_TYPE = b"\x01+"  # type-string of length 1 containing "+"
OBJECT_REPLACEMENT = "￼"  # stands in for attachments inside the text


class TypedStreamError(ValueError):
    pass


def _read_length(buf: bytes, pos: int) -> tuple[int, int]:
    """Return (length, position of first payload byte)."""
    if pos >= len(buf):
        raise TypedStreamError("truncated length")
    tag = buf[pos]
    if tag == 0x81:
        if pos + 3 > len(buf):
            raise TypedStreamError("truncated int16 length")
        return struct.unpack_from("<h", buf, pos + 1)[0], pos + 3
    if tag == 0x82:
        if pos + 5 > len(buf):
            raise TypedStreamError("truncated int32 length")
        return struct.unpack_from("<i", buf, pos + 1)[0], pos + 5
    if tag < 0x80:
        return tag, pos + 1
    raise TypedStreamError(f"unsupported length tag 0x{tag:02x}")


def _string_at(buf: bytes, type_pos: int) -> str | None:
    try:
        length, start = _read_length(buf, type_pos + len(_STRING_TYPE))
    except TypedStreamError:
        return None
    end = start + length
    if length < 0 or end > len(buf):
        return None
    try:
        return buf[start:end].decode("utf-8")
    except UnicodeDecodeError:
        return None


def decode_attributed_body(blob: bytes | None) -> str | None:
    """Return the plain text of an attributedBody blob, or None if it has none."""
    if not blob:
        return None
    buf = bytes(blob)
    if _HEADER not in buf[:32]:
        raise TypedStreamError("not a typedstream blob")

    # Primary path: first `+` string after the NSString class definition. The
    # string payload is the first thing NSAttributedString archives.
    m = _STRING_CLASS.search(buf)
    if m:
        pos = buf.find(_STRING_TYPE, m.end())
        if pos != -1:
            text = _string_at(buf, pos)
            if text is not None:
                return text

    # Fallback: try every `+` marker and keep the first that decodes cleanly.
    pos = buf.find(_STRING_TYPE)
    while pos != -1:
        text = _string_at(buf, pos)
        if text:
            return text
        pos = buf.find(_STRING_TYPE, pos + 1)
    raise TypedStreamError("no string payload found")


def message_text(text: str | None, attributed_body: bytes | None) -> str | None:
    """Best available text for a message row, attachments shown as [attachment]."""
    out = text
    if not out and attributed_body:
        try:
            out = decode_attributed_body(attributed_body)
        except TypedStreamError:
            out = None
    if out is None:
        return None
    out = out.replace(OBJECT_REPLACEMENT, "[attachment]").strip()
    return out or None
