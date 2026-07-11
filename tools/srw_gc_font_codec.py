#!/usr/bin/env python3
"""Codec helpers for Super Robot Taisen GC's compressed font.pak.

The bitstream format was recovered from Start.dol's decoder at 0x80151E18:
one MSB-first flag bit, followed by either an 8-bit literal (flag 0) or a
12-bit ring index plus a 4-bit length-minus-three (flag 1).  The dictionary
is a 4096-byte zero-filled ring whose initial write position is 0xFEE.
"""

from __future__ import annotations

import argparse
import hashlib
import struct
from array import array
from dataclasses import dataclass
from pathlib import Path


RING_SIZE = 0x1000
MAX_MATCH = 18
MIN_MATCH = 3
INITIAL_WRITE = RING_SIZE - MAX_MATCH


class BitReader:
    def __init__(self, data: bytes):
        self.data = data
        self.bit_offset = 0

    def read(self, count: int) -> int:
        value = 0
        while count:
            byte_index = self.bit_offset >> 3
            if byte_index >= len(self.data):
                raise ValueError("truncated font bitstream")
            used = self.bit_offset & 7
            available = 8 - used
            take = min(count, available)
            shift = available - take
            bits = (self.data[byte_index] >> shift) & ((1 << take) - 1)
            value = (value << take) | bits
            self.bit_offset += take
            count -= take
        return value


class BitWriter:
    def __init__(self):
        self.output = bytearray()
        self.accumulator = 0
        self.count = 0

    def write(self, value: int, count: int) -> None:
        if value < 0 or value >= (1 << count):
            raise ValueError("bit value does not fit requested width")
        while count:
            take = min(count, 8 - self.count)
            shift = count - take
            self.accumulator = (self.accumulator << take) | (
                (value >> shift) & ((1 << take) - 1)
            )
            self.count += take
            count -= take
            if self.count == 8:
                self.output.append(self.accumulator)
                self.accumulator = 0
                self.count = 0

    def finish(self) -> bytes:
        if self.count:
            self.output.append(self.accumulator << (8 - self.count))
            self.accumulator = 0
            self.count = 0
        return bytes(self.output)


def decompress_stream(stream: bytes, output_size: int) -> bytes:
    bits = BitReader(stream)
    ring = bytearray(RING_SIZE)
    write_position = INITIAL_WRITE
    output = bytearray()
    while len(output) < output_size:
        if bits.read(1) == 0:
            value = bits.read(8)
            output.append(value)
            ring[write_position] = value
            write_position = (write_position + 1) & (RING_SIZE - 1)
            continue
        source_position = bits.read(12)
        length = bits.read(4) + MIN_MATCH
        for step in range(length):
            value = ring[(source_position + step) & (RING_SIZE - 1)]
            if len(output) < output_size:
                output.append(value)
            ring[write_position] = value
            write_position = (write_position + 1) & (RING_SIZE - 1)
    return bytes(output)


def _key(data: bytes, position: int) -> int:
    return (data[position] << 16) | (data[position + 1] << 8) | data[position + 2]


def compress_stream(data: bytes, max_chain: int = 256) -> bytes:
    """Greedy hash-chain encoder compatible with the game's decoder."""

    previous = array("i", [-1]) * len(data)
    heads: dict[int, int] = {}
    writer = BitWriter()
    position = 0

    def add_position(candidate: int) -> None:
        if candidate + MIN_MATCH > len(data):
            return
        key = _key(data, candidate)
        previous[candidate] = heads.get(key, -1)
        heads[key] = candidate

    while position < len(data):
        best_length = 0
        best_position = -1
        maximum = min(MAX_MATCH, len(data) - position)
        if maximum >= MIN_MATCH:
            candidate = heads.get(_key(data, position), -1)
            oldest = position - RING_SIZE
            checked = 0
            while candidate >= 0 and candidate >= oldest and checked < max_chain:
                length = MIN_MATCH
                while length < maximum and data[candidate + length] == data[position + length]:
                    length += 1
                if length > best_length:
                    best_length = length
                    best_position = candidate
                    if length == maximum:
                        break
                candidate = previous[candidate]
                checked += 1

        if best_length >= MIN_MATCH:
            writer.write(1, 1)
            writer.write((INITIAL_WRITE + best_position) & (RING_SIZE - 1), 12)
            writer.write(best_length - MIN_MATCH, 4)
            consumed = best_length
        else:
            writer.write(0, 1)
            writer.write(data[position], 8)
            consumed = 1
        for added in range(position, position + consumed):
            add_position(added)
        position += consumed
    return writer.finish()


@dataclass(frozen=True)
class FontPak:
    prefix: bytes
    name: str
    decompressed: bytes
    compressed: bytes


def read_font_pak(path: Path) -> FontPak:
    data = path.read_bytes()
    if len(data) < 16:
        raise ValueError("truncated outer PAK")
    count, name_length = struct.unpack_from("<II", data, 0)
    if count != 1:
        raise ValueError(f"expected one outer entry, got {count}")
    name_end = 8 + name_length
    name = data[8:name_end].decode("ascii")
    compressed_size, decompressed_size = struct.unpack_from("<II", data, name_end)
    stream_start = name_end + 8
    compressed = data[stream_start : stream_start + compressed_size]
    if stream_start + compressed_size != len(data):
        raise ValueError("outer compressed-size field does not match file length")
    decompressed = decompress_stream(compressed, decompressed_size)
    return FontPak(data[:name_end], name, decompressed, compressed)


def build_font_pak(template: FontPak, decompressed: bytes, max_chain: int = 256) -> bytes:
    compressed = compress_stream(decompressed, max_chain=max_chain)
    return b"".join(
        [
            template.prefix,
            struct.pack("<I", len(compressed)),
            struct.pack("<I", len(decompressed)),
            compressed,
        ]
    )


@dataclass(frozen=True)
class InnerEntry:
    index: int
    name: str
    offset: int
    size: int


def parse_inner_entries(data: bytes) -> list[InnerEntry]:
    if len(data) < 4:
        raise ValueError("truncated inner font container")
    count = struct.unpack_from(">I", data, 0)[0]
    table_end = 4 + count * 40
    if table_end > len(data):
        raise ValueError("inner font table exceeds payload")
    entries: list[InnerEntry] = []
    for index in range(count):
        position = 4 + index * 40
        raw_name = data[position : position + 32]
        name = raw_name.decode("ascii").lstrip(" ")
        offset, size = struct.unpack_from(">II", data, position + 32)
        if offset < table_end or offset + size > len(data):
            raise ValueError(f"invalid inner entry extent for {name}")
        entries.append(InnerEntry(index, name, offset, size))
    return entries


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("font_pak", type=Path)
    parser.add_argument("--recompress", type=Path, help="write a round-trip recompressed PAK")
    parser.add_argument("--max-chain", type=int, default=256)
    args = parser.parse_args()

    pak = read_font_pak(args.font_pak)
    entries = parse_inner_entries(pak.decompressed)
    print(f"name={pak.name}")
    print(f"compressed_size={len(pak.compressed)}")
    print(f"decompressed_size={len(pak.decompressed)}")
    print(f"decompressed_sha256={hashlib.sha256(pak.decompressed).hexdigest().upper()}")
    print(f"inner_entries={len(entries)}")
    for entry in entries:
        print(f"{entry.name}\toffset=0x{entry.offset:X}\tsize=0x{entry.size:X}")

    if args.recompress:
        if args.recompress.exists():
            raise RuntimeError(f"destination already exists: {args.recompress}")
        rebuilt = build_font_pak(pak, pak.decompressed, max_chain=args.max_chain)
        round_trip = read_font_pak_bytes(rebuilt)
        if round_trip.decompressed != pak.decompressed:
            raise RuntimeError("round-trip decompression mismatch")
        args.recompress.write_bytes(rebuilt)
        print(f"recompressed_size={len(rebuilt)}")
        print(f"recompressed_sha256={hashlib.sha256(rebuilt).hexdigest().upper()}")
    return 0


def read_font_pak_bytes(data: bytes) -> FontPak:
    count, name_length = struct.unpack_from("<II", data, 0)
    if count != 1:
        raise ValueError("unexpected outer entry count")
    name_end = 8 + name_length
    compressed_size, decompressed_size = struct.unpack_from("<II", data, name_end)
    stream_start = name_end + 8
    if stream_start + compressed_size != len(data):
        raise ValueError("outer compressed-size field mismatch")
    compressed = data[stream_start:]
    return FontPak(
        data[:name_end],
        data[8:name_end].decode("ascii"),
        decompress_stream(compressed, decompressed_size),
        compressed,
    )


if __name__ == "__main__":
    raise SystemExit(main())
