#!/usr/bin/env python3
"""Render every SCR view belonging to an add00 BMP6 atlas.

This is an analysis helper.  It never modifies the source container.  Large
UI atlas 518 uses a 14-bit direct 8x8 tile index plus H/V flags in bits 14/15;
the smaller add00 atlases use the conventional low-10-bit index and flags.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps


TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS_DIR))
import add00_tools  # noqa: E402


def render_direct(atlas: Image.Image, scr: bytes) -> tuple[Image.Image, dict[str, object]]:
    if scr[:4] != b"SCR\0" or len(scr) < 32 or (len(scr) - 32) % 2:
        raise ValueError("invalid SCR block")
    width, height = struct.unpack_from(">II", scr, 4)
    stored_count = (len(scr) - 32) // 2
    values = struct.unpack_from(f">{stored_count}H", scr, 32)[: width * height]
    if len(values) < width * height:
        raise ValueError("SCR block is shorter than its visible canvas")
    tile_indices = [value & 0x3FFF for value in values]
    tiles_x = atlas.width // 8
    tile_count = tiles_x * (atlas.height // 8)
    output = Image.new("L", (width * 8, height * 8), 0)
    invalid: list[int] = []
    horizontal_flips = 0
    vertical_flips = 0
    for position, value in enumerate(values):
        tile_index = tile_indices[position]
        if tile_index >= tile_count:
            invalid.append(tile_index)
            continue
        source_x = (tile_index % tiles_x) * 8
        source_y = (tile_index // tiles_x) * 8
        tile = atlas.crop((source_x, source_y, source_x + 8, source_y + 8))
        if value & 0x4000:
            tile = tile.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            horizontal_flips += 1
        if value & 0x8000:
            tile = tile.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
            vertical_flips += 1
        output.paste(tile, ((position % width) * 8, (position // width) * 8))
    return output, {
        "mode": "direct_14bit_tile_index_hv_flags",
        "declared_tiles": [width, height],
        "stored_entry_count": stored_count,
        "visible_entry_count": width * height,
        "minimum_index": min(tile_indices) if tile_indices else None,
        "maximum_index": max(tile_indices) if tile_indices else None,
        "invalid_indices": sorted(set(invalid)),
        "horizontal_flip_count": horizontal_flips,
        "vertical_flip_count": vertical_flips,
    }


def render_gx10(atlas: Image.Image, scr: bytes) -> tuple[Image.Image, dict[str, object]]:
    if scr[:4] != b"SCR\0" or len(scr) < 32 or (len(scr) - 32) % 2:
        raise ValueError("invalid SCR block")
    width, height = struct.unpack_from(">II", scr, 4)
    stored_count = (len(scr) - 32) // 2
    values = struct.unpack_from(f">{stored_count}H", scr, 32)[: width * height]
    if len(values) < width * height:
        raise ValueError("SCR block is shorter than its visible canvas")
    tiles_x = atlas.width // 8
    tile_count = tiles_x * (atlas.height // 8)
    output = Image.new("L", (width * 8, height * 8), 0)
    invalid: list[int] = []
    for position, value in enumerate(values):
        tile_index = value & 0x03FF
        if tile_index >= tile_count:
            invalid.append(value)
            continue
        source_x = (tile_index % tiles_x) * 8
        source_y = (tile_index // tiles_x) * 8
        tile = atlas.crop((source_x, source_y, source_x + 8, source_y + 8))
        if value & 0x0400:
            tile = tile.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        if value & 0x0800:
            tile = tile.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        output.paste(tile, ((position % width) * 8, (position // width) * 8))
    return output, {
        "mode": "gx_10bit_tile_index_hv_flags",
        "declared_tiles": [width, height],
        "stored_entry_count": stored_count,
        "visible_entry_count": width * height,
        "minimum_raw_value": min(values) if values else None,
        "maximum_raw_value": max(values) if values else None,
        "invalid_raw_values": sorted(set(invalid)),
    }


def contact_sheets(records: list[dict[str, object]], output: Path, columns: int = 2) -> list[str]:
    paths: list[str] = []
    rows_per_page = 50
    cell_width, cell_height = 800, 64
    font = ImageFont.load_default()
    per_page = columns * rows_per_page
    for page_number, start in enumerate(range(0, len(records), per_page), start=1):
        page_records = records[start : start + per_page]
        rows = (len(page_records) + columns - 1) // columns
        page = Image.new("L", (columns * cell_width, rows * cell_height), 0)
        draw = ImageDraw.Draw(page)
        for position, record in enumerate(page_records):
            x = (position % columns) * cell_width
            y = (position // columns) * cell_height
            draw.text((x + 4, y + 3), f"SCR {record['block_index']}", font=font, fill=255)
            preview = Image.open(record["preview"]).convert("L")
            preview = preview.resize(
                (preview.width * 2, preview.height * 2), Image.Resampling.NEAREST
            )
            page.paste(preview, (x + 62, y + 8))
        path = output / f"contact_sheet_{page_number:02d}.png"
        page.save(path)
        paths.append(str(path.resolve()))
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--bitmap", type=int, default=518)
    parser.add_argument("--mode", choices=("auto", "direct", "gx10"), default="auto")
    args = parser.parse_args()

    container = add00_tools.parse_container(args.source)
    atlas_block = container.blocks[args.bitmap]
    atlas = add00_tools.decode_i4(atlas_block)
    mode = args.mode
    if mode == "auto":
        mode = "direct" if (atlas.width // 8) * (atlas.height // 8) > 1024 else "gx10"

    args.output.mkdir(parents=True, exist_ok=True)
    preview_dir = args.output / "scr"
    preview_dir.mkdir(exist_ok=True)
    ocr_dir = args.output / "ocr"
    ocr_dir.mkdir(exist_ok=True)
    records: list[dict[str, object]] = []
    block_index = args.bitmap + 1
    while block_index < len(container.blocks):
        block = container.blocks[block_index]
        if block[:4] in (b"BMP\x06", b"BMP\x09"):
            break
        if block[:4] == b"SCR\0":
            image, details = (
                render_direct(atlas, block) if mode == "direct" else render_gx10(atlas, block)
            )
            preview = preview_dir / f"scr_{block_index:04d}.png"
            image.save(preview)
            scale = max(2, min(8, 1800 // max(image.size)))
            border = max(8, scale * 4)
            ocr_image = ImageOps.expand(
                ImageOps.invert(image).resize(
                    (image.width * scale, image.height * scale),
                    Image.Resampling.NEAREST,
                ),
                border=border,
                fill=255,
            )
            ocr_input = ocr_dir / f"scr_{block_index:04d}_ocr.png"
            ocr_image.save(ocr_input)
            records.append(
                {
                    "id": f"add00:scr:{block_index:04d}",
                    "block_index": block_index,
                    "preview": str(preview.resolve()),
                    "ocr_input": str(ocr_input.resolve()),
                    "ocr_scale": scale,
                    "ocr_border": border,
                    "pixel_dimensions": list(image.size),
                    **details,
                }
            )
        block_index += 1

    report = {
        "schema": "srw-gc-add00-scr-atlas-analysis-v1",
        "source": str(args.source.resolve()),
        "source_sha256": hashlib.sha256(container.source).hexdigest().upper(),
        "bitmap_block": args.bitmap,
        "bitmap_dimensions": list(atlas.size),
        "bitmap_tile_capacity": (atlas.width // 8) * (atlas.height // 8),
        "group_end_exclusive": block_index,
        "scr_count": len(records),
        "mode": mode,
        "records": records,
    }
    report["contact_sheets"] = contact_sheets(records, args.output)
    (args.output / "manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({key: report[key] for key in (
        "source", "bitmap_block", "bitmap_dimensions", "bitmap_tile_capacity",
        "group_end_exclusive", "scr_count", "mode", "contact_sheets",
    )}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
