#!/usr/bin/env python3
"""Rebuild add00's large direct-index UI atlas without tile collisions.

The old patch draws into shared atlas tiles.  One edited tile can therefore
corrupt several unrelated labels.  This builder first renders each SCR as
an independent image, optionally replaces selected canvases with Korean text,
deduplicates the resulting 8x8 tiles, and rewrites every SCR to the new tile
indices.  Input files are never modified in place.

Translation JSON format::

    {"records": [
      {"block_index": 524, "korean": "파일럿 상태", "align": "center"}
    ]}

Each mapped SCR is replaced as a complete canvas so no English glyph can
survive or leak through shared tiles.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import sys
from collections import deque
from pathlib import Path

import PIL
from PIL import Image, ImageChops, ImageDraw, ImageFont, features


TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS_DIR))
import add00_tools  # noqa: E402

from extract_scr_atlas import contact_sheets, render_direct  # noqa: E402


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


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def render_korean(
    size: tuple[int, int],
    text: str,
    align: str,
    background: Image.Image | None = None,
) -> tuple[Image.Image, int]:
    image = background.copy() if background is not None else Image.new("L", size, 0)
    draw = ImageDraw.Draw(image)
    width, height = size
    horizontal_margin = 1 if width <= 8 else 2
    spacing = -2 if "\n" in text else 0
    chosen = None
    for font_size in range(min(28, height + 3), 5, -1):
        font = ImageFont.truetype(str(FONT), font_size)
        box = draw.multiline_textbbox(
            (0, 0), text, font=font, spacing=spacing, align="center"
        )
        text_width = box[2] - box[0]
        text_height = box[3] - box[1]
        if text_width <= width - horizontal_margin * 2 and text_height <= height - 2:
            chosen = font, font_size, box, text_width, text_height
            break
    if chosen is None:
        raise ValueError(f"cannot fit {text!r} into {size}")
    font, font_size, box, text_width, text_height = chosen
    if align == "left":
        x = horizontal_margin - box[0]
    elif align == "right":
        x = width - text_width - horizontal_margin - box[0]
    elif align == "center":
        x = (width - text_width) // 2 - box[0]
    else:
        raise ValueError(f"unsupported alignment: {align}")
    y = (height - text_height) // 2 - box[1]
    if width > 8:
        draw.multiline_text(
            (x + 1, y + 1), text, font=font, spacing=spacing,
            align="center", fill=64,
        )
    draw.multiline_text(
        (x, y), text, font=font, spacing=spacing, align="center", fill=255
    )
    return image, font_size


def image_tiles(image: Image.Image) -> list[bytes]:
    if image.width % 8 or image.height % 8:
        raise ValueError(f"SCR canvas is not 8x8 aligned: {image.size}")
    return [
        image.crop((x, y, x + 8, y + 8)).tobytes()
        for y in range(0, image.height, 8)
        for x in range(0, image.width, 8)
    ]


def quantize_i4(image: Image.Image) -> Image.Image:
    """Apply the exact rounding used by add00_tools.encode_i4."""

    output = Image.new("L", image.size)
    output.putdata([max(0, min(15, (value + 8) // 17)) * 17 for value in image.getdata()])
    return output


def common_bottom_decoration(source: Image.Image, japanese: Image.Image) -> Image.Image:
    """Keep invariant long lower-half components, dropping both languages.

    Japanese and English labels share the bevel/underline art but not their
    glyphs.  Exact common pixels below the midline isolate that art.  A small
    connected-component filter removes coincidental overlaps between glyphs.
    """

    output = Image.new("L", source.size, 0)
    width = min(source.width, japanese.width)
    height = min(source.height, japanese.height)
    source_pixels = source.load()
    japanese_pixels = japanese.load()
    candidates: set[tuple[int, int]] = set()
    for y in range(height // 2, height):
        for x in range(width):
            value = source_pixels[x, y]
            if 0 < value < 220 and value == japanese_pixels[x, y]:
                candidates.add((x, y))

    kept: list[list[tuple[int, int]]] = []
    while candidates:
        first = candidates.pop()
        component = [first]
        queue = deque([first])
        while queue:
            x, y = queue.popleft()
            for offset_y in (-1, 0, 1):
                for offset_x in (-1, 0, 1):
                    if not offset_x and not offset_y:
                        continue
                    neighbor = (x + offset_x, y + offset_y)
                    if neighbor in candidates:
                        candidates.remove(neighbor)
                        component.append(neighbor)
                        queue.append(neighbor)
        xs = [point[0] for point in component]
        ys = [point[1] for point in component]
        component_width = max(xs) - min(xs) + 1
        component_height = max(ys) - min(ys) + 1
        if (
            len(component) >= 20
            and component_width >= 12
            and component_width >= component_height * 1.8
        ):
            kept.append(component)

    output_pixels = output.load()
    for component in kept:
        for x, y in component:
            output_pixels[x, y] = source_pixels[x, y]
    return output


def main(argv: list[str] | None = None) -> int:
    global FONT

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--translations", type=Path)
    parser.add_argument("--japanese", type=Path)
    parser.add_argument("--english", type=Path)
    parser.add_argument("--bitmap", type=int, default=518)
    parser.add_argument(
        "--font",
        type=Path,
        default=DEFAULT_FONT,
        help="path to malgunbd.ttf; the pinned SHA-256 is always verified",
    )
    parser.add_argument("--report", type=Path)
    parser.add_argument("--preview-dir", type=Path)
    args = parser.parse_args(argv)
    FONT = args.font.expanduser().resolve()
    report_path = args.report or args.output.with_suffix(args.output.suffix + ".report.json")
    protected_inputs = {
        args.source.resolve(),
        FONT,
        *(
            path.resolve()
            for path in (args.translations, args.japanese, args.english)
            if path is not None
        ),
    }
    output_path = args.output.resolve()
    report_resolved = report_path.resolve()
    if output_path in protected_inputs:
        raise ValueError("refusing to overwrite an input file with the rebuilt container")
    if report_resolved in protected_inputs or report_resolved == output_path:
        raise ValueError("refusing to overwrite an input or output file with the report")
    if not FONT.exists():
        raise FileNotFoundError(FONT)
    actual_font_sha256 = sha256(FONT.read_bytes())
    if actual_font_sha256 != FONT_SHA256:
        raise ValueError(
            f"font SHA-256 drift: expected {FONT_SHA256}, got {actual_font_sha256}"
        )

    translation_records: list[dict[str, object]] = []
    translation_mapping_sha256 = None
    expected_scr_count = None
    declared_preserve_blocks: set[int] | None = None
    if args.translations:
        translation_mapping_bytes = args.translations.read_bytes()
        translation_mapping_sha256 = sha256(translation_mapping_bytes)
        translation_document = json.loads(translation_mapping_bytes.decode("utf-8"))
        translation_records = translation_document["records"]
        if "expected_scr_count" in translation_document:
            expected_scr_count = int(translation_document["expected_scr_count"])
        if "preserve_blocks" in translation_document:
            preserve_rows = [int(value) for value in translation_document["preserve_blocks"]]
            declared_preserve_blocks = set(preserve_rows)
            if len(declared_preserve_blocks) != len(preserve_rows):
                raise ValueError("translation JSON contains duplicate preserve_blocks")
    translations = {int(row["block_index"]): row for row in translation_records}
    if len(translations) != len(translation_records):
        raise ValueError("translation JSON contains duplicate block_index values")

    source = add00_tools.parse_container(args.source)
    atlas = add00_tools.decode_i4(source.blocks[args.bitmap])
    japanese_container = add00_tools.parse_container(args.japanese) if args.japanese else None
    japanese_atlas = (
        add00_tools.decode_i4(japanese_container.blocks[args.bitmap])
        if japanese_container is not None
        else None
    )
    english_container = add00_tools.parse_container(args.english) if args.english else None
    english_atlas = (
        add00_tools.decode_i4(english_container.blocks[args.bitmap])
        if english_container is not None
        else None
    )
    atlas_tiles_x = atlas.width // 8
    capacity = atlas_tiles_x * (atlas.height // 8)
    if capacity <= 1024:
        raise ValueError("this prototype is only for the large direct-index atlas")

    scr_blocks: list[int] = []
    target_images: dict[int, Image.Image] = {}
    render_records: list[dict[str, object]] = []
    index = args.bitmap + 1
    while index < len(source.blocks):
        block = source.blocks[index]
        if block[:4] in (b"BMP\x06", b"BMP\x09"):
            break
        if block[:4] == b"SCR\0":
            image, details = render_direct(atlas, block)
            if index in translations:
                row = translations[index]
                korean = str(row["korean"]).strip()
                if not korean:
                    raise ValueError(f"empty Korean translation for SCR {index}")
                if JAPANESE_RE.search(korean):
                    raise ValueError(
                        f"Japanese remains in Korean translation for SCR {index}: "
                        f"{korean!r}"
                    )
                if ASCII_WORD_RE.search(korean):
                    raise ValueError(
                        f"English word remains in Korean translation for SCR {index}: "
                        f"{korean!r}"
                    )
                reference_view = image
                if english_container is not None and english_atlas is not None:
                    reference_view, _ = render_direct(
                        english_atlas, english_container.blocks[index]
                    )
                if reference_view.size != image.size:
                    raise ValueError(
                        f"English/current SCR dimensions differ for {index}: "
                        f"{reference_view.size} != {image.size}"
                    )
                detected_decoration = None
                detected_decoration_pixels = 0
                if japanese_container is not None and japanese_atlas is not None:
                    japanese_view, _ = render_direct(
                        japanese_atlas, japanese_container.blocks[index]
                    )
                    detected_decoration = common_bottom_decoration(reference_view, japanese_view)
                    detected_decoration_pixels = sum(
                        value != 0 for value in detected_decoration.getdata()
                    )

                decoration_explicit = "decoration" in row and str(row["decoration"]) != "auto"
                if decoration_explicit:
                    decoration_mode = str(row["decoration"])
                else:
                    decoration_mode = (
                        "common_bottom" if detected_decoration_pixels else "none"
                    )
                if decoration_mode == "common_bottom":
                    if detected_decoration is None:
                        raise ValueError(
                            f"SCR {index} needs JP/EN references for common_bottom decoration"
                        )
                    decoration = detected_decoration
                    decoration_pixels = detected_decoration_pixels
                elif decoration_mode == "none":
                    decoration = None
                    decoration_pixels = 0
                else:
                    raise ValueError(
                        f"unsupported decoration mode for SCR {index}: {decoration_mode}"
                    )
                align_explicit = "align" in row and str(row["align"]) != "auto"
                align = (
                    str(row["align"])
                    if align_explicit
                    else ("left" if decoration_mode == "common_bottom" else "center")
                )
                image, font_size = render_korean(image.size, korean, align, decoration)
                image = quantize_i4(image)
                render_records.append(
                    {
                        "block_index": index,
                        "korean": korean,
                        "align": align,
                        "font_size": font_size,
                        "decoration": decoration_mode,
                        "decoration_pixels": decoration_pixels,
                        "decoration_auto_selected": not decoration_explicit,
                        "align_auto_selected": not align_explicit,
                        "dimensions": list(image.size),
                        "ascii_words": ASCII_WORD_RE.findall(korean),
                    }
                )
            scr_blocks.append(index)
            target_images[index] = image
        index += 1
    unknown = sorted(set(translations) - set(scr_blocks))
    if unknown:
        raise ValueError(f"translations reference SCRs outside the atlas group: {unknown}")
    if expected_scr_count is not None and len(scr_blocks) != expected_scr_count:
        raise ValueError(
            f"atlas SCR count drift: expected {expected_scr_count}, got {len(scr_blocks)}"
        )
    if declared_preserve_blocks is not None:
        unknown_preserve = sorted(declared_preserve_blocks - set(scr_blocks))
        overlap = sorted(declared_preserve_blocks & set(translations))
        uncovered = sorted(
            set(scr_blocks) - set(translations) - declared_preserve_blocks
        )
        if unknown_preserve or overlap or uncovered:
            raise ValueError(
                "translation coverage drift: "
                f"unknown_preserve={unknown_preserve} overlap={overlap} "
                f"uncovered={uncovered}"
            )

    blank = bytes(64)
    patterns: list[bytes] = [blank]
    pattern_to_index = {blank: 0}
    map_indices: dict[int, list[int]] = {}
    for block_index in scr_blocks:
        indices: list[int] = []
        for pattern in image_tiles(target_images[block_index]):
            if pattern not in pattern_to_index:
                if len(patterns) >= capacity:
                    raise ValueError(
                        f"deduplicated atlas needs more than {capacity} tiles; "
                        f"failed while packing SCR {block_index}"
                    )
                pattern_to_index[pattern] = len(patterns)
                patterns.append(pattern)
            indices.append(pattern_to_index[pattern])
        map_indices[block_index] = indices

    rebuilt_atlas = Image.new("L", atlas.size, 0)
    for tile_index, pattern in enumerate(patterns):
        tile = Image.frombytes("L", (8, 8), pattern)
        rebuilt_atlas.paste(
            tile,
            ((tile_index % atlas_tiles_x) * 8, (tile_index // atlas_tiles_x) * 8),
        )

    output = bytearray(source.source)
    atlas_replacement = add00_tools.encode_i4(rebuilt_atlas, source.blocks[args.bitmap])
    atlas_start = source.offsets[args.bitmap]
    output[atlas_start : atlas_start + len(atlas_replacement)] = atlas_replacement
    for block_index in scr_blocks:
        block = bytearray(source.blocks[block_index])
        width, height = struct.unpack_from(">II", block, 4)
        indices = map_indices[block_index]
        if len(indices) != width * height:
            raise AssertionError("packed SCR entry count changed")
        struct.pack_into(f">{len(indices)}H", block, 32, *indices)
        start = source.offsets[block_index]
        output[start : start + len(block)] = block

    built = bytes(output)
    verified = add00_tools.parse_container(built)
    if verified.offsets != source.offsets or len(verified.source) != len(source.source):
        raise ValueError("fixed-layout outer structure changed")
    if verified.blocks[args.bitmap][:32] != source.blocks[args.bitmap][:32]:
        raise ValueError("BMP6 atlas header changed")

    verified_atlas = add00_tools.decode_i4(verified.blocks[args.bitmap])
    visual_failures: list[int] = []
    for block_index in scr_blocks:
        actual, _ = render_direct(verified_atlas, verified.blocks[block_index])
        if ImageChops.difference(actual, target_images[block_index]).getbbox():
            visual_failures.append(block_index)
    changed = [
        block_index
        for block_index, (before, after) in enumerate(zip(source.blocks, verified.blocks))
        if before != after
    ]
    allowed = {args.bitmap, *scr_blocks}
    unexpected = sorted(set(changed) - allowed)
    if visual_failures or unexpected:
        raise ValueError(
            f"repack verification failed: visual={visual_failures} unexpected={unexpected}"
        )

    palette_blocks = [
        block_index
        for block_index in range(args.bitmap + 1, scr_blocks[0])
        if source.blocks[block_index][:4] == b"BMP\x07"
    ]
    palette_hashes = {
        str(block_index): {
            "before": sha256(source.blocks[block_index]),
            "after": sha256(verified.blocks[block_index]),
            "unchanged": source.blocks[block_index] == verified.blocks[block_index],
        }
        for block_index in palette_blocks
    }
    if any(not value["unchanged"] for value in palette_hashes.values()):
        raise ValueError("one or more BMP7 palette blocks changed")

    input_horizontal_flips = 0
    input_vertical_flips = 0
    output_horizontal_flips = 0
    output_vertical_flips = 0
    for block_index in scr_blocks:
        for block, output_side in (
            (source.blocks[block_index], False),
            (verified.blocks[block_index], True),
        ):
            width, height = struct.unpack_from(">II", block, 4)
            values = struct.unpack_from(f">{(len(block) - 32) // 2}H", block, 32)[: width * height]
            horizontal = sum(bool(value & 0x4000) for value in values)
            vertical = sum(bool(value & 0x8000) for value in values)
            if output_side:
                output_horizontal_flips += horizontal
                output_vertical_flips += vertical
            else:
                input_horizontal_flips += horizontal
                input_vertical_flips += vertical

    preview_records: list[dict[str, object]] = []
    if args.preview_dir:
        args.preview_dir.mkdir(parents=True, exist_ok=True)
        preview_scr = args.preview_dir / "scr"
        preview_scr.mkdir(exist_ok=True)
        for row in render_records:
            block_index = int(row["block_index"])
            actual, _ = render_direct(verified_atlas, verified.blocks[block_index])
            preview_path = preview_scr / f"scr_{block_index:04d}_ko.png"
            actual.save(preview_path)
            preview_records.append(
                {
                    "block_index": block_index,
                    "preview": str(preview_path.resolve()),
                }
            )
        preview_sheets = (
            contact_sheets(preview_records, args.preview_dir) if preview_records else []
        )
    else:
        preview_sheets = []

    # Publish the binary only after every in-memory structural and visual
    # verification has passed, so a failed run cannot leave a bad artifact.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(built)

    report = {
        "schema": "srw-gc-add00-direct-scr-repack-v1",
        "source": str(args.source.resolve()),
        "source_sha256": sha256(source.source),
        "font": {
            "path": str(FONT),
            "sha256": actual_font_sha256,
        },
        "renderer": {
            "pillow": PIL.__version__,
            "freetype": features.version("freetype2"),
        },
        "translation_mapping": str(args.translations.resolve()) if args.translations else None,
        "translation_mapping_sha256": translation_mapping_sha256,
        "japanese_reference": str(args.japanese.resolve()) if args.japanese else None,
        "japanese_reference_sha256": (
            sha256(japanese_container.source) if japanese_container else None
        ),
        "english_reference": str(args.english.resolve()) if args.english else None,
        "english_reference_sha256": (
            sha256(english_container.source) if english_container else None
        ),
        "output": str(args.output.resolve()),
        "output_sha256": sha256(built),
        "size": len(built),
        "outer_offsets_identical": verified.offsets == source.offsets,
        "bitmap_block": args.bitmap,
        "bitmap_dimensions": list(atlas.size),
        "tile_capacity": capacity,
        "deduplicated_tiles": len(patterns),
        "free_tiles": capacity - len(patterns),
        "scr_count": len(scr_blocks),
        "translation_count": len(render_records),
        "untranslated_scr_count": len(scr_blocks) - len(render_records),
        "declared_preserve_blocks": (
            sorted(declared_preserve_blocks)
            if declared_preserve_blocks is not None
            else None
        ),
        "translations": render_records,
        "palette_blocks": palette_hashes,
        "flip_flags": {
            "input_horizontal": input_horizontal_flips,
            "input_vertical": input_vertical_flips,
            "output_horizontal": output_horizontal_flips,
            "output_vertical": output_vertical_flips,
            "note": (
                "final visible pixels are materialized into new tiles; "
                "rewritten SCR entries need no flip flags"
            ),
        },
        "preview_contact_sheets": preview_sheets,
        "visual_round_trip_failures": visual_failures,
        "unexpected_changed_blocks": unexpected,
        "changed_block_count": len(changed),
        "status": "pass",
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output": report["output"],
                "output_sha256": report["output_sha256"],
                "size": report["size"],
                "bitmap_block": report["bitmap_block"],
                "scr_count": report["scr_count"],
                "translation_count": report["translation_count"],
                "untranslated_scr_count": report["untranslated_scr_count"],
                "deduplicated_tiles": report["deduplicated_tiles"],
                "tile_capacity": report["tile_capacity"],
                "status": report["status"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
