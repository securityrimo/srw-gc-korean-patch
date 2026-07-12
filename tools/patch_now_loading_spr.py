#!/usr/bin/env python3
"""add00 블록 3508의 분할 로딩 이미지를 한국어로 재작성합니다."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


import add00_tools


BLOCK_INDEX = 3508
EXPECTED_BLOCK_SHA256 = "0577870542B2F71219DD0247ED27D42711279C76D52FADA9B59603E47F00240B"
EXPECTED_FONT_SHA256 = "E8CBC0B2AFCC14FB45DFB6086D5102C0B23A96E7B6E708F3122ACDE1B86C9082"
WINDOWS_DIR = os.environ.get("WINDIR")
DEFAULT_FONT = (
    Path(WINDOWS_DIR) / "Fonts" / "malgunbd.ttf"
    if WINDOWS_DIR
    else Path("malgunbd.ttf")
)
LOGICAL_SIZE = (312, 40)
TEXTURE_LAYOUT = {
    "nowload.biTEX00": (0, 0, 256, 32),
    "nowload.biTEX01": (256, 0, 32, 32),
    "nowload.biTEX02": (288, 0, 16, 32),
    "nowload.biTEX03": (304, 0, 8, 32),
    "nowload.biTEX04": (0, 32, 256, 8),
    "nowload.biTEX05": (256, 32, 32, 8),
    "nowload.biTEX06": (288, 32, 16, 8),
    "nowload.biTEX07": (304, 32, 8, 8),
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def parse_texture_chunks(block: bytes) -> dict[str, dict[str, int]]:
    if block[:4] != b"SPR\0":
        raise ValueError("block 3508 is not SPR")
    chunks: dict[str, dict[str, int]] = {}
    offset = 0x20
    while offset + 8 <= len(block):
        tag = block[offset : offset + 4]
        if tag == b"END_":
            break
        if tag not in (b"PAL_", b"TEX_", b"CEL_"):
            raise ValueError(f"unknown SPR chunk {tag!r} at 0x{offset:X}")
        size = struct.unpack_from("<I", block, offset + 4)[0]
        if size < 0x20 or offset + size > len(block):
            raise ValueError(f"invalid SPR chunk size at 0x{offset:X}")
        if tag == b"TEX_":
            name = block[offset + 0x10 : offset + 0x30].split(b"\0", 1)[0].decode("ascii")
            width, height, format_code, palette_count = struct.unpack_from("<4H", block, offset + 0x20)
            payload_size = width * height // 2
            if format_code != 4 or palette_count != 1 or size != 0x80 + payload_size:
                raise ValueError(f"unexpected C4 texture metadata for {name}")
            chunks[name] = {
                "chunk_offset": offset,
                "width": width,
                "height": height,
                "payload_offset": offset + 0x80,
                "payload_size": payload_size,
            }
        offset += size
    if set(chunks) != set(TEXTURE_LAYOUT):
        raise ValueError(f"unexpected texture set: {sorted(chunks)}")
    return chunks


def encode_linear_indices(image: Image.Image) -> bytes:
    if image.mode != "L" or image.width % 2:
        raise ValueError("index image must be even-width L mode")
    pixels = image.load()
    output = bytearray()
    for y in range(image.height):
        for x in range(0, image.width, 2):
            high, low = pixels[x, y], pixels[x + 1, y]
            if high > 15 or low > 15:
                raise ValueError("C4 palette index out of range")
            output.append((high << 4) | low)
    return bytes(output)


def render_indices(text: str, font_path: Path, font_size: int) -> tuple[Image.Image, dict[str, object]]:
    # Index 1 is the original opaque-black background; 2..15 are the
    # original cyan-to-white antialias ramp. Index 0 stays unused.
    mask = Image.new("L", LOGICAL_SIZE, 0)
    draw = ImageDraw.Draw(mask)
    font = ImageFont.truetype(str(font_path), font_size)
    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=0)
    width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
    old_right = 308
    old_bottom = 37
    x = old_right - width - bbox[0]
    y = 4 + (33 - height) // 2 - bbox[1]
    if x < 0 or y + bbox[1] < 0 or y + bbox[3] > LOGICAL_SIZE[1]:
        raise ValueError(f"text does not fit at font size {font_size}: bbox={bbox}, origin={(x, y)}")
    draw.text((x, y), text, font=font, fill=255)

    # Pillow's metric box can contain one or two empty edge columns. Align
    # the actual antialiased ink to the original image's visible right and
    # bottom edges so the separate SPR tiles join without a visual gap.
    initial_ink_bbox = mask.getbbox()
    if initial_ink_bbox is None:
        raise ValueError("rendered text is blank")
    dx = old_right - initial_ink_bbox[2]
    dy = old_bottom - initial_ink_bbox[3]
    if dx or dy:
        shifted = Image.new("L", LOGICAL_SIZE, 0)
        shifted.paste(mask, (dx, dy))
        mask = shifted
        mask_pixels = mask.load()
        x += dx
        y += dy

    indices = Image.new("L", LOGICAL_SIZE, 1)
    mask_pixels = mask.load()
    index_pixels = indices.load()
    for py in range(LOGICAL_SIZE[1]):
        for px in range(LOGICAL_SIZE[0]):
            alpha = mask_pixels[px, py]
            if alpha:
                index_pixels[px, py] = 2 + (alpha * 13 + 127) // 255
    ink_bbox = mask.getbbox()
    return indices, {
        "text": text,
        "font_size": font_size,
        "textbbox": list(bbox),
        "origin": [x, y],
        "ink_bbox": list(ink_bbox) if ink_bbox else None,
        "right_alignment": old_right,
        "bottom_alignment": old_bottom,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--font", type=Path, default=DEFAULT_FONT)
    parser.add_argument("--text", default="불러오는 중...")
    parser.add_argument("--font-size", type=int, default=33)
    parser.add_argument("--logical-payload", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    paths = [args.source, args.output]
    paths.extend(path for path in (args.logical_payload, args.report) if path is not None)
    resolved = [path.resolve() for path in paths]
    if len(set(resolved)) != len(resolved):
        raise ValueError("입력·출력·페이로드·보고서 경로는 서로 달라야 합니다")
    for path in paths[1:]:
        if path.exists():
            raise FileExistsError(path)
    font_data = args.font.read_bytes()
    if sha256(font_data) != EXPECTED_FONT_SHA256:
        raise ValueError(f"font SHA-256 drift: {sha256(font_data)}")

    container = add00_tools.parse_container(args.source)
    original_block = container.blocks[BLOCK_INDEX]
    if sha256(original_block) != EXPECTED_BLOCK_SHA256:
        raise ValueError(f"unexpected block 3508 SHA-256: {sha256(original_block)}")
    chunks = parse_texture_chunks(original_block)
    indices, render_info = render_indices(args.text, args.font, args.font_size)
    logical_payload = encode_linear_indices(indices)

    patched_block = bytearray(original_block)
    texture_report = []
    for name, (x, y, width, height) in TEXTURE_LAYOUT.items():
        chunk = chunks[name]
        if (chunk["width"], chunk["height"]) != (width, height):
            raise ValueError(f"unexpected dimensions for {name}")
        part = indices.crop((x, y, x + width, y + height))
        payload = encode_linear_indices(part)
        start = chunk["payload_offset"]
        end = start + chunk["payload_size"]
        if len(payload) != chunk["payload_size"]:
            raise ValueError(f"payload length mismatch for {name}")
        patched_block[start:end] = payload
        texture_report.append({
            "name": name,
            "dimensions": [width, height],
            "payload_extent": [start, end],
            "before_sha256": sha256(original_block[start:end]),
            "after_sha256": sha256(payload),
        })

    rebuilt = bytearray(container.source)
    block_start = container.offsets[BLOCK_INDEX]
    block_end = block_start + len(original_block)
    rebuilt[block_start:block_end] = patched_block
    verified = add00_tools.parse_container(bytes(rebuilt))
    changed_blocks = [
        index for index, (before, after) in enumerate(zip(container.blocks, verified.blocks))
        if before != after
    ]
    if changed_blocks != [BLOCK_INDEX] or container.offsets != verified.offsets:
        raise RuntimeError(f"container guard failed: changed={changed_blocks}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(rebuilt)
    if args.logical_payload:
        args.logical_payload.parent.mkdir(parents=True, exist_ok=True)
        args.logical_payload.write_bytes(logical_payload)
    report = {
        "status": "pass",
        "source": str(args.source.resolve()),
        "output": str(args.output.resolve()),
        "source_sha256": sha256(container.source),
        "output_sha256": sha256(rebuilt),
        "file_size": len(rebuilt),
        "outer_offsets_identical": container.offsets == verified.offsets,
        "changed_blocks": changed_blocks,
        "block_index": BLOCK_INDEX,
        "block_absolute_offset": block_start,
        "old_block_sha256": sha256(original_block),
        "new_block_sha256": sha256(bytes(patched_block)),
        "font": str(args.font.resolve()),
        "font_sha256": sha256(font_data),
        "logical_size": list(LOGICAL_SIZE),
        "logical_payload_sha256": sha256(logical_payload),
        "render": render_info,
        "textures": texture_report,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
