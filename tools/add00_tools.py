#!/usr/bin/env python3
"""Inspect and safely patch ``add00dat.bin`` graphic-text assets.

``add00dat.bin`` is a 0x20-aligned container whose first big-endian pointer
equals the byte size of its absolute-offset table.  The retail and public
English files both contain 3,920 blocks in the same order.  Localized labels
are not CP932 strings: they are primarily GX I4 bitmaps (``BMP\x06``), with
``SCR\0`` blocks acting as tile maps.

This module deliberately uses the English file as the topology template.  A
replacement bitmap must have exactly the same dimensions as its English
counterpart, so only the I4 pixel payload changes; the file length, outer
pointer table, block boundaries, and every non-bitmap byte remain identical.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from PIL import Image, ImageOps


ALIGNMENT = 0x20
BMP_I4 = b"BMP\x06"


class Add00FormatError(ValueError):
    """Raised when an add00 structural invariant is violated."""


@dataclass(frozen=True)
class Add00Container:
    source: bytes
    offsets: tuple[int, ...]
    blocks: tuple[bytes, ...]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def _read_source(source: Path | str | bytes | bytearray | memoryview) -> bytes:
    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source)
    return Path(source).read_bytes()


def parse_container(source: Path | str | bytes | bytearray | memoryview) -> Add00Container:
    data = _read_source(source)
    if len(data) < 4:
        raise Add00FormatError("file is too small for an offset table")
    table_size = struct.unpack_from(">I", data, 0)[0]
    if table_size == 0 or table_size % 4:
        raise Add00FormatError(f"invalid table size 0x{table_size:X}")
    if table_size > len(data):
        raise Add00FormatError("offset table extends past EOF")
    count = table_size // 4
    offsets = tuple(struct.unpack_from(f">{count}I", data, 0))
    if offsets[0] != table_size:
        raise Add00FormatError("first block does not start after pointer table")
    if any(offset % ALIGNMENT for offset in offsets):
        raise Add00FormatError("one or more blocks are not 0x20 aligned")
    if list(offsets) != sorted(offsets) or len(set(offsets)) != len(offsets):
        raise Add00FormatError("block offsets are not strictly increasing")
    if offsets[-1] >= len(data):
        raise Add00FormatError("last block starts at or after EOF")
    blocks = tuple(
        data[offset : offsets[index + 1] if index + 1 < count else len(data)]
        for index, offset in enumerate(offsets)
    )
    return Add00Container(data, offsets, blocks)


def i4_dimensions(block: bytes) -> tuple[int, int]:
    if block[:4] != BMP_I4 or len(block) < 32:
        raise Add00FormatError("block is not a BMP6/I4 bitmap")
    width, height = struct.unpack_from(">II", block, 8)
    if width == 0 or height == 0 or width % 8 or height % 8:
        raise Add00FormatError(f"invalid I4 dimensions {width}x{height}")
    expected = 32 + width * height // 2
    # A handful of English-patch blocks retain one extra 0x20 padding unit.
    # It is outside the raster payload and must be preserved byte-for-byte.
    if len(block) < expected or (len(block) - expected) % ALIGNMENT:
        raise Add00FormatError(
            f"I4 block length {len(block)} is incompatible with {width}x{height} "
            f"(minimum {expected})"
        )
    return width, height


def decode_i4(block: bytes) -> Image.Image:
    """Decode a GameCube GX I4 bitmap (8x8 tiled, high nibble first)."""

    width, height = i4_dimensions(block)
    output = Image.new("L", (width, height))
    pixels = output.load()
    cursor = 32
    for tile_y in range(0, height, 8):
        for tile_x in range(0, width, 8):
            for y in range(8):
                for x in range(0, 8, 2):
                    value = block[cursor]
                    cursor += 1
                    pixels[tile_x + x, tile_y + y] = (value >> 4) * 17
                    pixels[tile_x + x + 1, tile_y + y] = (value & 0x0F) * 17
    return output


def encode_i4(image: Image.Image, template_block: bytes) -> bytes:
    """Encode an image without changing the template header or block size."""

    width, height = i4_dimensions(template_block)
    image = image.convert("L")
    if image.size != (width, height):
        raise Add00FormatError(
            f"replacement image {image.size} does not match template {(width, height)}"
        )
    pixels = image.load()
    payload = bytearray()
    for tile_y in range(0, height, 8):
        for tile_x in range(0, width, 8):
            for y in range(8):
                for x in range(0, 8, 2):
                    high = max(0, min(15, (pixels[tile_x + x, tile_y + y] + 8) // 17))
                    low = max(0, min(15, (pixels[tile_x + x + 1, tile_y + y] + 8) // 17))
                    payload.append((high << 4) | low)
    raster_end = 32 + width * height // 2
    result = template_block[:32] + bytes(payload) + template_block[raster_end:]
    if len(result) != len(template_block):
        raise Add00FormatError("encoded bitmap unexpectedly changed block length")
    return result


def render_scr_tilemap(bitmap: Image.Image, scr_block: bytes) -> Image.Image:
    """Apply a ``SCR\0`` u16 tile map to a decoded 8x8-tiled bitmap.

    Retail story/location bitmaps are glyph atlases rather than readable
    raster lines.  Their immediately following SCR block maps those glyph
    tiles into the actual on-screen Japanese title.  The lower ten bits are
    the tile index and bits 10/11 are horizontal/vertical flip flags.
    """

    if scr_block[:4] != b"SCR\0" or len(scr_block) < 32 or (len(scr_block) - 32) % 2:
        raise Add00FormatError("block is not a supported SCR u16 tile map")
    width_tiles, height_tiles = struct.unpack_from(">II", scr_block, 4)
    if not width_tiles or not height_tiles:
        raise Add00FormatError("SCR tile map has zero dimensions")
    entries = struct.unpack_from(f">{(len(scr_block) - 32) // 2}H", scr_block, 32)
    # Some English assets retain an unused trailing layer; retail maps used
    # for Japanese extraction match the declared height.  Ignore trailing
    # entries instead of allowing them to distort the visible canvas.
    visible_count = width_tiles * height_tiles
    if len(entries) < visible_count:
        raise Add00FormatError("SCR tile map is shorter than its declared canvas")
    entries = entries[:visible_count]
    output = Image.new("L", (width_tiles * 8, height_tiles * 8))
    tiles_x = bitmap.width // 8
    tiles_y = bitmap.height // 8
    tile_count = tiles_x * tiles_y
    for position, value in enumerate(entries):
        tile_index = value & 0x03FF
        if tile_index >= tile_count:
            continue
        source_x = (tile_index % tiles_x) * 8
        source_y = (tile_index // tiles_x) * 8
        tile = bitmap.crop((source_x, source_y, source_x + 8, source_y + 8))
        if value & 0x0400:
            tile = tile.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        if value & 0x0800:
            tile = tile.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        destination_x = (position % width_tiles) * 8
        destination_y = (position // width_tiles) * 8
        output.paste(tile, (destination_x, destination_y))
    return output


def changed_blocks(original: Add00Container, english: Add00Container) -> list[int]:
    if len(original.blocks) != len(english.blocks):
        raise Add00FormatError("retail and English block counts differ")
    return [index for index, (left, right) in enumerate(zip(original.blocks, english.blocks)) if left != right]


def extract_graphic_candidates(
    original_source: Path | str,
    english_source: Path | str,
    output_dir: Path | str,
) -> list[dict[str, object]]:
    """Render all changed BMP6 blocks and return stable graphic records."""

    original = parse_container(original_source)
    english = parse_container(english_source)
    output = Path(output_dir)
    original_dir = output / "original"
    english_dir = output / "english"
    ocr_dir = output / "ocr"
    mapped_dir = output / "original_mapped"
    japanese_ocr_dir = output / "ocr_japanese"
    for directory in (original_dir, english_dir, ocr_dir, mapped_dir, japanese_ocr_dir):
        directory.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, object]] = []
    for index in changed_blocks(original, english):
        left, right = original.blocks[index], english.blocks[index]
        if left[:4] != BMP_I4 or right[:4] != BMP_I4:
            continue
        original_image = decode_i4(left)
        english_image = decode_i4(right)
        original_path = original_dir / f"add00_{index:04d}_ja.png"
        english_path = english_dir / f"add00_{index:04d}_en.png"
        ocr_path = ocr_dir / f"add00_{index:04d}_en_ocr.png"
        original_image.save(original_path)
        english_image.save(english_path)

        # Windows OCR is substantially more reliable on dark text over white.
        # Nearest-neighbour scaling preserves the game's pixel glyph edges.
        max_dimension = max(english_image.size)
        scale = max(1, min(6, 2400 // max_dimension))
        prepared = ImageOps.invert(english_image).resize(
            (english_image.width * scale, english_image.height * scale),
            Image.Resampling.NEAREST,
        )
        prepared = ImageOps.expand(prepared, border=max(8, scale * 4), fill=255)
        prepared.save(ocr_path)

        mapped_path = ""
        japanese_ocr_path = ""
        tilemap_block: int | None = None
        map_dimensions: list[int] | None = None
        if index + 1 < len(original.blocks) and original.blocks[index + 1][:4] == b"SCR\0":
            try:
                mapped = render_scr_tilemap(original_image, original.blocks[index + 1])
                mapped_path_obj = mapped_dir / f"add00_{index:04d}_ja_mapped.png"
                japanese_ocr_path_obj = japanese_ocr_dir / f"add00_{index:04d}_ja_ocr.png"
                mapped.save(mapped_path_obj)
                mapped_scale = max(1, min(6, 2400 // max(mapped.size)))
                mapped_prepared = ImageOps.invert(mapped).resize(
                    (mapped.width * mapped_scale, mapped.height * mapped_scale),
                    Image.Resampling.NEAREST,
                )
                mapped_prepared = ImageOps.expand(
                    mapped_prepared, border=max(8, mapped_scale * 4), fill=255
                )
                mapped_prepared.save(japanese_ocr_path_obj)
                mapped_path = str(mapped_path_obj)
                japanese_ocr_path = str(japanese_ocr_path_obj)
                tilemap_block = index + 1
                map_dimensions = list(mapped.size)
            except Add00FormatError:
                pass

        ow, oh = original_image.size
        ew, eh = english_image.size
        zero_ratio = sum(1 for value in english_image.getdata() if value == 0) / (ew * eh)
        records.append(
            {
                "id": f"add00:graphic:{index:04d}",
                "container": "add00",
                "block_index": index,
                "block_type": "BMP6_GX_I4",
                "original_dimensions": [ow, oh],
                "english_dimensions": [ew, eh],
                "original_block_sha256": sha256(left),
                "english_block_sha256": sha256(right),
                "english_zero_pixel_ratio": round(zero_ratio, 6),
                "original_preview": str(original_path),
                "english_preview": str(english_path),
                "ocr_input": str(ocr_path),
                "ocr_scale": scale,
                "original_tilemap_block": tilemap_block,
                "mapped_original_preview": mapped_path,
                "japanese_ocr_input": japanese_ocr_path,
                "mapped_dimensions": map_dimensions,
                "english_ocr": "",
                "japanese_graphic_text": "",
                "final_korean": "",
                "patch_status": "pending_ocr",
            }
        )
    return records


def patch_images(
    english_source: Path | str,
    replacements: Mapping[int, Path | str | Image.Image],
) -> bytes:
    """Return a fixed-layout English container with selected I4 pixels changed."""

    container = parse_container(english_source)
    output = bytearray(container.source)
    for index, image_source in sorted(replacements.items()):
        if index < 0 or index >= len(container.blocks):
            raise Add00FormatError(f"block index out of range: {index}")
        block = container.blocks[index]
        if isinstance(image_source, Image.Image):
            image = image_source
        else:
            image = Image.open(image_source)
        replacement = encode_i4(image, block)
        start = container.offsets[index]
        output[start : start + len(block)] = replacement

    result = bytes(output)
    verify_fixed_layout(container.source, result)
    return result


def verify_fixed_layout(template_source: Path | str | bytes, result_source: Path | str | bytes) -> dict[str, object]:
    template = parse_container(template_source)
    result = parse_container(result_source)
    if len(template.source) != len(result.source):
        raise Add00FormatError("patched file length differs from English template")
    if template.offsets != result.offsets:
        raise Add00FormatError("patched outer pointer table differs from English template")
    if len(template.blocks) != len(result.blocks):
        raise Add00FormatError("patched block count differs from English template")
    changed: list[int] = []
    for index, (left, right) in enumerate(zip(template.blocks, result.blocks)):
        if left == right:
            continue
        changed.append(index)
        if left[:4] != BMP_I4 or right[:4] != BMP_I4:
            raise Add00FormatError(f"non-BMP6 block {index} was modified")
        if left[:32] != right[:32] or len(left) != len(right):
            raise Add00FormatError(f"BMP6 header/size changed in block {index}")
        if i4_dimensions(left) != i4_dimensions(right):
            raise Add00FormatError(f"BMP6 dimensions changed in block {index}")
    return {
        "size": len(result.source),
        "block_count": len(result.blocks),
        "outer_offsets_identical": template.offsets == result.offsets,
        "changed_block_count": len(changed),
        "changed_blocks": changed,
        "template_sha256": sha256(template.source),
        "result_sha256": sha256(result.source),
    }


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    extract = sub.add_parser("extract", help="render changed BMP6 graphic candidates")
    extract.add_argument("original", type=Path)
    extract.add_argument("english", type=Path)
    extract.add_argument("output_dir", type=Path)
    extract.add_argument("records_json", type=Path)

    verify = sub.add_parser("verify", help="verify fixed-layout patch invariants")
    verify.add_argument("english", type=Path)
    verify.add_argument("result", type=Path)
    verify.add_argument("report", type=Path)

    args = parser.parse_args(argv)
    if args.command == "extract":
        records = extract_graphic_candidates(args.original, args.english, args.output_dir)
        _write_json(args.records_json, {"schema": "srw-gc-add00-graphic-v1", "records": records})
        print(f"rendered {len(records)} changed BMP6 records")
        return 0
    report = verify_fixed_layout(args.english, args.result)
    _write_json(args.report, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
