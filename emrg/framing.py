"""Length-prefixed framing for EMRG IPC protocol.

Replaces NDJSON newline-delimited framing with a 4-byte big-endian
length prefix + raw body. This eliminates the 64KB asyncio readline()
buffer limit that caused LimitOverrunError on large messages.

Write: 4-byte big-endian unsigned int (body length) + body bytes
Read:  read exactly 4 bytes → decode length → read exactly N bytes
"""

from __future__ import annotations

import asyncio
import struct

_HEADER_FMT = ">I"
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)

# 16MB sanity cap to prevent memory attacks
_MAX_FRAME_BYTES = 16 * 1024 * 1024


async def read_frame(reader: asyncio.StreamReader) -> bytes | None:
    """Read a single length-prefixed frame.

    Returns the raw body bytes, or None if the stream ended cleanly.
    Raises ValueError if the declared length exceeds the sanity cap.
    """
    try:
        header = await reader.readexactly(_HEADER_SIZE)
    except asyncio.IncompleteReadError:
        return None
    body_len = struct.unpack(_HEADER_FMT, header)[0]
    if body_len > _MAX_FRAME_BYTES:
        raise ValueError(
            f"frame too large: {body_len} bytes (max {_MAX_FRAME_BYTES})"
        )
    if body_len == 0:
        return b""
    try:
        return await reader.readexactly(body_len)
    except asyncio.IncompleteReadError:
        return None


def encode_frame(data: bytes) -> bytes:
    """Encode raw bytes as a length-prefixed frame."""
    return struct.pack(_HEADER_FMT, len(data)) + data


async def write_frame(writer: asyncio.StreamWriter, data: bytes) -> None:
    """Write a length-prefixed frame and drain."""
    writer.write(encode_frame(data))
    await writer.drain()
