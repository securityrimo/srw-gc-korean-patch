#!/usr/bin/env python3
"""Build a Korean ``add00dat.bin`` on the untouched Japanese container.

Only explicitly Korean-rendered BMP6 groups are imported from the existing
fixed-layout Korean asset.  Every other block starts from retail Japanese.
The title uses the retail Japanese SPR/BMP9 topology; its indexed atlas and
tile maps are rebuilt locally for a Korean logo, so no English-patch title or
credit asset is present in the result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont


ALIGN = 0x20
BMP6 = b"BMP\x06"
BMP9 = b"BMP\x09"
BMP10 = b"BMP\x0A"
SCR = b"SCR\0"

UI_BITMAPS = (334, 355, 433, 438, 518, 923, 930, 947, 952, 959, 2714)
STORY_BITMAPS = tuple(range(2716, 2951, 3))
LOCATION_BITMAPS = tuple(range(3513, 3918, 2))
KOREAN_BITMAPS = UI_BITMAPS + STORY_BITMAPS + LOCATION_BITMAPS
TITLE_BITMAP = 3467
TITLE_PALETTE = 3468
TITLE_MAPS = tuple(range(3469, 3478))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def parse_container(path: Path) -> tuple[bytes, tuple[int, ...], tuple[bytes, ...]]:
    data = path.read_bytes()
    table_size = struct.unpack_from(">I", data, 0)[0]
    if not table_size or table_size % 4:
        raise ValueError(f"invalid offset table size: 0x{table_size:X}")
    count = table_size // 4
    offsets = tuple(struct.unpack_from(f">{count}I", data, 0))
    if offsets[0] != table_size or any(value % ALIGN for value in offsets):
        raise ValueError("container offsets are not 0x20-aligned")
    if list(offsets) != sorted(offsets) or len(set(offsets)) != len(offsets):
        raise ValueError("container offsets are not strictly increasing")
    blocks = tuple(
        data[start : offsets[index + 1] if index + 1 < count else len(data)]
        for index, start in enumerate(offsets)
    )
    return data, offsets, blocks


def rebuild_container(blocks: list[bytes]) -> bytes:
    table_size = len(blocks) * 4
    if table_size % ALIGN:
        raise ValueError("offset table is not aligned")
    offsets: list[int] = []
    cursor = table_size
    for index, block in enumerate(blocks):
        if len(block) % ALIGN:
            raise ValueError(f"block {index} length is not 0x20-aligned")
        offsets.append(cursor)
        cursor += len(block)
    return struct.pack(f">{len(offsets)}I", *offsets) + b"".join(blocks)


def bitmap_group_end(blocks: tuple[bytes, ...], start: int) -> int:
    """Return the next color-bitmap boundary after one BMP6 asset group."""

    if blocks[start][:4] != BMP6:
        raise ValueError(f"block {start} is not BMP6")
    cursor = start + 1
    while cursor < len(blocks):
        if blocks[cursor][:4] in (BMP6, BMP9):
            break
        cursor += 1
    return cursor


def rgba_palette(block: bytes) -> list[tuple[int, int, int, int]]:
    if block[:4] != BMP10 or len(block) < 32 + 1024:
        raise ValueError("title palette is not a 256-entry BMP10/RGBA table")
    if struct.unpack_from(">I", block, 4)[0] != 256:
        raise ValueError("unexpected title palette count")
    return [tuple(block[32 + index * 4 : 36 + index * 4]) for index in range(256)]


def _fit_text_mask(
    text: str,
    font_path: Path,
    font_size: int,
    target_size: tuple[int, int],
) -> Image.Image:
    scale = 4
    font = ImageFont.truetype(str(font_path), font_size * scale)
    probe = Image.new("L", (1, 1))
    box = ImageDraw.Draw(probe).textbbox((0, 0), text, font=font, stroke_width=0)
    width = box[2] - box[0]
    height = box[3] - box[1]
    mask = Image.new("L", (width + 16 * scale, height + 12 * scale))
    draw = ImageDraw.Draw(mask)
    draw.text((8 * scale - box[0], 5 * scale - box[1]), text, font=font, fill=255)
    target_width, target_height = target_size
    ratio = min(target_width / mask.width, target_height / mask.height)
    size = (max(1, round(mask.width * ratio)), max(1, round(mask.height * ratio)))
    return mask.resize(size, Image.Resampling.LANCZOS)


def _gradient(size: tuple[int, int]) -> Image.Image:
    width, height = size
    image = Image.new("RGBA", size)
    pixels = image.load()
    stops = (
        (0.00, (255, 255, 245, 255)),
        (0.23, (255, 239, 157, 255)),
        (0.58, (255, 178, 31, 255)),
        (1.00, (171, 49, 11, 255)),
    )
    for y in range(height):
        position = y / max(1, height - 1)
        for stop_index in range(len(stops) - 1):
            left, right = stops[stop_index], stops[stop_index + 1]
            if left[0] <= position <= right[0]:
                amount = (position - left[0]) / (right[0] - left[0])
                color = tuple(round(left[1][channel] * (1 - amount) + right[1][channel] * amount) for channel in range(4))
                break
        else:
            color = stops[-1][1]
        for x in range(width):
            pixels[x, y] = color
    return image


def _paste_logo_text(canvas: Image.Image, mask: Image.Image, position: tuple[int, int]) -> None:
    x, y = position
    # White outer keyline, black separator, and warm gold face reproduce the
    # retail logo's high-contrast hierarchy without borrowing English pixels.
    outer = mask.filter(ImageFilter.MaxFilter(11))
    inner = mask.filter(ImageFilter.MaxFilter(7))
    shadow = Image.new("L", canvas.size)
    shadow.paste(outer, (x + 4, y + 5))
    shadow_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    shadow_layer.paste((10, 8, 8, 210), (0, 0), shadow)
    canvas.alpha_composite(shadow_layer)

    outer_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    outer_layer.paste((245, 245, 236, 255), (x, y), outer)
    canvas.alpha_composite(outer_layer)

    black_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    black_layer.paste((26, 12, 8, 255), (x, y), inner)
    canvas.alpha_composite(black_layer)

    face = _gradient(mask.size)
    face.putalpha(mask)
    canvas.alpha_composite(face, (x, y))

    # A subtle white highlight at the upper edge survives palette quantizing.
    highlight = ImageChops.subtract(mask, ImageChops.offset(mask, 0, 2))
    highlight_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    highlight_layer.paste((255, 255, 255, 235), (x, y), highlight)
    canvas.alpha_composite(highlight_layer)


def make_korean_logo() -> Image.Image:
    canvas = Image.new("RGBA", (304, 120), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    # A compact lightning plate gives the logo the same visual mass as the
    # retail Japanese mark while keeping the Korean lettering unobstructed.
    plate = [(5, 34), (22, 17), (267, 17), (254, 29), (299, 29), (281, 48), (298, 51), (275, 65), (288, 69), (268, 83), (28, 83), (35, 72), (5, 72), (17, 59), (2, 55), (20, 44)]
    draw.polygon(plate, fill=(18, 12, 10, 235))
    draw.line(plate + [plate[0]], fill=(244, 244, 233, 255), width=2, joint="curve")

    korean_font = Path(r"C:\Windows\Fonts\malgunbd.ttf")
    latin_font = Path(r"C:\Windows\Fonts\ariblk.ttf")
    if not latin_font.exists():
        latin_font = korean_font
    main = _fit_text_mask("슈퍼로봇대전", korean_font, 86, (238, 70))
    gc = _fit_text_mask("GC", latin_font, 54, (55, 49))
    _paste_logo_text(canvas, main, (9, 20))
    _paste_logo_text(canvas, gc, (244, 55))
    draw.line((28, 94, 279, 94), fill=(248, 245, 231, 255), width=3)
    draw.line((36, 99, 266, 99), fill=(230, 129, 18, 255), width=3)
    return canvas


def quantize_to_palette(image: Image.Image, palette: list[tuple[int, int, int, int]]) -> Image.Image:
    source = image.convert("RGBA")
    output = Image.new("P", source.size)
    flat_palette: list[int] = []
    for red, green, blue, _alpha in palette:
        flat_palette.extend((red, green, blue))
    output.putpalette(flat_palette)
    destination = output.load()
    opaque = [(index, color) for index, color in enumerate(palette) if color[3] >= 128 and index != 0]
    cache: dict[tuple[int, int, int], int] = {}
    for y in range(source.height):
        for x in range(source.width):
            red, green, blue, alpha = source.getpixel((x, y))
            if alpha < 80:
                destination[x, y] = 0
                continue
            key = (red, green, blue)
            if key not in cache:
                cache[key] = min(
                    opaque,
                    key=lambda item: (red - item[1][0]) ** 2 + (green - item[1][1]) ** 2 + (blue - item[1][2]) ** 2,
                )[0]
            destination[x, y] = cache[key]
    output.info["transparency"] = 0
    return output


def encode_title_atlas(template: bytes, indexed_logo: Image.Image) -> bytes:
    if template[:4] != BMP9:
        raise ValueError("title atlas is not BMP9")
    colors, width, height = struct.unpack_from(">III", template, 4)
    if (colors, width, height) != (256, 512, 120):
        raise ValueError(f"unexpected retail title atlas shape: {(colors, width, height)}")
    if indexed_logo.size != (304, 120):
        raise ValueError("Korean logo must be 304x120")
    atlas = Image.new("P", (width, height), 0)
    atlas.putpalette(indexed_logo.getpalette())
    # Column zero remains a guaranteed transparent tile.  The 38-column
    # Korean canvas occupies tile columns 1..38.
    atlas.paste(indexed_logo, (8, 0))
    pixels = atlas.load()
    payload = bytearray()
    for tile_y in range(0, height, 8):
        for tile_x in range(0, width, 8):
            for y in range(8):
                for x in range(8):
                    payload.append(pixels[tile_x + x, tile_y + y])
    result = template[:32] + bytes(payload)
    if len(result) != len(template):
        raise ValueError("title atlas length changed")
    return result


def rewrite_scr(block: bytes, entries: list[int]) -> bytes:
    if block[:4] != SCR:
        raise ValueError("title map is not SCR")
    width, height = struct.unpack_from(">II", block, 4)
    count = (len(block) - 32) // 2
    if len(entries) != width * height or len(entries) > count:
        raise ValueError("SCR replacement entry count mismatch")
    output = bytearray(block)
    output[32:] = b"\0" * (len(block) - 32)
    struct.pack_into(f">{len(entries)}H", output, 32, *entries)
    return bytes(output)


def render_scr(atlas: Image.Image, block: bytes) -> Image.Image:
    width, height = struct.unpack_from(">II", block, 4)
    values = struct.unpack_from(f">{(len(block) - 32) // 2}H", block, 32)[: width * height]
    result = Image.new("RGBA", (width * 8, height * 8), (0, 0, 0, 0))
    tiles_x = atlas.width // 8
    for position, value in enumerate(values):
        tile_index = value & 0x03FF
        source_x = (tile_index % tiles_x) * 8
        source_y = (tile_index // tiles_x) * 8
        tile = atlas.crop((source_x, source_y, source_x + 8, source_y + 8))
        if value & 0x0400:
            tile = tile.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        if value & 0x0800:
            tile = tile.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        result.alpha_composite(tile, ((position % width) * 8, (position // width) * 8))
    return result


def decode_title_atlas(block: bytes, palette: list[tuple[int, int, int, int]]) -> Image.Image:
    _, width, height = struct.unpack_from(">III", block, 4)
    payload = block[32 : 32 + width * height]
    result = Image.new("RGBA", (width, height))
    pixels = result.load()
    cursor = 0
    for tile_y in range(0, height, 8):
        for tile_x in range(0, width, 8):
            for y in range(8):
                for x in range(8):
                    pixels[tile_x + x, tile_y + y] = palette[payload[cursor]]
                    cursor += 1
    return result


def build(args: argparse.Namespace) -> dict[str, object]:
    japanese_data, japanese_offsets, japanese = parse_container(args.japanese)
    english_data, _english_offsets, english = parse_container(args.english)
    korean_data, _korean_offsets, korean = parse_container(args.korean)
    if not (len(japanese) == len(english) == len(korean) == 3920):
        raise ValueError("unexpected add00 block count")

    blocks = list(japanese)
    imported: set[int] = set()
    group_records: list[dict[str, object]] = []
    for bitmap in KOREAN_BITMAPS:
        end = bitmap_group_end(japanese, bitmap)
        group = list(range(bitmap, end))
        for index in group:
            blocks[index] = korean[index]
            imported.add(index)
        group_records.append({"bitmap": bitmap, "start": bitmap, "end_exclusive": end, "blocks": group})

    palette = rgba_palette(japanese[TITLE_PALETTE])
    logo = make_korean_logo()
    indexed = quantize_to_palette(logo, palette)
    blocks[TITLE_BITMAP] = encode_title_atlas(japanese[TITLE_BITMAP], indexed)

    # The seven per-glyph maps drive the retail fly-in animation.  They are
    # blanked rather than showing Japanese or English letters.  The final logo
    # map selects the Korean canvas.  The retail white-flash map is also
    # blanked to avoid covering the new colored logo.
    for index in TITLE_MAPS[:7]:
        width, height = struct.unpack_from(">II", japanese[index], 4)
        blocks[index] = rewrite_scr(japanese[index], [0] * (width * height))
    final_map = TITLE_MAPS[7]
    width, height = struct.unpack_from(">II", japanese[final_map], 4)
    if (width, height) != (38, 15):
        raise ValueError("unexpected retail final-logo map size")
    entries = [row * 64 + column + 1 for row in range(height) for column in range(width)]
    blocks[final_map] = rewrite_scr(japanese[final_map], entries)
    flash_map = TITLE_MAPS[8]
    width, height = struct.unpack_from(">II", japanese[flash_map], 4)
    blocks[flash_map] = rewrite_scr(japanese[flash_map], [0] * (width * height))

    output = rebuild_container(blocks)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(output)

    # Save deterministic visual QA artifacts alongside the binary.
    args.preview_dir.mkdir(parents=True, exist_ok=True)
    logo.save(args.preview_dir / "korean_title_logo_source.png")
    indexed.convert("RGBA").save(args.preview_dir / "korean_title_logo_palette_preview.png")
    atlas = decode_title_atlas(blocks[TITLE_BITMAP], palette)
    render_scr(atlas, blocks[final_map]).save(args.preview_dir / "korean_title_logo_final_map.png")

    parsed_data, parsed_offsets, parsed_blocks = parse_container(args.output)
    if parsed_data != output or tuple(blocks) != parsed_blocks:
        raise ValueError("rebuilt container failed round-trip verification")
    if any(value % ALIGN for value in parsed_offsets):
        raise ValueError("rebuilt offsets are not aligned")

    changed_from_japanese = [index for index, (left, right) in enumerate(zip(japanese, parsed_blocks)) if left != right]
    changed_from_english = [index for index, (left, right) in enumerate(zip(english, parsed_blocks)) if left != right]
    exact_english_visual = [
        index
        for index, (jp, en, out) in enumerate(zip(japanese, english, parsed_blocks))
        if jp != en and out == en and out[:4] in (BMP6, BMP9, b"SPR\0")
    ]
    title_exact_english = [index for index in range(3466, 3478) if parsed_blocks[index] == english[index] and japanese[index] != english[index]]
    outside_allowed = [
        index
        for index in changed_from_japanese
        if index not in imported and index not in {TITLE_BITMAP, *TITLE_MAPS}
    ]
    if exact_english_visual or title_exact_english or outside_allowed:
        raise ValueError(
            f"English visual residue or out-of-scope block change: visual={exact_english_visual} "
            f"title={title_exact_english} outside={outside_allowed}"
        )

    report = {
        "schema": "srw-gc-add00-japanese-base-korean-v1",
        "inputs": {
            "japanese": str(args.japanese.resolve()),
            "japanese_size": len(japanese_data),
            "japanese_sha256": sha256(japanese_data),
            "english_reference": str(args.english.resolve()),
            "english_size": len(english_data),
            "english_sha256": sha256(english_data),
            "korean_graphic_source": str(args.korean.resolve()),
            "korean_graphic_source_size": len(korean_data),
            "korean_graphic_source_sha256": sha256(korean_data),
        },
        "output": {
            "path": str(args.output.resolve()),
            "size": len(output),
            "sha256": sha256(output),
            "block_count": len(parsed_blocks),
            "offset_alignment": ALIGN,
            "round_trip_verified": True,
        },
        "korean_imports": {
            "bitmap_count": len(KOREAN_BITMAPS),
            "ui_bitmap_count": len(UI_BITMAPS),
            "story_bitmap_count": len(STORY_BITMAPS),
            "location_bitmap_count": len(LOCATION_BITMAPS),
            "bitmap_blocks": list(KOREAN_BITMAPS),
            "full_group_block_count": len(imported),
            "full_group_blocks": sorted(imported),
            "groups": group_records,
        },
        "title": {
            "topology": "retail_japanese",
            "retail_spr_block_preserved": parsed_blocks[3466] == japanese[3466],
            "generated_korean_bmp9_block": TITLE_BITMAP,
            "retail_palette_block_preserved": parsed_blocks[TITLE_PALETTE] == japanese[TITLE_PALETTE],
            "patched_scr_blocks": list(TITLE_MAPS),
            "english_title_blocks_exact": title_exact_english,
            "preview": str((args.preview_dir / "korean_title_logo_final_map.png").resolve()),
        },
        "audit": {
            "changed_from_japanese_count": len(changed_from_japanese),
            "changed_from_japanese_blocks": changed_from_japanese,
            "different_from_english_count": len(changed_from_english),
            "english_only_visual_blocks_exact": exact_english_visual,
            "outside_allowed_changes": outside_allowed,
            "status": "pass",
        },
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    workspace = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--japanese", type=Path, default=workspace / "work/srw_gc_iso_extract/original/add00dat.bin")
    parser.add_argument("--english", type=Path, default=workspace / "work/srw_gc_iso_extract/english/add00dat.bin")
    parser.add_argument(
        "--korean",
        type=Path,
        default=workspace / "work/srw_gc_complete_patch/add00dat.complete_ko.ui.fixed_layout.bin",
    )
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "add00dat.japanese_base.korean.bin")
    parser.add_argument("--report", type=Path, default=Path(__file__).resolve().parent / "build_report.json")
    parser.add_argument("--preview-dir", type=Path, default=Path(__file__).resolve().parent / "preview")
    args = parser.parse_args()
    report = build(args)
    print(json.dumps(report["output"], ensure_ascii=False, indent=2))
    print(json.dumps(report["title"], ensure_ascii=False, indent=2))
    print(json.dumps(report["audit"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
