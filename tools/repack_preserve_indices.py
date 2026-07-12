#!/usr/bin/env python3
"""Rebuild translated add00 UI maps without moving any pre-existing atlas tile.

The earlier UI builders compacted each atlas and rewrote every following SCR.
That is unsafe because some tiles which are not present in the static SCRs are
selected dynamically by the game.  This builder keeps every source tile at
its original index, appends new Korean tiles, and rewrites translated SCRs
only.  Preserved SCR blocks and all unrelated blocks remain byte-identical.

The 256x240 ability-name atlas (block 438) already occupies 960 of the 1024
indices available to the conventional SCR format.  Its Korean labels are
therefore rendered as one compact 8x8 Malgun Gothic Bold glyph per character;
the remaining groups retain the previously approved full-canvas rendering.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
from collections import defaultdict
from pathlib import Path

import PIL
from PIL import Image, ImageChops, ImageDraw, ImageFont, features


TOOLS_DIR = Path(__file__).resolve().parent
REPOSITORY_DIR = TOOLS_DIR.parent
sys.path.insert(0, str(TOOLS_DIR))
import add00_tools  # noqa: E402

from repack_small_ui_atlases import (  # noqa: E402
    FLIP_FLAGS,
    flip_tile,
    image_tiles,
    render_gx10,
)

from extract_scr_atlas import render_direct  # noqa: E402


WINDOWS_DIR = os.environ.get("WINDIR")
DEFAULT_FONT = (
    Path(WINDOWS_DIR) / "Fonts" / "malgunbd.ttf"
    if WINDOWS_DIR
    else Path("malgunbd.ttf")
)
FONT = DEFAULT_FONT
FONT_SHA256 = "E8CBC0B2AFCC14FB45DFB6086D5102C0B23A96E7B6E708F3122ACDE1B86C9082"
DIRECT_BITMAP = 518
COMPACT_BITMAP = 438
SMALL_BITMAPS = (334, 355, 433, 438, 923, 930, 947, 952, 959, 2714, 3489)
TARGET_BITMAPS = (*SMALL_BITMAPS[:4], DIRECT_BITMAP, *SMALL_BITMAPS[4:])


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def quantize_i4(image: Image.Image) -> Image.Image:
    output = Image.new("L", image.size)
    output.putdata(
        [max(0, min(15, (value + 8) // 17)) * 17 for value in image.getdata()]
    )
    return output


def scr_group(container: add00_tools.Add00Container, bitmap: int) -> list[int]:
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


def source_tiles(atlas: Image.Image) -> list[bytes]:
    return list(image_tiles(atlas))


def compact_glyph(character: str) -> Image.Image:
    """Render one legible Korean glyph into one 8x8 I4 tile."""

    if character.isspace():
        return Image.new("L", (8, 8), 0)
    probe = Image.new("L", (8, 8), 0)
    draw = ImageDraw.Draw(probe)
    chosen = None
    for size in range(11, 5, -1):
        font = ImageFont.truetype(str(FONT), size)
        box = draw.textbbox((0, 0), character, font=font)
        width, height = box[2] - box[0], box[3] - box[1]
        if width <= 8 and height <= 8:
            chosen = font, box, width, height
            break
    if chosen is None:
        raise ValueError(f"cannot fit compact glyph {character!r}")
    font, box, width, height = chosen
    x = (8 - width) // 2 - box[0]
    y = (8 - height) // 2 - box[1]
    draw.text((x, y), character, font=font, fill=255)
    return quantize_i4(probe)


def compact_label(width: int, height: int, text: str) -> Image.Image:
    """Compose one 8x8 tile per character into the existing SCR canvas."""

    characters = list(text)
    if len(characters) > width:
        raise ValueError(f"compact label {text!r} exceeds {width} tiles")
    image = Image.new("L", (width * 8, height * 8), 0)
    start_x = (width - len(characters)) // 2
    row = max(0, (height - 1) // 2)
    for position, character in enumerate(characters):
        image.paste(compact_glyph(character), ((start_x + position) * 8, row * 8))
    return image


def build_expanded_bitmap(
    template: bytes,
    source_atlas: Image.Image,
    appended: list[bytes],
) -> tuple[bytes, Image.Image, int]:
    tiles_x = source_atlas.width // 8
    old_capacity = tiles_x * (source_atlas.height // 8)
    old_raster_end = 32 + source_atlas.width * source_atlas.height // 2
    if len(template) != old_raster_end:
        raise ValueError("target BMP contains unsupported trailing data")
    if any(len(pattern) != 64 for pattern in appended):
        raise ValueError("one or more appended tiles are not 8x8 L patterns")
    needed = old_capacity + len(appended)
    rows = (needed + tiles_x - 1) // tiles_x
    new_height = rows * 8
    expanded = Image.new("L", (source_atlas.width, new_height), 0)
    expanded.paste(source_atlas, (0, 0))
    for offset, pattern in enumerate(appended):
        tile_index = old_capacity + offset
        expanded.paste(
            Image.frombytes("L", (8, 8), pattern),
            ((tile_index % tiles_x) * 8, (tile_index // tiles_x) * 8),
        )

    header = bytearray(template[:32])
    struct.pack_into(">II", header, 8, source_atlas.width, new_height)
    blank_template = bytes(header) + bytes(source_atlas.width * new_height // 2)
    encoded = add00_tools.encode_i4(expanded, blank_template)
    if encoded[32:old_raster_end] != template[32:old_raster_end]:
        raise RuntimeError("expansion changed one or more original atlas tile bytes")
    if encoded[:12] != template[:12] or encoded[16:32] != template[16:32]:
        raise RuntimeError("expansion changed BMP header fields other than height")
    return encoded, expanded, rows * tiles_x


def rebuild_container(blocks: list[bytes]) -> tuple[bytes, tuple[int, ...]]:
    table_size = len(blocks) * 4
    if table_size % add00_tools.ALIGNMENT:
        raise ValueError("offset table is unexpectedly unaligned")
    offsets: list[int] = []
    cursor = table_size
    for block in blocks:
        if cursor % add00_tools.ALIGNMENT or len(block) % add00_tools.ALIGNMENT:
            raise ValueError("block layout is unexpectedly unaligned")
        offsets.append(cursor)
        cursor += len(block)
    output = bytearray(struct.pack(f">{len(offsets)}I", *offsets))
    for block in blocks:
        output.extend(block)
    return bytes(output), tuple(offsets)


def load_actions(
    large_mapping: Path,
    small_mapping: Path,
) -> dict[int, dict[int, dict[str, object]]]:
    small_document = json.loads(small_mapping.read_text(encoding="utf-8"))
    if small_document.get("schema") != "srw-gc-ui-small-title-ko-v1":
        raise ValueError(f"unsupported small mapping schema: {small_mapping}")
    rows = small_document.get("records")
    if not isinstance(rows, list):
        raise ValueError("small mapping records must be a list")
    grouped: dict[int, dict[int, dict[str, object]]] = defaultdict(dict)
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("small mapping record must be an object")
        bitmap = int(row["bitmap_block"])
        block_index = int(row["block_index"])
        action = str(row.get("action"))
        if bitmap not in SMALL_BITMAPS:
            raise ValueError(f"small mapping contains unsupported bitmap {bitmap}")
        if action not in {"replace_with_korean", "preserve"}:
            raise ValueError(f"small mapping SCR {block_index} has invalid action {action!r}")
        korean_value = row.get("korean_proposal")
        if action == "replace_with_korean" and (
            not isinstance(korean_value, str) or not korean_value.strip()
        ):
            raise ValueError(f"small mapping SCR {block_index} has empty Korean text")
        if block_index in grouped[bitmap]:
            raise ValueError(f"duplicate small mapping SCR {block_index}")
        grouped[bitmap][block_index] = row
    if set(grouped) != set(SMALL_BITMAPS):
        raise ValueError(
            f"small mapping bitmap coverage drift: {sorted(grouped)} != {list(SMALL_BITMAPS)}"
        )

    large_document = json.loads(large_mapping.read_text(encoding="utf-8"))
    if large_document.get("schema") != "srw-gc-ui-block518-ko-v1":
        raise ValueError(f"unsupported large mapping schema: {large_mapping}")
    block518 = large_document.get("records")
    preserve_rows = large_document.get("preserve_blocks")
    expected_scr_count = int(large_document.get("expected_scr_count", -1))
    if not isinstance(block518, list) or not isinstance(preserve_rows, list):
        raise ValueError("large mapping records and preserve_blocks must be lists")
    large_indices: set[int] = set()
    for row in block518:
        if not isinstance(row, dict):
            raise ValueError("large mapping record must be an object")
        block_index = int(row["block_index"])
        if block_index in large_indices:
            raise ValueError(f"duplicate large mapping SCR {block_index}")
        korean_value = row.get("korean")
        if not isinstance(korean_value, str) or not korean_value.strip():
            raise ValueError(f"large mapping SCR {block_index} has empty Korean text")
        korean = korean_value.strip()
        large_indices.add(block_index)
        grouped[DIRECT_BITMAP][block_index] = {
            "bitmap_block": DIRECT_BITMAP,
            "block_index": block_index,
            "action": "replace_with_korean",
            "korean_proposal": korean,
        }
    preserve_indices = [int(index) for index in preserve_rows]
    if len(preserve_indices) != len(set(preserve_indices)):
        raise ValueError("large mapping contains duplicate preserve_blocks")
    overlap = sorted(large_indices & set(preserve_indices))
    if overlap:
        raise ValueError(f"large mapping replaces and preserves the same SCRs: {overlap}")
    for block_index in preserve_indices:
        grouped[DIRECT_BITMAP][block_index] = {
            "bitmap_block": DIRECT_BITMAP,
            "block_index": block_index,
            "action": "preserve",
        }
    if len(large_indices) + len(preserve_indices) != expected_scr_count:
        raise ValueError(
            "large mapping expected_scr_count does not match records plus preserve_blocks"
        )
    return dict(grouped)


def main() -> int:
    global FONT

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="pre-UI Japanese-base add00")
    parser.add_argument("approved", type=Path, help="current approved rendered UI add00")
    parser.add_argument("output", type=Path, help="new preserved-index add00 output")
    parser.add_argument("--report", type=Path, required=True, help="new JSON audit report")
    parser.add_argument("--preview-dir", type=Path, help="absent or empty preview directory")
    parser.add_argument(
        "--large-mapping",
        type=Path,
        default=REPOSITORY_DIR / "data" / "ui_block518_ko.json",
        help="block 518 translation/preserve mapping JSON",
    )
    parser.add_argument(
        "--small-mapping",
        type=Path,
        default=REPOSITORY_DIR / "data" / "ui_small_and_title_ko.json",
        help="small-atlas and title mapping JSON",
    )
    parser.add_argument(
        "--font",
        type=Path,
        default=DEFAULT_FONT,
        help="path to malgunbd.ttf; the pinned SHA-256 is always verified",
    )
    parser.add_argument(
        "--copy-block",
        type=int,
        action="append",
        default=[],
        help="copy an independently approved non-atlas block from approved input",
    )
    args = parser.parse_args()
    FONT = args.font.expanduser().resolve()
    source_path = args.source.expanduser().resolve()
    approved_path = args.approved.expanduser().resolve()
    large_mapping_path = args.large_mapping.expanduser().resolve()
    small_mapping_path = args.small_mapping.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    report_path = args.report.expanduser().resolve()

    protected_inputs = {
        source_path,
        approved_path,
        large_mapping_path,
        small_mapping_path,
        FONT,
    }
    if output_path in protected_inputs:
        raise ValueError("refusing to overwrite an input with the rebuilt container")
    if report_path in protected_inputs or report_path == output_path:
        raise ValueError("refusing to overwrite an input or output with the report")
    if source_path == approved_path:
        raise ValueError("source and approved visual-reference inputs must be different")
    if output_path.exists() or report_path.exists():
        raise FileExistsError("output and report paths must not already exist")
    preview_path = args.preview_dir.expanduser().resolve() if args.preview_dir else None
    if preview_path in protected_inputs or preview_path in {output_path, report_path}:
        raise ValueError("preview directory conflicts with an input or output path")
    if preview_path and (
        preview_path in output_path.parents or preview_path in report_path.parents
    ):
        raise ValueError("preview directory must not contain the output or report")
    if preview_path and preview_path.exists():
        if not preview_path.is_dir() or any(preview_path.iterdir()):
            raise FileExistsError("preview directory must be absent or empty")
    if len(args.copy_block) != len(set(args.copy_block)):
        raise ValueError("--copy-block contains duplicate block indices")

    actual_font_sha256 = sha256(FONT.read_bytes())
    if actual_font_sha256 != FONT_SHA256:
        raise ValueError("Malgun Gothic Bold font hash drift")
    source = add00_tools.parse_container(source_path)
    approved = add00_tools.parse_container(approved_path)
    if len(source.blocks) != len(approved.blocks):
        raise ValueError("source/approved block counts differ")

    identity_rebuild, _ = rebuild_container(list(source.blocks))
    if identity_rebuild != source.source:
        raise RuntimeError("source container cannot be rebuilt byte-identically")

    grouped = load_actions(large_mapping_path, small_mapping_path)
    blocks = list(source.blocks)
    allowed_changes: set[int] = set()
    target_images: dict[int, Image.Image] = {}
    group_reports: list[dict[str, object]] = []

    for bitmap, records in sorted(grouped.items()):
        direct = bitmap == DIRECT_BITMAP
        limit = 0x4000 if direct else 0x400
        source_atlas = add00_tools.decode_i4(source.blocks[bitmap])
        approved_atlas = add00_tools.decode_i4(approved.blocks[bitmap])
        old_tiles = source_tiles(source_atlas)
        old_capacity = len(old_tiles)
        group_scrs = scr_group(source, bitmap)
        approved_group_scrs = scr_group(approved, bitmap)
        if approved_group_scrs != group_scrs:
            raise ValueError(f"bitmap {bitmap} source/approved SCR topology differs")
        if set(records) != set(group_scrs):
            raise ValueError(
                f"bitmap {bitmap} mapping coverage drift: "
                f"mapped={sorted(records)} actual={group_scrs}"
            )
        if approved_atlas.size != source_atlas.size:
            raise ValueError(
                f"bitmap {bitmap} source/approved dimensions differ: "
                f"{source_atlas.size} != {approved_atlas.size}"
            )
        expected_mode = 1 if direct else 0
        if any(source.blocks[index][15] != expected_mode for index in group_scrs):
            raise ValueError(f"bitmap {bitmap} SCR index mode field drift")
        target_scrs = sorted(
            index
            for index, row in records.items()
            if str(row.get("action")) == "replace_with_korean"
        )
        preserved_scrs = sorted(set(group_scrs) - set(target_scrs))
        if not set(target_scrs).issubset(group_scrs):
            raise ValueError(f"bitmap {bitmap} mapping references another group")

        appended: list[bytes] = []
        entries_by_scr: dict[int, list[int]] = {}
        if direct:
            pattern_to_entry: dict[bytes, int] = {}
            for index, pattern in enumerate(old_tiles):
                pattern_to_entry.setdefault(pattern, index)

            def entry_for(pattern: bytes) -> int:
                entry = pattern_to_entry.get(pattern)
                if entry is not None:
                    return entry
                entry = old_capacity + len(appended)
                if entry >= limit:
                    raise ValueError(f"direct atlas {bitmap} exceeds 14-bit index space")
                pattern_to_entry[pattern] = entry
                appended.append(pattern)
                return entry

        else:
            pattern_to_entry = {}
            for index, pattern in enumerate(old_tiles):
                for flags in FLIP_FLAGS:
                    pattern_to_entry.setdefault(flip_tile(pattern, flags), index | flags)

            def entry_for(pattern: bytes) -> int:
                entry = pattern_to_entry.get(pattern)
                if entry is not None:
                    return entry
                variants = [flip_tile(pattern, flags) for flags in FLIP_FLAGS]
                canonical = min(variants)
                index = old_capacity + len(appended)
                if index >= limit:
                    raise ValueError(f"small atlas {bitmap} exceeds 10-bit index space")
                appended.append(canonical)
                for flags in FLIP_FLAGS:
                    pattern_to_entry.setdefault(flip_tile(canonical, flags), index | flags)
                return pattern_to_entry[pattern]

        for scr_index in target_scrs:
            width, height = struct.unpack_from(">II", source.blocks[scr_index], 4)
            approved_dimensions = struct.unpack_from(">II", approved.blocks[scr_index], 4)
            if approved_dimensions != (width, height):
                raise ValueError(f"SCR {scr_index} source/approved dimensions differ")
            if bitmap == COMPACT_BITMAP:
                text = str(records[scr_index]["korean_proposal"])
                image = compact_label(width, height, text)
            else:
                image = (
                    render_direct(approved_atlas, approved.blocks[scr_index])[0]
                    if direct
                    else render_gx10(approved_atlas, approved.blocks[scr_index])
                )
            target_images[scr_index] = image
            entries_by_scr[scr_index] = [entry_for(pattern) for pattern in image_tiles(image)]

        expanded_block, expanded_atlas, physical_capacity = build_expanded_bitmap(
            source.blocks[bitmap], source_atlas, appended
        )
        if physical_capacity > limit:
            raise ValueError(
                f"bitmap {bitmap} physical capacity {physical_capacity} exceeds {limit}"
            )
        blocks[bitmap] = expanded_block
        allowed_changes.add(bitmap)
        for scr_index, entries in entries_by_scr.items():
            block = bytearray(source.blocks[scr_index])
            width, height = struct.unpack_from(">II", block, 4)
            if len(entries) != width * height:
                raise AssertionError("visible SCR entry count changed")
            struct.pack_into(f">{len(entries)}H", block, 32, *entries)
            blocks[scr_index] = bytes(block)
            allowed_changes.add(scr_index)

        visual_failures = []
        for scr_index in target_scrs:
            actual = (
                render_direct(expanded_atlas, blocks[scr_index])[0]
                if direct
                else render_gx10(expanded_atlas, blocks[scr_index])
            )
            if ImageChops.difference(actual, target_images[scr_index]).getbbox():
                visual_failures.append(scr_index)
        preserved_failures = [
            index for index in preserved_scrs if blocks[index] != source.blocks[index]
        ]
        if visual_failures or preserved_failures:
            raise RuntimeError(
                f"bitmap {bitmap} verification failed: visual={visual_failures}, "
                f"preserved={preserved_failures}"
            )

        group_reports.append(
            {
                "bitmap_block": bitmap,
                "mode": "direct_14bit" if direct else "gx10_hv",
                "original_dimensions": list(source_atlas.size),
                "expanded_dimensions": list(expanded_atlas.size),
                "original_capacity": old_capacity,
                "appended_unique_tiles": len(appended),
                "physical_capacity": physical_capacity,
                "index_limit": limit,
                "maximum_written_index": max(
                    (entry & (0x3FFF if direct else 0x03FF))
                    for entries in entries_by_scr.values()
                    for entry in entries
                ),
                "target_scr_count": len(target_scrs),
                "preserved_scr_count": len(preserved_scrs),
                "target_scrs": target_scrs,
                "preserved_scrs": preserved_scrs,
                "all_original_tile_bytes_preserved": True,
                "preserved_scrs_byte_identical": not preserved_failures,
                "visual_round_trip_failures": visual_failures,
                "compact_one_tile_glyphs": bitmap == COMPACT_BITMAP,
            }
        )

    reserved_group_blocks = {
        index
        for bitmap in grouped
        for index in (bitmap, *scr_group(source, bitmap))
    }
    copy_blocks = sorted(args.copy_block)
    for index in copy_blocks:
        if not 0 <= index < len(blocks):
            raise ValueError(f"copy block index out of range: {index}")
        if index in reserved_group_blocks:
            raise ValueError(f"copy block {index} overlaps a guarded UI atlas group")
        if len(source.blocks[index]) != len(approved.blocks[index]):
            raise ValueError(f"copy block {index} changes block length")
        if source.blocks[index][:4] != approved.blocks[index][:4]:
            raise ValueError(f"copy block {index} changes block type")
        if source.blocks[index] == approved.blocks[index]:
            raise ValueError(f"copy block {index} is already identical")
        blocks[index] = approved.blocks[index]
        allowed_changes.add(index)

    built, offsets = rebuild_container(blocks)
    verified = add00_tools.parse_container(built)
    if verified.offsets != offsets:
        raise RuntimeError("rebuilt offset table failed to parse")
    changed = [
        index
        for index, (before, after) in enumerate(zip(source.blocks, verified.blocks))
        if before != after
    ]
    unexpected = sorted(set(changed) - allowed_changes)
    missing = sorted(allowed_changes - set(changed))
    if unexpected or missing:
        raise RuntimeError(f"changed-block guard failed: unexpected={unexpected}, missing={missing}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("xb") as handle:
        handle.write(built)

    preview_paths: list[str] = []
    if preview_path:
        preview_path.mkdir(parents=True, exist_ok=True)
        bitmap = COMPACT_BITMAP
        group = next(row for row in group_reports if row["bitmap_block"] == bitmap)
        atlas = add00_tools.decode_i4(verified.blocks[bitmap])
        rows = []
        for index in group["target_scrs"]:
            before = render_gx10(add00_tools.decode_i4(source.blocks[bitmap]), source.blocks[index])
            after = render_gx10(atlas, verified.blocks[index])
            rows.append((index, before, after))
        width = 900
        height = 50 * len(rows) + 24
        sheet = Image.new("L", (width, height), 0)
        draw = ImageDraw.Draw(sheet)
        draw.text((40, 4), "SOURCE", fill=255)
        draw.text((460, 4), "PRESERVED-INDEX KOREAN", fill=255)
        for row_number, (index, before, after) in enumerate(rows):
            y = 24 + row_number * 50
            draw.text((2, y + 4), str(index), fill=255)
            sheet.paste(before, (40, y))
            sheet.paste(after, (460, y))
        path = preview_path / "block438_compact_labels.png"
        sheet.save(path)
        preview_paths.append(str(path.resolve()))

    report = {
        "schema": "srw-gc-add00-preserved-index-ui-repack-v1",
        "status": "pass",
        "source": str(source_path),
        "source_sha256": sha256(source.source),
        "approved_render_source": str(approved_path),
        "approved_render_source_sha256": sha256(approved.source),
        "output": str(output_path),
        "output_sha256": sha256(built),
        "source_size": len(source.source),
        "output_size": len(built),
        "size_growth": len(built) - len(source.source),
        "block_count": len(verified.blocks),
        "changed_blocks": changed,
        "unexpected_changed_blocks": unexpected,
        "font": {"path": str(FONT), "sha256": actual_font_sha256},
        "renderer": {
            "pillow": PIL.__version__,
            "freetype": features.version("freetype2"),
        },
        "large_mapping": str(large_mapping_path),
        "large_mapping_sha256": sha256(large_mapping_path.read_bytes()),
        "small_mapping": str(small_mapping_path),
        "small_mapping_sha256": sha256(small_mapping_path.read_bytes()),
        "groups": group_reports,
        "copied_independent_blocks": copy_blocks,
        "preview_paths": preview_paths,
        "global_guards": {
            "all_preexisting_atlas_tile_bytes_preserved": all(
                row["all_original_tile_bytes_preserved"] for row in group_reports
            ),
            "all_preserved_scrs_byte_identical": all(
                row["preserved_scrs_byte_identical"] for row in group_reports
            ),
            "all_target_scrs_visual_round_trip": all(
                not row["visual_round_trip_failures"] for row in group_reports
            ),
            "all_other_blocks_byte_identical": not unexpected,
        },
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({
        "output": report["output"],
        "output_sha256": report["output_sha256"],
        "source_size": report["source_size"],
        "output_size": report["output_size"],
        "groups": [
            {
                "bitmap": row["bitmap_block"],
                "old_capacity": row["original_capacity"],
                "appended": row["appended_unique_tiles"],
                "new_capacity": row["physical_capacity"],
                "limit": row["index_limit"],
            }
            for row in group_reports
        ],
        "status": report["status"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
