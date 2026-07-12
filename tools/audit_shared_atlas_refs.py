#!/usr/bin/env python3
"""Audit hidden/shared consumers of add00 UI atlas tile indices."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

from PIL import Image


TOOLS_DIR = Path(__file__).resolve().parent
REPOSITORY_DIR = TOOLS_DIR.parent
sys.path.insert(0, str(TOOLS_DIR))
import add00_tools  # noqa: E402

from repack_preserve_indices import (  # noqa: E402
    TARGET_BITMAPS,
    load_actions,
    rebuild_container,
)


CONTROL_TYPES = {b"SPR\0", b"ECD\0", b"ATR\0", b"PAT\0", b"TIM\0"}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def tiles(image: Image.Image) -> list[bytes]:
    return [
        image.crop((x, y, x + 8, y + 8)).tobytes()
        for y in range(0, image.height, 8)
        for x in range(0, image.width, 8)
    ]


def runs(values: list[int]) -> list[list[int]]:
    output: list[list[int]] = []
    for value in values:
        if not output or value != output[-1][-1] + 1:
            output.append([value])
        else:
            output[-1].append(value)
    return output


def scr_group(container: add00_tools.Add00Container, bitmap: int) -> list[int]:
    output: list[int] = []
    index = bitmap + 1
    while index < len(container.blocks):
        signature = container.blocks[index][:4]
        if signature in (b"BMP\x06", b"BMP\x09"):
            break
        if signature == b"SCR\0":
            output.append(index)
        index += 1
    return output


def find_all(data: bytes, needle: bytes) -> list[int]:
    offsets: list[int] = []
    cursor = 0
    while True:
        cursor = data.find(needle, cursor)
        if cursor < 0:
            return offsets
        offsets.append(cursor)
        cursor += 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="pre-UI Japanese-base add00")
    parser.add_argument("repacked", type=Path, help="final preserved-index add00")
    parser.add_argument("--report", type=Path, required=True, help="new JSON audit report")
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
        "--allow-changed-block",
        type=int,
        action="append",
        default=None,
        help="allow an independently audited non-UI block in addition to default SPR 3508",
    )
    args = parser.parse_args()

    source_path = args.source.expanduser().resolve()
    repacked_path = args.repacked.expanduser().resolve()
    report_path = args.report.expanduser().resolve()
    large_mapping_path = args.large_mapping.expanduser().resolve()
    small_mapping_path = args.small_mapping.expanduser().resolve()
    protected = {
        source_path,
        repacked_path,
        large_mapping_path,
        small_mapping_path,
    }
    if report_path in protected:
        raise ValueError("refusing to overwrite an input with the report")
    if report_path.exists():
        raise FileExistsError("report path must not already exist")
    if source_path == repacked_path:
        raise ValueError("source and repacked inputs must be different files")

    source = add00_tools.parse_container(source_path)
    repacked = add00_tools.parse_container(repacked_path)
    if len(source.blocks) != len(repacked.blocks):
        raise ValueError("source/repacked block counts differ")
    actions = load_actions(large_mapping_path, small_mapping_path)
    group_reports: list[dict[str, object]] = []
    pointer_hits: list[dict[str, object]] = []
    structure_failures: list[dict[str, object]] = []
    expected_target_scrs: set[int] = set()

    for bitmap in TARGET_BITMAPS:
        atlas = add00_tools.decode_i4(source.blocks[bitmap])
        source_tile_data = tiles(atlas)
        capacity = len(source_tile_data)
        direct = bitmap == 518
        mask = 0x3FFF if direct else 0x03FF
        group_scrs = scr_group(source, bitmap)
        expected_mode = 1 if direct else 0
        if any(source.blocks[index][15] != expected_mode for index in group_scrs):
            raise ValueError(f"bitmap {bitmap} source SCR index mode field drift")
        action_map = actions.get(bitmap, {})
        if set(action_map) != set(group_scrs):
            raise ValueError(
                f"bitmap {bitmap} mapping coverage drift: "
                f"mapped={sorted(action_map)} actual={group_scrs}"
            )
        target_scrs = {
            index
            for index, row in action_map.items()
            if str(row.get("action")) == "replace_with_korean"
        }
        expected_target_scrs.update(target_scrs)
        referenced: set[int] = set()
        for index in group_scrs:
            block = source.blocks[index]
            width, height = struct.unpack_from(">II", block, 4)
            entries = struct.unpack_from(f">{(len(block) - 32) // 2}H", block, 32)
            referenced.update(value & mask for value in entries[: width * height])
        nonblank = {index for index, pattern in enumerate(source_tile_data) if any(pattern)}
        unreferenced_nonblank = sorted(nonblank - referenced)
        unreferenced_blank = sorted(set(range(capacity)) - referenced - nonblank)
        run_records = [
            {
                "first": run[0],
                "last": run[-1],
                "count": len(run),
                "sha256": sha256(b"".join(source_tile_data[index] for index in run)),
            }
            for run in runs(unreferenced_nonblank)
        ]

        changed_preserved_scrs = []
        preserved_scrs = [
            index
            for index in group_scrs
            if index not in target_scrs
        ]
        for index in preserved_scrs:
            if source.blocks[index] != repacked.blocks[index]:
                changed_preserved_scrs.append(index)

        original_tile_bytes_changed: int | None = None
        original_raster_prefix_byte_identical = False
        bmp_header_fields_preserved = False
        atlas_structure_failure = None
        target_scr_structure_failures: list[dict[str, object]] = []
        repacked_tile_capacity = None
        try:
            repacked_atlas = add00_tools.decode_i4(repacked.blocks[bitmap])
            if repacked_atlas.width != atlas.width:
                raise ValueError(
                    f"width changed from {atlas.width} to {repacked_atlas.width}"
                )
            if repacked_atlas.height < atlas.height:
                raise ValueError(
                    f"height shrank from {atlas.height} to {repacked_atlas.height}"
                )
            repacked_tile_capacity = (
                repacked_atlas.width // 8 * (repacked_atlas.height // 8)
            )
            index_limit = 0x4000 if direct else 0x400
            if repacked_tile_capacity > index_limit:
                raise ValueError(
                    f"physical capacity {repacked_tile_capacity} exceeds {index_limit}"
                )
            source_raster_end = 32 + atlas.width * atlas.height // 2
            original_raster_prefix_byte_identical = (
                repacked.blocks[bitmap][32:source_raster_end]
                == source.blocks[bitmap][32:source_raster_end]
            )
            bmp_header_fields_preserved = (
                repacked.blocks[bitmap][:12] == source.blocks[bitmap][:12]
                and repacked.blocks[bitmap][16:32] == source.blocks[bitmap][16:32]
            )
            if not bmp_header_fields_preserved:
                raise ValueError("BMP header fields other than height changed")
            if not original_raster_prefix_byte_identical:
                repacked_tiles = tiles(repacked_atlas)[:capacity]
                original_tile_bytes_changed = sum(
                    left != right for left, right in zip(source_tile_data, repacked_tiles)
                )
            else:
                original_tile_bytes_changed = 0
        except (ValueError, struct.error) as error:
            atlas_structure_failure = str(error)
            structure_failures.append(
                {"bitmap_block": bitmap, "failure": atlas_structure_failure}
            )

        for index in sorted(target_scrs):
            try:
                source_scr = source.blocks[index]
                repacked_scr = repacked.blocks[index]
                if source_scr[:4] != b"SCR\0" or repacked_scr[:4] != b"SCR\0":
                    raise ValueError("target block is not SCR")
                if len(source_scr) != len(repacked_scr):
                    raise ValueError("target SCR length changed")
                if source_scr[:32] != repacked_scr[:32]:
                    raise ValueError("target SCR header changed")
                width, height = struct.unpack_from(">II", repacked_scr, 4)
                visible_count = width * height
                stored_count = (len(repacked_scr) - 32) // 2
                if stored_count < visible_count:
                    raise ValueError("target SCR is shorter than its declared canvas")
                if repacked_tile_capacity is None:
                    raise ValueError("target atlas structure is unavailable")
                entries = struct.unpack_from(f">{stored_count}H", repacked_scr, 32)
                invalid_indices = sorted(
                    {
                        value & mask
                        for value in entries[:visible_count]
                        if (value & mask) >= repacked_tile_capacity
                    }
                )
                if invalid_indices:
                    raise ValueError(
                        f"target SCR has out-of-range tile indices {invalid_indices[:8]}"
                    )
            except (ValueError, struct.error) as error:
                failure = {"scr_block": index, "failure": str(error)}
                target_scr_structure_failures.append(failure)
                structure_failures.append({"bitmap_block": bitmap, **failure})

        group_reports.append(
            {
                "bitmap_block": bitmap,
                "bitmap_offset": source.offsets[bitmap],
                "dimensions": list(atlas.size),
                "tile_capacity": capacity,
                "scr_mode_field": sorted({source.blocks[index][15] for index in group_scrs}),
                "index_mode": "14bit_direct" if direct else "10bit_hv",
                "immediate_scrs": group_scrs,
                "immediate_scr_count": len(group_scrs),
                "referenced_tile_count": len(referenced),
                "nonblank_tile_count": len(nonblank),
                "unreferenced_nonblank_count": len(unreferenced_nonblank),
                "unreferenced_nonblank_indices": unreferenced_nonblank,
                "unreferenced_nonblank_runs": run_records,
                "unreferenced_nonblank_sha256": sha256(
                    b"".join(source_tile_data[index] for index in unreferenced_nonblank)
                ),
                "unreferenced_blank_count": len(unreferenced_blank),
                "preserved_scrs": preserved_scrs,
                "preserved_scrs_rewritten_by_old_repacker": changed_preserved_scrs,
                "original_atlas_tiles_changed_by_old_repacker": original_tile_bytes_changed,
                "original_raster_prefix_byte_identical": original_raster_prefix_byte_identical,
                "bmp_header_fields_preserved": bmp_header_fields_preserved,
                "atlas_structure_failure": atlas_structure_failure,
                "repacked_tile_capacity": repacked_tile_capacity,
                "target_scr_structure_failures": target_scr_structure_failures,
            }
        )

        # Exact absolute-offset and 32-bit block-ID probes in non-raster
        # control blocks.  These are conservative probes; absence does not
        # disprove runtime/code-generated indices.
        probes = {
            "offset_be32": struct.pack(">I", source.offsets[bitmap]),
            "offset_le32": struct.pack("<I", source.offsets[bitmap]),
            "block_be32": struct.pack(">I", bitmap),
            "block_le32": struct.pack("<I", bitmap),
        }
        excluded = {bitmap, *group_scrs}
        for block_index, block in enumerate(source.blocks):
            if block_index in excluded or block[:4] not in CONTROL_TYPES:
                continue
            for probe_name, needle in probes.items():
                for offset in find_all(block, needle):
                    pointer_hits.append(
                        {
                            "target_bitmap": bitmap,
                            "probe": probe_name,
                            "consumer_block": block_index,
                            "consumer_type": block[:4].decode("ascii", errors="replace"),
                            "consumer_relative_offset": offset,
                            "aligned_4": offset % 4 == 0,
                            "context_hex": block[max(0, offset - 8) : offset + 12].hex().upper(),
                        }
                    )

    changed = [
        index
        for index, (left, right) in enumerate(zip(source.blocks, repacked.blocks))
        if left != right
    ]
    allowed_independent_blocks = {3508}
    allowed_independent_blocks.update(args.allow_changed_block or [])
    if any(not 0 <= index < len(source.blocks) for index in allowed_independent_blocks):
        raise ValueError("one or more allowed changed block indices are out of range")
    expected_generated_changes = set(TARGET_BITMAPS) | expected_target_scrs
    allowed_changes = expected_generated_changes | allowed_independent_blocks
    unexpected_changed_blocks = sorted(set(changed) - allowed_changes)
    missing_expected_changes = sorted(expected_generated_changes - set(changed))
    total_preserved_scr_changes = sum(
        len(row["preserved_scrs_rewritten_by_old_repacker"])
        for row in group_reports
    )
    total_original_tile_changes = sum(
        row["original_atlas_tiles_changed_by_old_repacker"] or 0
        for row in group_reports
    )
    try:
        rebuilt_repacked, rebuilt_offsets = rebuild_container(list(repacked.blocks))
        offset_table_rebuild_byte_identical = (
            rebuilt_repacked == repacked.source and rebuilt_offsets == repacked.offsets
        )
    except ValueError as error:
        offset_table_rebuild_byte_identical = False
        structure_failures.append({"container": "repacked", "failure": str(error)})
    destructive_reindex = bool(
        total_preserved_scr_changes
        or total_original_tile_changes
        or structure_failures
        or unexpected_changed_blocks
        or missing_expected_changes
        or not offset_table_rebuild_byte_identical
    )
    all_offset_matches = sum(
        row["probe"].startswith("offset_") for row in pointer_hits
    )
    aligned_offset_matches = sum(
        row["probe"].startswith("offset_") and row["aligned_4"]
        for row in pointer_hits
    )
    report = {
        "schema": "srw-gc-add00-shared-atlas-reference-audit-v1",
        "status": "unsafe_repack_confirmed" if destructive_reindex else "shared_indices_preserved",
        "source": str(source_path),
        "source_sha256": sha256(source.source),
        "repacked": str(repacked_path),
        "repacked_sha256": sha256(repacked.source),
        "large_mapping_sha256": sha256(large_mapping_path.read_bytes()),
        "small_mapping_sha256": sha256(small_mapping_path.read_bytes()),
        "changed_block_count": len(changed),
        "changed_blocks": changed,
        "expected_generated_changes": sorted(expected_generated_changes),
        "allowed_independent_blocks": sorted(allowed_independent_blocks),
        "unexpected_changed_blocks": unexpected_changed_blocks,
        "missing_expected_changes": missing_expected_changes,
        "structure_failures": structure_failures,
        "offset_table_rebuild_byte_identical": offset_table_rebuild_byte_identical,
        "groups": group_reports,
        "totals": {
            "target_atlases": len(group_reports),
            "immediate_scrs": sum(row["immediate_scr_count"] for row in group_reports),
            "unreferenced_nonblank_tiles": sum(
                row["unreferenced_nonblank_count"] for row in group_reports
            ),
            "preserved_scrs_rewritten": total_preserved_scr_changes,
            "original_atlas_tiles_changed": total_original_tile_changes,
        },
        "control_block_exact_reference_hits": pointer_hits,
        "control_block_exact_offset_probe_summary": {
            "all_offset_byte_matches": all_offset_matches,
            "aligned_offset_byte_matches": aligned_offset_matches,
            "note": (
                "Unaligned matches inside opaque control payloads can be statistical coincidences; "
                "inspect every reported context rather than treating the count as a pointer table."
            ),
        },
        "interpretation": {
            "static_topology": (
                "Every static SCR in these groups is contiguous after its BMP until the next BMP. "
                "No separate SCR declares a bitmap identifier."
            ),
            "dynamic_consumer_evidence": (
                "Nonblank source tiles absent from every contiguous SCR prove that static SCR coverage "
                "is not a valid free-space test. In the v12 source, block 2714 visibly retains the "
                "unused-at-this-moment digits 2..0 and the Sub label outside SCR 2715, while the retail "
                "atlas likewise retains final/part variants. This is a direct example of runtime-selected "
                "tile indices."
            ),
            "old_repacker_failure": (
                "The old builders compacted atlases and rewrote even preserve SCRs, destroying stable "
                "tile-number identity required by dynamic or code-generated consumers."
            ),
        },
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": report["status"], **report["totals"]}, indent=2))
    return 0 if report["status"] == "shared_indices_preserved" else 1


if __name__ == "__main__":
    raise SystemExit(main())
