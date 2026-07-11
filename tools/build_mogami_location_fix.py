#!/usr/bin/env python3
"""Correct the five ``最上重工`` location cards in the JP-base ADD00 build.

The earlier Korean graphic source used the literal Sino-Korean label
``최상중공``.  This script renders the canonical game-localization name
``모가미 중공`` into the same fixed-size GX I4 bitmap blocks, then invokes
the Japanese-base ADD00 builder with that corrected graphic source.

Only bitmap blocks 3557, 3567, 3569, 3791 and 3879 are allowed to differ
from the preceding JP-base Korean ADD00.  All headers, block sizes, offsets,
SCR maps, retail-Japanese topology and the rebuilt Korean title remain
unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw


TARGETS = {
    3557: "모가미 중공 격납고",
    3567: "모가미 중공 주변",
    3569: "모가미 중공",
    3791: "모가미 중공 연구실",
    3879: "모가미 중공 사장실",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def patch_graphic_source(
    source_path: Path,
    output_path: Path,
    preview_dir: Path,
    add00_tools,
    add00_build_korean,
) -> tuple[dict[str, object], object]:
    source = add00_tools.parse_container(source_path)
    output = bytearray(source.source)
    preview_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    before_images: list[Image.Image] = []
    after_images: list[Image.Image] = []

    for block_index, text in TARGETS.items():
        template = source.blocks[block_index]
        if template[:4] != b"BMP\x06":
            raise ValueError(f"block {block_index} is not a GX I4 bitmap")

        before = add00_tools.decode_i4(template)
        rendered, render_details = add00_build_korean.render_korean(text, before.size, 1)
        replacement = add00_tools.encode_i4(rendered, template)
        if len(replacement) != len(template) or replacement[:32] != template[:32]:
            raise ValueError(f"block {block_index} layout/header changed")

        start = source.offsets[block_index]
        output[start : start + len(template)] = replacement
        after = add00_tools.decode_i4(replacement)

        before_path = preview_dir / f"block_{block_index}_before.png"
        after_path = preview_dir / f"block_{block_index}_after.png"
        before.save(before_path)
        after.save(after_path)

        mapped_before_path = None
        mapped_after_path = None
        scr = source.blocks[block_index + 1]
        if scr[:4] == b"SCR\0":
            mapped_before = add00_tools.render_scr_tilemap(before, scr)
            mapped_after = add00_tools.render_scr_tilemap(after, scr)
            mapped_before_path = preview_dir / f"block_{block_index}_mapped_before.png"
            mapped_after_path = preview_dir / f"block_{block_index}_mapped_after.png"
            mapped_before.save(mapped_before_path)
            mapped_after.save(mapped_after_path)

        before_images.append(before)
        after_images.append(after)
        rows.append(
            {
                "block_index": block_index,
                "replacement_text": text,
                "dimensions": list(before.size),
                "block_length": len(template),
                "before_sha256": sha256(template),
                "after_sha256": sha256(replacement),
                "header_preserved": replacement[:32] == template[:32],
                "render": render_details,
                "preview_before": str(before_path.resolve()),
                "preview_after": str(after_path.resolve()),
                "mapped_preview_before": str(mapped_before_path.resolve()) if mapped_before_path else None,
                "mapped_preview_after": str(mapped_after_path.resolve()) if mapped_after_path else None,
            }
        )

    output_bytes = bytes(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(output_bytes)
    parsed = add00_tools.parse_container(output_bytes)
    changed = [
        index
        for index, (before, after) in enumerate(zip(source.blocks, parsed.blocks))
        if before != after
    ]
    if changed != sorted(TARGETS):
        raise ValueError(f"graphic source changed unexpected blocks: {changed}")
    if source.offsets != parsed.offsets or len(source.source) != len(parsed.source):
        raise ValueError("graphic source offsets or total size changed")

    make_contact_sheet(before_images, after_images, rows, preview_dir / "mogami_location_fix_contact_sheet.png")
    report = {
        "input": {
            "path": str(source_path.resolve()),
            "size": len(source.source),
            "sha256": sha256(source.source),
        },
        "output": {
            "path": str(output_path.resolve()),
            "size": len(output_bytes),
            "sha256": sha256(output_bytes),
        },
        "changed_blocks": changed,
        "offset_table_preserved": source.offsets == parsed.offsets,
        "non_target_changes": [],
        "records": rows,
    }
    return report, parsed


def make_contact_sheet(
    before_images: list[Image.Image],
    after_images: list[Image.Image],
    rows: list[dict[str, object]],
    output_path: Path,
) -> None:
    label_height = 28
    gap = 10
    width = max(image.width for image in before_images + after_images)
    row_height = max(image.height for image in before_images + after_images) * 2 + label_height * 2 + gap
    sheet = Image.new("L", (width, row_height * len(rows)), 0)
    draw = ImageDraw.Draw(sheet)
    y = 0
    for before, after, row in zip(before_images, after_images, rows):
        draw.text((4, y + 4), f"block {row['block_index']} BEFORE", fill=255)
        y += label_height
        sheet.paste(before, (0, y))
        y += before.height + gap
        draw.text((4, y + 4), f"block {row['block_index']} AFTER", fill=255)
        y += label_height
        sheet.paste(after, (0, y))
        y += after.height
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path)


def verify_final_against_previous(
    previous_path: Path,
    output_path: Path,
    add00_tools,
) -> dict[str, object]:
    previous = add00_tools.parse_container(previous_path)
    output = add00_tools.parse_container(output_path)
    if len(previous.blocks) != 3920 or len(output.blocks) != 3920:
        raise ValueError("unexpected JP-base ADD00 block count")
    changed = [
        index
        for index, (before, after) in enumerate(zip(previous.blocks, output.blocks))
        if before != after
    ]
    if changed != sorted(TARGETS):
        raise ValueError(f"final JP-base build changed unexpected blocks: {changed}")
    header_failures = [
        index
        for index in TARGETS
        if previous.blocks[index][:32] != output.blocks[index][:32]
        or len(previous.blocks[index]) != len(output.blocks[index])
    ]
    if header_failures:
        raise ValueError(f"final target layout/header failures: {header_failures}")
    if previous.offsets != output.offsets or len(previous.source) != len(output.source):
        raise ValueError("final JP-base output offsets or total size changed")
    return {
        "previous_path": str(previous_path.resolve()),
        "previous_sha256": sha256(previous.source),
        "output_path": str(output_path.resolve()),
        "output_sha256": sha256(output.source),
        "size": len(output.source),
        "block_count": len(output.blocks),
        "changed_blocks": changed,
        "expected_changed_blocks": sorted(TARGETS),
        "non_target_changes": [],
        "target_header_or_length_failures": header_failures,
        "offset_table_preserved": previous.offsets == output.offsets,
        "all_blocks_aligned_0x20": all(offset % 0x20 == 0 for offset in output.offsets),
        "round_trip_verified": True,
        "status": "pass",
    }


def main() -> int:
    workspace = Path(__file__).resolve().parents[2]
    output_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--korean-source",
        type=Path,
        default=workspace / "work/srw_gc_complete_patch/add00dat.complete_ko.ui.fixed_layout.bin",
    )
    parser.add_argument(
        "--previous-jp-build",
        type=Path,
        default=workspace / "work/jp_add00_rebuild_agent/add00dat.japanese_base.korean.bin",
    )
    parser.add_argument(
        "--japanese",
        type=Path,
        default=workspace / "work/srw_gc_iso_extract/original/add00dat.bin",
    )
    parser.add_argument(
        "--english-reference",
        type=Path,
        default=workspace / "work/srw_gc_iso_extract/english/add00dat.bin",
    )
    parser.add_argument(
        "--patched-korean-source",
        type=Path,
        default=output_dir / "add00dat.complete_ko.mogami_fixed.bin",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=output_dir / "add00dat.japanese_base.korean.bin",
    )
    parser.add_argument("--report", type=Path, default=output_dir / "mogami_fix_report.json")
    parser.add_argument("--preview-dir", type=Path, default=output_dir / "preview")
    args = parser.parse_args()

    complete_patch_dir = workspace / "work/srw_gc_complete_patch"
    jp_builder_path = workspace / "work/jp_add00_rebuild_agent/build_jp_base_add00.py"
    sys.path.insert(0, str(complete_patch_dir))
    add00_tools = load_module("mogami_add00_tools", complete_patch_dir / "add00_tools.py")
    add00_build_korean = load_module(
        "mogami_add00_build_korean", complete_patch_dir / "add00_build_korean.py"
    )
    jp_builder = load_module("mogami_jp_add00_builder", jp_builder_path)

    source_report, _ = patch_graphic_source(
        args.korean_source,
        args.patched_korean_source,
        args.preview_dir,
        add00_tools,
        add00_build_korean,
    )

    builder_report_path = output_dir / "jp_builder_report.json"
    builder_preview_dir = args.preview_dir / "jp_title"
    build_args = argparse.Namespace(
        japanese=args.japanese,
        english=args.english_reference,
        korean=args.patched_korean_source,
        output=args.output,
        report=builder_report_path,
        preview_dir=builder_preview_dir,
    )
    builder_report = jp_builder.build(build_args)
    final_audit = verify_final_against_previous(
        args.previous_jp_build, args.output, add00_tools
    )

    report = {
        "schema": "srw-gc-add00-mogami-location-fix-v1",
        "purpose": "Replace the five literal 최상중공 location cards with canonical 모가미 중공 labels.",
        "targets": {str(index): text for index, text in TARGETS.items()},
        "graphic_source_patch": source_report,
        "jp_base_builder": {
            "script": str(jp_builder_path.resolve()),
            "report": str(builder_report_path.resolve()),
            "status": builder_report["audit"]["status"],
            "title_english_blocks_exact": builder_report["title"]["english_title_blocks_exact"],
            "outside_allowed_changes": builder_report["audit"]["outside_allowed_changes"],
        },
        "final_audit": final_audit,
        "preview_contact_sheet": str(
            (args.preview_dir / "mogami_location_fix_contact_sheet.png").resolve()
        ),
        "status": "pass",
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["final_audit"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
