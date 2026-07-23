"""Tests for emrg.framing — length-prefixed IPC framing protocol."""

from __future__ import annotations

import asyncio
import struct
from unittest.mock import AsyncMock, MagicMock

from emrg.framing import (
    encode_frame, read_frame, write_frame,
    _MAX_FRAME_BYTES, _HEADER_SIZE, _HEADER_FMT,
)


class TestEncodeFrame:
    """Tests for encode_frame (synchronous, pure function)."""

    def test_empty_body(self) -> None:
        result = encode_frame(b"")
        assert len(result) == _HEADER_SIZE
        assert struct.unpack(_HEADER_FMT, result) == (0,)

    def test_small_body(self) -> None:
        body = b'{"type":"ping"}'
        result = encode_frame(body)
        assert len(result) == _HEADER_SIZE + len(body)
        assert struct.unpack(_HEADER_FMT, result[:_HEADER_SIZE]) == (len(body),)
        assert result[_HEADER_SIZE:] == body

    def test_large_body(self) -> None:
        body = b"x" * 65536  # 64 KB — exactly the old NDJSON limit
        result = encode_frame(body)
        assert len(result) == _HEADER_SIZE + 65536
        assert struct.unpack(_HEADER_FMT, result[:_HEADER_SIZE]) == (65536,)

    def test_max_frame(self) -> None:
        body = b"y" * _MAX_FRAME_BYTES
        result = encode_frame(body)
        assert len(result) == _HEADER_SIZE + _MAX_FRAME_BYTES


class TestReadFrame:
    """Tests for read_frame (async, run via asyncio.run)."""

    def test_normal_frame(self) -> None:
        body = b'{"type":"ping"}'
        frame = encode_frame(body)

        async def go():
            reader = AsyncMock()
            reader.readexactly = AsyncMock()
            reader.readexactly.side_effect = [frame[:_HEADER_SIZE], frame[_HEADER_SIZE:]]
            result = await read_frame(reader)
            assert result == body
            assert reader.readexactly.call_args_list == [
                ((_HEADER_SIZE,),),
                ((len(body),),),
            ]

        asyncio.run(go())

    def test_empty_body(self) -> None:
        frame = encode_frame(b"")

        async def go():
            reader = AsyncMock()
            reader.readexactly = AsyncMock()
            reader.readexactly.side_effect = [frame[:_HEADER_SIZE]]
            result = await read_frame(reader)
            assert result == b""

        asyncio.run(go())

    def test_eof_on_header(self) -> None:
        """EOF during header read → return None."""

        async def go():
            reader = AsyncMock()
            reader.readexactly = AsyncMock(
                side_effect=asyncio.IncompleteReadError(b"", 4)
            )
            result = await read_frame(reader)
            assert result is None

        asyncio.run(go())

    def test_eof_mid_body(self) -> None:
        """EOF partway through body read → return None."""
        body = b"x" * 100
        frame = encode_frame(body)

        async def go():
            reader = AsyncMock()
            reader.readexactly = AsyncMock()
            reader.readexactly.side_effect = [
                frame[:_HEADER_SIZE],
                asyncio.IncompleteReadError(body[:40], 100),
            ]
            result = await read_frame(reader)
            assert result is None

        asyncio.run(go())

    def test_size_exceeds_cap(self) -> None:
        """Declared length > _MAX_FRAME_BYTES → ValueError."""

        async def go():
            huge_header = struct.pack(_HEADER_FMT, _MAX_FRAME_BYTES + 1)
            reader = AsyncMock()
            reader.readexactly = AsyncMock(return_value=huge_header)
            try:
                await read_frame(reader)
                assert False, "expected ValueError"
            except ValueError as e:
                assert "too large" in str(e)

        asyncio.run(go())

    def test_exactly_at_cap(self) -> None:
        """Body exactly at _MAX_FRAME_BYTES → succeeds."""
        body = b"z" * _MAX_FRAME_BYTES
        frame = encode_frame(body)

        async def go():
            reader = AsyncMock()
            reader.readexactly = AsyncMock()
            reader.readexactly.side_effect = [frame[:_HEADER_SIZE], frame[_HEADER_SIZE:]]
            result = await read_frame(reader)
            assert result == body

        asyncio.run(go())


class TestWriteFrame:
    """Tests for write_frame (async, run via asyncio.run)."""

    def test_writes_and_drains(self) -> None:
        async def go():
            writer = MagicMock()
            writer.write = MagicMock()
            writer.drain = AsyncMock()
            body = b'{"type":"ping"}'
            await write_frame(writer, body)
            writer.write.assert_called_once_with(encode_frame(body))
            writer.drain.assert_awaited_once()

        asyncio.run(go())

    def test_empty_body(self) -> None:
        async def go():
            writer = MagicMock()
            writer.write = MagicMock()
            writer.drain = AsyncMock()
            await write_frame(writer, b"")
            writer.write.assert_called_once_with(encode_frame(b""))
            writer.drain.assert_awaited_once()

        asyncio.run(go())


class TestRoundtrip:
    """End-to-end encode → read_frame round-trip."""

    def test_rtt_small(self) -> None:
        body = b'{"type":"task","prompt":"hello"}'
        frame = encode_frame(body)

        async def go():
            reader = AsyncMock()
            reader.readexactly = AsyncMock()
            reader.readexactly.side_effect = [frame[:_HEADER_SIZE], frame[_HEADER_SIZE:]]
            result = await read_frame(reader)
            assert result == body

        asyncio.run(go())

    def test_rtt_large(self) -> None:
        body = b"x" * (128 * 1024)  # 128 KB — > old NDJSON limit
        frame = encode_frame(body)

        async def go():
            reader = AsyncMock()
            reader.readexactly = AsyncMock()
            reader.readexactly.side_effect = [frame[:_HEADER_SIZE], frame[_HEADER_SIZE:]]
            result = await read_frame(reader)
            assert result == body

        asyncio.run(go())

    def test_rtt_cjk(self) -> None:
        body = "你好世界！测试中文响应。".encode("utf-8")
        frame = encode_frame(body)

        async def go():
            reader = AsyncMock()
            reader.readexactly = AsyncMock()
            reader.readexactly.side_effect = [frame[:_HEADER_SIZE], frame[_HEADER_SIZE:]]
            result = await read_frame(reader)
            assert result == body

        asyncio.run(go())
