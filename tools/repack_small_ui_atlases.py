#!/usr/bin/env python3
"""Rebuild the small add00 UI atlases without shared-tile corruption.

The English patch stores many independent UI labels in compact GX I4 atlases.
Editing atlas pixels in place is unsafe because different SCR maps share the
same 8x8 tiles.  This builder renders every SCR independently, replaces the
requested canvases with Korean, deduplicates tiles (including H/V mirrored
forms), and rewrites every SCR with the conventional 10-bit tile index plus
bits 10/11 for horizontal/vertical flips.

The input container is never modified in place.  Its size, pointer table,
block offsets, BMP headers, palette blocks, and all blocks outside the chosen
atlas groups are guarded byte-for-byte.

This builder produces a visual-reference intermediate.  Because it renumbers
existing atlas tiles, its output must be passed through
``repack_preserve_indices.py`` before it can be used by the game.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import sys
from collections import defaultdict
from pathlib import Path
from typing import Iterable

import PIL
from PIL import Image, ImageChops, ImageDraw, ImageFont, features


TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS_DIR))
import add00_tools  # noqa: E402


WINDOWS_DIR = os.environ.get("WINDIR")
DEFAULT_FONT = (
    Path(WINDOWS_DIR) / "Fonts" / "malgunbd.ttf"
    if WINDOWS_DIR
    else Path("malgunbd.ttf")
)
FONT = DEFAULT_FONT
FONT_SHA256 = "E8CBC0B2AFCC14FB45DFB6086D5102C0B23A96E7B6E708F3122ACDE1B86C9082"
JAPANESE_RE = re.compile(r"[\u3041-\u3096\u30a1-\u30fa\u3400-\u9fff]")
ASCII_WORD_RE = re.compile(r"[A-Za-z]+")
SUPPORTED_BITMAPS = (334, 355, 433, 438, 923, 930, 947, 952, 959, 2714, 3489)
FLIP_FLAGS = (0x0000, 0x0400, 0x0800, 0x0C00)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def quantize_i4(image: Image.Image) -> Image.Image:
    """Use the exact rounding performed by :func:`add00_tools.encode_i4`."""

    output = Image.new("L", image.size)
    output.putdata(
        [max(0, min(15, (value + 8) // 17)) * 17 for value in image.getdata()]
    )
    return output


def render_gx10(atlas: Image.Image, scr: bytes) -> Image.Image:
    """Render a small SCR map (10-bit tile index, H/V in bits 10/11)."""

    if scr[:4] != b"SCR\0" or len(scr) < 32 or (len(scr) - 32) % 2:
        raise ValueError("invalid SCR block")
    width, height = struct.unpack_from(">II", scr, 4)
    values = struct.unpack_from(f">{(len(scr) - 32) // 2}H", scr, 32)
    if len(values) < width * height:
        raise ValueError("SCR block is shorter than its visible canvas")
    return render_entries(atlas, width, height, values[: width * height])


def render_entries(
    atlas: Image.Image, width: int, height: int, values: Iterable[int]
) -> Image.Image:
    output = Image.new("L", (width * 8, height * 8), 0)
    tiles_x = atlas.width // 8
    tile_count = tiles_x * (atlas.height // 8)
    for position, value in enumerate(values):
        tile_index = value & 0x03FF
        if tile_index >= tile_count:
            raise ValueError(f"SCR tile index {tile_index} exceeds capacity {tile_count}")
        x = (tile_index % tiles_x) * 8
        y = (tile_index // tiles_x) * 8
        tile = atlas.crop((x, y, x + 8, y + 8))
        if value & 0x0400:
            tile = tile.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        if value & 0x0800:
            tile = tile.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        output.paste(tile, ((position % width) * 8, (position // width) * 8))
    return output


def flip_tile(pattern: bytes, flags: int) -> bytes:
    tile = Image.frombytes("L", (8, 8), pattern)
    if flags & 0x0400:
        tile = tile.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if flags & 0x0800:
        tile = tile.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    return tile.tobytes()


def image_tiles(image: Image.Image) -> Iterable[bytes]:
    if image.width % 8 or image.height % 8:
        raise ValueError(f"SCR canvas is not 8x8 aligned: {image.size}")
    for y in range(0, image.height, 8):
        for x in range(0, image.width, 8):
            yield image.crop((x, y, x + 8, y + 8)).tobytes()


def pack_images(
    atlas_size: tuple[int, int], images: dict[int, Image.Image]
) -> tuple[Image.Image, dict[int, list[int]], int]:
    """Pack images with flip-aware canonical tile deduplication."""

    tiles_x = atlas_size[0] // 8
    capacity = tiles_x * (atlas_size[1] // 8)
    if capacity > 1024:
        raise ValueError(f"small-atlas capacity exceeds 10-bit index space: {capacity}")

    blank = bytes(64)
    patterns = [blank]
    canonical_to_index = {blank: 0}
    entries_by_scr: dict[int, list[int]] = {}
    for block_index in sorted(images):
        entries: list[int] = []
        for pattern in image_tiles(images[block_index]):
            variants = [flip_tile(pattern, flags) for flags in FLIP_FLAGS]
            canonical = min(variants)
            tile_index = canonical_to_index.get(canonical)
            if tile_index is None:
                if len(patterns) >= capacity:
                    raise ValueError(
                        f"atlas {atlas_size} exhausted at SCR {block_index}: "
                        f"more than {capacity} flip-deduplicated tiles"
                    )
                tile_index = len(patterns)
                canonical_to_index[canonical] = tile_index
                patterns.append(canonical)
            chosen_flags = next(
                flags for flags in FLIP_FLAGS if flip_tile(canonical, flags) == pattern
            )
            entries.append(tile_index | chosen_flags)
        entries_by_scr[block_index] = entries

    atlas = Image.new("L", atlas_size, 0)
    for tile_index, pattern in enumerate(patterns):
        atlas.paste(
            Image.frombytes("L", (8, 8), pattern),
            ((tile_index % tiles_x) * 8, (tile_index // tiles_x) * 8),
        )
    return atlas, entries_by_scr, len(patterns)


def clean_background(bitmap: int, source: Image.Image) -> Image.Image:
    """Reconstruct non-text art for the few labels that are not black canvases."""

    image = source.copy().convert("L")
    draw = ImageDraw.Draw(image)
    if bitmap == 334:
        # Square command key: retain its outer bevel and restore the flat core.
        draw.rectangle((2, 2, 29, 29), fill=51)
    elif bitmap == 355:
        # Square status key.  The public English atlas has a flat level-4 core.
        draw.rectangle((2, 2, 29, 29), fill=68)
    elif bitmap == 433:
        # White capture-status pill; rows 6..27 are a uniform I4 level 14.
        draw.rectangle((7, 6, 144, 27), fill=238)
    elif bitmap == 952:
        # The long selector is a vertical gradient.  Its clean left section is
        # repeated horizontally, so use x=20 as the row's unoccluded sample.
        for y in range(image.height):
            value = image.getpixel((20, y))
            draw.line((30, y, 119, y), fill=value)
    else:
        image = Image.new("L", source.size, 0)
    return image


def normalize_display_text(block_index: int, text: str) -> str:
    if 363 <= block_index <= 369 and text.startswith("격추 "):
        return text.replace(" ", "\n", 1)
    return text


def draw_fitted_text(
    background: Image.Image,
    text: str,
    *,
    align: str,
    region: tuple[int, int, int, int] | None = None,
    fill: int = 255,
    shadow: int | None = 64,
    maximum_font_size: int | None = None,
) -> tuple[Image.Image, int, tuple[int, int, int, int]]:
    """Fit Malgun Gothic Bold into a canvas or an explicit subregion."""

    output = background.copy().convert("L")
    draw = ImageDraw.Draw(output)
    if region is None:
        region = (0, 0, output.width, output.height)
    left, top, right, bottom = region
    width, height = right - left, bottom - top
    spacing = -2 if "\n" in text else 0
    limit = maximum_font_size or min(32, height + 4)
    chosen = None
    for font_size in range(limit, 5, -1):
        font = ImageFont.truetype(str(FONT), font_size)
        box = draw.multiline_textbbox(
            (0, 0), text, font=font, spacing=spacing, align="center"
        )
        text_width = box[2] - box[0]
        text_height = box[3] - box[1]
        if text_width <= width - 4 and text_height <= height - 2:
            chosen = font, font_size, box, text_width, text_height
            break
    if chosen is None:
        raise ValueError(f"cannot fit {text!r} into region {region} of {output.size}")
    font, font_size, box, text_width, text_height = chosen
    if align == "left":
        x = left + 2 - box[0]
    elif align == "right":
        x = right - text_width - 2 - box[0]
    elif align == "center":
        x = left + (width - text_width) // 2 - box[0]
    else:
        raise ValueError(f"unsupported text alignment: {align}")
    y = top + (height - text_height) // 2 - box[1]
    if shadow is not None:
        draw.multiline_text(
            (x + 1, y + 1), text, font=font, spacing=spacing,
            align="center", fill=shadow,
        )
    draw.multiline_text(
        (x, y), text, font=font, spacing=spacing, align="center", fill=fill
    )
    return output, font_size, region


def render_korean_label(
    bitmap: int, block_index: int, english_view: Image.Image, korean: str
) -> tuple[Image.Image, dict[str, object]]:
    display = normalize_display_text(block_index, korean)
    if bitmap == 930:
        # SCR 933 is a complete 640x480 frame.  Only the title in the black
        # header panel is replaced; all chrome and panel art stays untouched.
        background = english_view.copy().convert("L")
        ImageDraw.Draw(background).rectangle((36, 61, 303, 95), fill=0)
        result, font_size, region = draw_fitted_text(
            background,
            display,
            align="left",
            region=(40, 62, 300, 95),
            fill=255,
            shadow=64,
            maximum_font_size=28,
        )
        mode = "screen_header_overlay"
    else:
        background = clean_background(bitmap, english_view)
        if bitmap == 334:
            region, align, fill, shadow = (2, 2, 30, 30), "center", 255, 0
            mode = "square_command_key"
        elif bitmap == 355:
            region, align, fill, shadow = (2, 2, 30, 30), "center", 255, 0
            mode = "square_status_key"
        elif bitmap == 433:
            region, align, fill, shadow = (8, 6, 144, 28), "center", 0, None
            mode = "capture_status_pill"
        elif bitmap == 947:
            region, align, fill, shadow = None, "left", 255, 64
            mode = "black_canvas_left"
        elif bitmap == 952:
            region, align, fill, shadow = (30, 2, 120, 30), "center", 255, 0
            mode = "gradient_selector"
        else:
            region, align, fill, shadow = None, "center", 255, 64
            mode = "black_canvas_center"
        result, font_size, region = draw_fitted_text(
            background,
            display,
            align=align,
            region=region,
            fill=fill,
            shadow=shadow,
            # The 128x32 source atlas only has 64 tiles for all three pills.
            # A common 16 px face is the largest clean Korean rendering that
            # remains within that original capacity after flip deduplication.
            maximum_font_size=(
                16 if bitmap == 433 else
                24 if bitmap == 952 else
                None
            ),
        )
    result = quantize_i4(result)
    return result, {
        "block_index": block_index,
        "korean": korean,
        "rendered_text": display,
        "render_mode": mode,
        "font_size": font_size,
        "dimensions": list(result.size),
        "region": list(region),
        "ascii_words": ASCII_WORD_RE.findall(korean),
    }


def group_scr_blocks(container: add00_tools.Add00Container, bitmap: int) -> list[int]:
    result: list[int] = []
    index = bitmap + 1
    while index < len(container.blocks):
        signature = container.blocks[index][:4]
        if signature in (b"BMP\x06", b"BMP\x09"):
            break
        if signature == b"SCR\0":
            result.append(index)
        index += 1
    return result


def build_contact_sheet(
    bitmap: int,
    scr_blocks: list[int],
    japanese_images: dict[int, Image.Image],
    english_images: dict[int, Image.Image],
    korean_images: dict[int, Image.Image],
    output: Path,
) -> str:
    cell_width = 420
    row_height = 76
    header_height = 24
    page = Image.new(
        "L", (40 + cell_width * 3, header_height + row_height * len(scr_blocks)), 0
    )
    draw = ImageDraw.Draw(page)
    for column, label in enumerate(("JAPANESE", "ENGLISH", "KOREAN")):
        draw.text((42 + column * cell_width, 4), label, fill=255)
    for row, block_index in enumerate(scr_blocks):
        y = header_height + row * row_height
        draw.text((2, y + 3), str(block_index), fill=255)
        for column, images in enumerate((japanese_images, english_images, korean_images)):
            preview = images[block_index]
            ratio = min(
                1.0,
                (cell_width - 8) / max(1, preview.width),
                (row_height - 6) / max(1, preview.height),
            )
            if ratio < 1:
                preview = preview.resize(
                    (
                        max(1, round(preview.width * ratio)),
                        max(1, round(preview.height * ratio)),
                    ),
                    Image.Resampling.LANCZOS,
                )
            page.paste(preview, (40 + column * cell_width + 4, y + 3))
    path = output / f"ui_group_{bitmap}_jp_en_ko.png"
    page.save(path)
    return str(path.resolve())


def main(argv: list[str] | None = None) -> int:
    global FONT

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--japanese", type=Path, required=True)
    parser.add_argument("--english", type=Path, required=True)
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument(
        "--font",
        type=Path,
        default=DEFAULT_FONT,
        help="path to malgunbd.ttf; the pinned SHA-256 is always verified",
    )
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--preview-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    FONT = args.font.expanduser().resolve()

    protected_inputs = {
        args.source.resolve(),
        args.japanese.resolve(),
        args.english.resolve(),
        args.mapping.resolve(),
        FONT,
    }
    output_path = args.output.resolve()
    report_path = args.report.resolve()
    if output_path in protected_inputs:
        raise ValueError("refusing to overwrite an input file with the rebuilt container")
    if report_path in protected_inputs or report_path == output_path:
        raise ValueError("refusing to overwrite an input or output file with the report")
    if not FONT.exists():
        raise FileNotFoundError(FONT)
    actual_font_hash = sha256(FONT.read_bytes())
    if actual_font_hash != FONT_SHA256:
        raise ValueError(
            f"font SHA-256 drift: expected {FONT_SHA256}, got {actual_font_hash}"
        )

    mapping_bytes = args.mapping.read_bytes()
    mapping_hash = sha256(mapping_bytes)
    mapping = json.loads(mapping_bytes.decode("utf-8"))
    rows = mapping["records"]
    grouped: dict[int, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["bitmap_block"])].append(row)
    unsupported = sorted(set(grouped) - set(SUPPORTED_BITMAPS))
    if not grouped or unsupported:
        raise ValueError(
            f"unsupported or empty mapping bitmap set: {sorted(grouped)}; "
            f"supported={list(SUPPORTED_BITMAPS)}"
        )
    duplicate_scr = [
        index
        for index in {int(row["block_index"]) for row in rows}
        if sum(int(item["block_index"]) == index for item in rows) != 1
    ]
    if duplicate_scr:
        raise ValueError(f"duplicate mapping SCR indices: {duplicate_scr}")

    source = add00_tools.parse_container(args.source)
    japanese = add00_tools.parse_container(args.japanese)
    english = add00_tools.parse_container(args.english)
    if not (len(source.blocks) == len(japanese.blocks) == len(english.blocks)):
        raise ValueError("reference containers have different block counts")

    output_bytes = bytearray(source.source)
    all_allowed: set[int] = set()
    group_reports: list[dict[str, object]] = []
    all_render_records: list[dict[str, object]] = []
    previews: dict[
        int,
        tuple[
            dict[int, Image.Image],
            dict[int, Image.Image],
            dict[int, Image.Image],
        ],
    ] = {}

    for bitmap in sorted(grouped):
        scr_blocks = group_scr_blocks(source, bitmap)
        mapped_blocks = sorted(int(row["block_index"]) for row in grouped[bitmap])
        if mapped_blocks != scr_blocks:
            raise ValueError(
                f"bitmap {bitmap} map coverage drift: mapped={mapped_blocks} actual={scr_blocks}"
            )
        if source.blocks[bitmap][:4] != b"BMP\x06":
            raise ValueError(f"bitmap {bitmap} is not GX I4")
        source_atlas = add00_tools.decode_i4(source.blocks[bitmap])
        english_atlas = add00_tools.decode_i4(english.blocks[bitmap])
        japanese_atlas = add00_tools.decode_i4(japanese.blocks[bitmap])
        if source_atlas.size != english_atlas.size:
            raise ValueError(
                f"bitmap {bitmap} source/English topology differs: "
                f"{source_atlas.size} != {english_atlas.size}"
            )
        capacity = (source_atlas.width // 8) * (source_atlas.height // 8)

        source_images = {
            index: render_gx10(source_atlas, source.blocks[index]) for index in scr_blocks
        }
        english_images = {
            index: render_gx10(english_atlas, english.blocks[index]) for index in scr_blocks
        }
        japanese_images = {
            index: render_gx10(japanese_atlas, japanese.blocks[index]) for index in scr_blocks
        }
        for index in scr_blocks:
            if source_images[index].size != english_images[index].size:
                raise ValueError(
                    f"SCR {index} source/English dimensions differ: "
                    f"{source_images[index].size} != {english_images[index].size}"
                )

        identity_atlas, identity_entries, identity_tiles = pack_images(
            source_atlas.size, source_images
        )
        identity_failures = []
        for index in scr_blocks:
            width, height = struct.unpack_from(">II", source.blocks[index], 4)
            rebuilt = render_entries(
                identity_atlas, width, height, identity_entries[index]
            )
            if ImageChops.difference(rebuilt, source_images[index]).getbbox():
                identity_failures.append(index)
        if identity_failures:
            raise ValueError(
                f"bitmap {bitmap} identity preflight failed: {identity_failures}"
            )

        by_scr = {int(row["block_index"]): row for row in grouped[bitmap]}
        target_images: dict[int, Image.Image] = {}
        group_render_records: list[dict[str, object]] = []
        for index in scr_blocks:
            row = by_scr[index]
            action = str(row["action"])
            if action == "preserve":
                target_images[index] = english_images[index]
                continue
            if action != "replace_with_korean":
                raise ValueError(f"unsupported action {action!r} for SCR {index}")
            korean = str(row["korean_proposal"]).strip()
            if not korean:
                raise ValueError(f"empty Korean proposal for SCR {index}")
            if JAPANESE_RE.search(korean):
                raise ValueError(f"Japanese remains in Korean proposal {index}: {korean!r}")
            if ASCII_WORD_RE.search(korean):
                raise ValueError(f"English word remains in Korean proposal {index}: {korean!r}")
            target, render_record = render_korean_label(
                bitmap, index, english_images[index], korean
            )
            target_images[index] = target
            group_render_records.append(render_record)
            all_render_records.append({"bitmap_block": bitmap, **render_record})

        rebuilt_atlas, entries_by_scr, tile_count = pack_images(
            source_atlas.size, target_images
        )
        atlas_replacement = add00_tools.encode_i4(
            rebuilt_atlas, source.blocks[bitmap]
        )
        atlas_start = source.offsets[bitmap]
        output_bytes[atlas_start : atlas_start + len(atlas_replacement)] = atlas_replacement
        for index in scr_blocks:
            block = bytearray(source.blocks[index])
            width, height = struct.unpack_from(">II", block, 4)
            entries = entries_by_scr[index]
            if len(entries) != width * height:
                raise AssertionError("visible SCR entry count changed")
            struct.pack_into(f">{len(entries)}H", block, 32, *entries)
            start = source.offsets[index]
            output_bytes[start : start + len(block)] = block

        palette_blocks = []
        for index in range(bitmap + 1, scr_blocks[0]):
            if source.blocks[index][:4] == b"BMP\x07":
                palette_blocks.append(index)
        all_allowed.update((bitmap, *scr_blocks))
        group_reports.append(
            {
                "bitmap_block": bitmap,
                "bitmap_dimensions": list(source_atlas.size),
                "tile_capacity": capacity,
                "identity_preflight_tiles": identity_tiles,
                "identity_preflight_free_tiles": capacity - identity_tiles,
                "identity_round_trip_failures": identity_failures,
                "production_tiles": tile_count,
                "production_free_tiles": capacity - tile_count,
                "scr_count": len(scr_blocks),
                "translation_count": len(group_render_records),
                "preserve_count": len(scr_blocks) - len(group_render_records),
                "palette_blocks": palette_blocks,
                "render_records": group_render_records,
            }
        )
        previews[bitmap] = (japanese_images, english_images, target_images)

    built = bytes(output_bytes)
    verified = add00_tools.parse_container(built)
    if len(verified.source) != len(source.source) or verified.offsets != source.offsets:
        raise ValueError("fixed-layout outer structure changed")

    visual_failures: list[int] = []
    palette_hashes: dict[str, dict[str, object]] = {}
    for group_report in group_reports:
        bitmap = int(group_report["bitmap_block"])
        atlas = add00_tools.decode_i4(verified.blocks[bitmap])
        target_images = previews[bitmap][2]
        for index, expected in target_images.items():
            actual = render_gx10(atlas, verified.blocks[index])
            if ImageChops.difference(actual, expected).getbbox():
                visual_failures.append(index)
        if verified.blocks[bitmap][:32] != source.blocks[bitmap][:32]:
            raise ValueError(f"BMP6 header changed for bitmap {bitmap}")
        for index in group_report["palette_blocks"]:
            unchanged = verified.blocks[index] == source.blocks[index]
            palette_hashes[str(index)] = {
                "before": sha256(source.blocks[index]),
                "after": sha256(verified.blocks[index]),
                "unchanged": unchanged,
            }
            if not unchanged:
                raise ValueError(f"palette block {index} changed")

    changed = [
        index
        for index, (before, after) in enumerate(zip(source.blocks, verified.blocks))
        if before != after
    ]
    unexpected = sorted(set(changed) - all_allowed)
    if visual_failures or unexpected:
        raise ValueError(
            f"verification failed: visual={visual_failures} unexpected={unexpected}"
        )

    args.preview_dir.mkdir(parents=True, exist_ok=True)
    preview_scr = args.preview_dir / "scr"
    preview_scr.mkdir(exist_ok=True)
    contact_sheets: list[str] = []
    for group_report in group_reports:
        bitmap = int(group_report["bitmap_block"])
        japanese_images, english_images, target_images = previews[bitmap]
        scr_blocks = sorted(target_images)
        for index, image in target_images.items():
            image.save(preview_scr / f"scr_{index:04d}_ko.png")
        contact_sheets.append(
            build_contact_sheet(
                bitmap,
                scr_blocks,
                japanese_images,
                english_images,
                target_images,
                args.preview_dir,
            )
        )

    # Publish the binary only after every in-memory structural and visual
    # verification has passed, so a failed run cannot leave a bad artifact.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(built)

    report = {
        "schema": "srw-gc-add00-small-ui-atlas-repack-v1",
        "source": str(args.source.resolve()),
        "source_sha256": sha256(source.source),
        "japanese_reference": str(args.japanese.resolve()),
        "japanese_reference_sha256": sha256(japanese.source),
        "english_reference": str(args.english.resolve()),
        "english_reference_sha256": sha256(english.source),
        "mapping": str(args.mapping.resolve()),
        "mapping_sha256": mapping_hash,
        "font": {"path": str(FONT), "sha256": actual_font_hash},
        "renderer": {
            "pillow": PIL.__version__,
            "freetype": features.version("freetype2"),
        },
        "output": str(args.output.resolve()),
        "output_sha256": sha256(built),
        "size": len(built),
        "outer_offsets_identical": verified.offsets == source.offsets,
        "bitmap_blocks": list(sorted(grouped)),
        "scr_count": len(rows),
        "translation_count": len(all_render_records),
        "preserve_count": len(rows) - len(all_render_records),
        "english_word_residuals_in_korean_fields": [
            row for row in all_render_records if row["ascii_words"]
        ],
        "groups": group_reports,
        "palette_blocks": palette_hashes,
        "changed_blocks": changed,
        "unexpected_changed_blocks": unexpected,
        "visual_round_trip_failures": visual_failures,
        "preview_contact_sheets": contact_sheets,
        "status": "pass",
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output": report["output"],
                "output_sha256": report["output_sha256"],
                "size": report["size"],
                "translation_count": report["translation_count"],
                "preserve_count": report["preserve_count"],
                "groups": [
                    {
                        "bitmap_block": row["bitmap_block"],
                        "tiles": row["production_tiles"],
                        "capacity": row["tile_capacity"],
                        "translations": row["translation_count"],
                    }
                    for row in group_reports
                ],
                "status": report["status"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
