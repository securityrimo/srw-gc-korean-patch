#!/usr/bin/env python3
"""Build a Korean add02 while preserving the English reference topology.

This is a deliberately conservative alternative to the general add02 repacker.
It keeps all 128 top-level offsets, the complete file length, and every non-text
block byte-identical to the known-good English reference.  Only the contents of
text blocks and their *internal* pointer tables are rebuilt.

The supplied Korean library prose is slightly larger than English block 40.
``--compact-library-whitespace`` removes only layout-neutral spaces adjacent to
the game's line-break arrow and spaces after Japanese/ASCII full stops or
commas.  It does not drop or replace any non-space character.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys
from collections import Counter
from pathlib import Path
from typing import Mapping

import add02_dol_tools as add02


ARROW = "\u2192"
PUNCTUATION = "\u3002\u3001,."
ID_PATTERN = re.compile(r"^add02:b(?P<block>\d{3}):")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def compact_layout_whitespace(text: str) -> str:
    """Remove layout-neutral spaces without deleting visible text characters."""
    text = re.sub(r" +" + ARROW, ARROW, text)
    text = re.sub(ARROW + r" +", ARROW, text)
    text = re.sub(r"([" + PUNCTUATION + r"]) +", r"\1", text)
    return text


def prepare_replacements(
    replacements: Mapping[str, str],
    *,
    compact_library_whitespace: bool,
) -> tuple[dict[str, str], dict]:
    prepared = dict(replacements)
    changed_ids: list[str] = []
    removed_spaces = 0

    if compact_library_whitespace:
        for record_id, text in list(prepared.items()):
            if not record_id.startswith("add02:b040:"):
                continue
            compacted = compact_layout_whitespace(text)
            if compacted != text:
                changed_ids.append(record_id)
                removed_spaces += len(text) - len(compacted)
                prepared[record_id] = compacted

    return prepared, {
        "enabled": compact_library_whitespace,
        "changed_records": len(changed_ids),
        "removed_space_characters": removed_spaces,
        "changed_record_ids": changed_ids,
        "non_space_characters_changed": 0,
    }


def replacement_blocks(replacements: Mapping[str, str]) -> set[int]:
    result: set[int] = set()
    for record_id in replacements:
        match = ID_PATTERN.match(record_id)
        if match:
            result.add(int(match.group("block")))
    return result


def block_map(data: bytes) -> dict[int, tuple[int, int]]:
    _, blocks = add02._top_blocks(data)
    return {block: (start, end) for block, start, end in blocks}


def build_compact_library_block(
    source_data: bytes,
    source_path: Path,
    block: int,
    replacements: Mapping[str, str],
    encoder,
) -> bytes:
    """Rebuild a library block without retaining obsolete per-record reserve."""
    field_count = add02.LIBRARY_BLOCK_FIELDS[block]
    start, end = block_map(source_data)[block]
    segment = source_data[start:end]
    pointers = add02._u32_pointer_table(segment)
    records = {
        record["id"]: record
        for record in add02.extract_records(source_path)
        if record["block_index"] == block
    }
    packed_records: list[bytes] = []
    for record_index, relative in enumerate(pointers):
        relative_end = pointers[record_index + 1] if record_index + 1 < len(pointers) else len(segment)
        raw_record = segment[relative:relative_end]
        header = list(struct.unpack_from(f">{field_count + 1}H", raw_record, 0))
        old_offsets = header[1:]
        header_size = (field_count + 1) * 2
        new_offsets: list[int] = []
        fields: list[bytes] = []
        final_texts: list[str] = []
        cursor = header_size
        for field, field_offset in enumerate(old_offsets):
            field_end = old_offsets[field + 1] if field + 1 < field_count else len(raw_record)
            raw = raw_record[field_offset:field_end]
            record_id = add02._stable_id(block, record_index, field)
            item = records[record_id]
            replacement = add02._resolve_replacement(item, replacements)
            final_text = item["japanese"] if replacement is None else replacement
            final_texts.append(final_text)
            if replacement is None:
                packed = add02._terminated_even(raw.rstrip(b"\0"))
            else:
                encoded = encoder(replacement)
                packed = add02._terminated_even(encoded)
            new_offsets.append(cursor)
            fields.append(packed)
            cursor += len(packed)
        if cursor > 0xFFFF:
            raise ValueError(f"library block {block} record {record_index} exceeds u16 offsets")
        line_break_count = final_texts[-1].count(ARROW)
        packed_records.append(
            struct.pack(f">{field_count + 1}H", line_break_count, *new_offsets)
            + b"".join(fields)
        )
    result = add02._pack_u32_table(packed_records)
    return result + bytes((-len(result)) % add02.TOP_ALIGNMENT)


def rebuilt_text_blocks(
    source: Path,
    replacements: Mapping[str, str],
    encoder,
) -> dict[int, bytes]:
    """Return changed blocks in compact form, each padded to top alignment."""
    source_data = Path(source).read_bytes()
    variable = add02.repack(Path(source), replacements, encoder)
    variable_blocks = block_map(variable)
    result: dict[int, bytes] = {}
    for block in replacement_blocks(replacements):
        if block in add02.LIBRARY_BLOCK_FIELDS:
            result[block] = build_compact_library_block(
                source_data, Path(source), block, replacements, encoder
            )
        else:
            start, end = variable_blocks[block]
            result[block] = variable[start:end]
    return result


def build_fixed_topology(
    source: Path,
    replacements: Mapping[str, str],
    encoder,
) -> tuple[bytes, list[dict]]:
    original = Path(source).read_bytes()
    original_blocks = block_map(original)
    rebuilt_blocks = rebuilt_text_blocks(Path(source), replacements, encoder)
    changed_blocks = replacement_blocks(replacements)

    output = bytearray(original)
    capacities: list[dict] = []
    for block in sorted(changed_blocks):
        if block not in original_blocks or block not in rebuilt_blocks:
            raise ValueError(f"replacement block {block} is absent from add02")
        original_start, original_end = original_blocks[block]
        capacity = original_end - original_start
        rebuilt = rebuilt_blocks[block]
        required = len(rebuilt)
        capacities.append(
            {
                "block": block,
                "capacity": capacity,
                "required": required,
                "remaining": capacity - required,
                "fits": required <= capacity,
            }
        )
        if required > capacity:
            raise ValueError(
                f"block {block} needs {required} bytes but fixed topology allows "
                f"{capacity} (overflow {required - capacity})"
            )
        output[original_start:original_end] = rebuilt + bytes(capacity - required)

    if output[: add02.TOP_SIZE] != original[: add02.TOP_SIZE]:
        raise AssertionError("top-level offset table changed")
    if len(output) != len(original):
        raise AssertionError("fixed-topology output size changed")
    return bytes(output), capacities


def verify_output(
    source: Path,
    output_path: Path,
    replacements: Mapping[str, str],
    encoder,
    capacities: list[dict],
) -> dict:
    source_data = Path(source).read_bytes()
    output_data = Path(output_path).read_bytes()
    source_blocks = block_map(source_data)
    output_blocks = block_map(output_data)
    changed_blocks = replacement_blocks(replacements)

    records = {record["id"]: record for record in add02.extract_records(output_path)}
    missing_ids: list[str] = []
    payload_mismatches: list[dict] = []
    for record_id, text in replacements.items():
        record = records.get(record_id)
        if record is None:
            missing_ids.append(record_id)
            continue
        actual = bytes.fromhex(record["raw_hex"])
        expected = encoder(text)
        if actual != expected:
            payload_mismatches.append(
                {
                    "id": record_id,
                    "expected_size": len(expected),
                    "actual_size": len(actual),
                    "expected_sha256": sha256(expected),
                    "actual_sha256": sha256(actual),
                }
            )

    non_text_changed: list[int] = []
    for block, (source_start, source_end) in source_blocks.items():
        if block in changed_blocks:
            continue
        output_start, output_end = output_blocks[block]
        if source_data[source_start:source_end] != output_data[output_start:output_end]:
            non_text_changed.append(block)

    structural_validation = add02.validate_add02_structure(output_path, source)

    return {
        "source": str(Path(source).resolve()),
        "output": str(Path(output_path).resolve()),
        "source_size": len(source_data),
        "output_size": len(output_data),
        "source_sha256": sha256(source_data),
        "output_sha256": sha256(output_data),
        "top_table_byte_identical": (
            source_data[: add02.TOP_SIZE] == output_data[: add02.TOP_SIZE]
        ),
        "top_offsets_identical": block_map(source_data) == block_map(output_data),
        "non_text_blocks_byte_identical": not non_text_changed,
        "non_text_blocks_changed": non_text_changed,
        "replacement_count": len(replacements),
        "verified_payload_count": len(replacements) - len(missing_ids) - len(payload_mismatches),
        "missing_record_ids": missing_ids,
        "payload_mismatches": payload_mismatches,
        "block_capacities": capacities,
        "structural_validation": structural_validation,
        "success": (
            len(source_data) == len(output_data)
            and source_data[: add02.TOP_SIZE] == output_data[: add02.TOP_SIZE]
            and block_map(source_data) == block_map(output_data)
            and not non_text_changed
            and not missing_ids
            and not payload_mismatches
            and all(item["fits"] for item in capacities)
            and structural_validation["valid"]
        ),
    }


def strict_capacity_assessment(source: Path, replacements, encoder) -> dict:
    source_data = Path(source).read_bytes()
    source_blocks = block_map(source_data)
    rebuilt_blocks = rebuilt_text_blocks(Path(source), replacements, encoder)
    rows = []
    for block in sorted(replacement_blocks(replacements)):
        source_start, source_end = source_blocks[block]
        capacity = source_end - source_start
        required = len(rebuilt_blocks[block])
        rows.append(
            {
                "block": block,
                "capacity": capacity,
                "required": required,
                "remaining": capacity - required,
                "fits": required <= capacity,
            }
        )
    return {
        "block_capacities": rows,
        "overflow_blocks": [item for item in rows if not item["fits"]],
    }


def individual_slot_assessment(source: Path, replacements, encoder) -> dict:
    records = {record["id"]: record for record in add02.extract_records(source)}
    rows = []
    for record_id, text in replacements.items():
        record = records.get(record_id)
        if record is None:
            continue
        encoded_size = len(encoder(text))
        required = encoded_size + 1
        capacity = record["slot_size"]
        rows.append(
            {
                "id": record_id,
                "block": record["block_index"],
                "capacity": capacity,
                "encoded_size": encoded_size,
                "required_with_terminator": required,
                "overflow": max(0, required - capacity),
                "fits": required <= capacity,
            }
        )
    overflow = [row for row in rows if not row["fits"]]
    by_block = Counter(row["block"] for row in overflow)
    return {
        "assessed_records": len(rows),
        "fits": len(rows) - len(overflow),
        "overflows": len(overflow),
        "maximum_overflow": max((row["overflow"] for row in overflow), default=0),
        "overflow_counts_by_block": dict(sorted(by_block.items())),
        "overflow_records": overflow,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--replacements", type=Path, required=True)
    parser.add_argument("--codebook", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--compact-library-whitespace",
        action="store_true",
        help="compact layout-neutral spaces in block 40 so it fits its fixed slot",
    )
    args = parser.parse_args()

    replacements = json.loads(args.replacements.read_text(encoding="utf-8"))
    encoder = add02.codebook_encoder(add02.load_codebook(args.codebook))
    strict = strict_capacity_assessment(args.source, replacements, encoder)
    individual = individual_slot_assessment(args.source, replacements, encoder)
    prepared, whitespace = prepare_replacements(
        replacements,
        compact_library_whitespace=args.compact_library_whitespace,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        output, capacities = build_fixed_topology(args.source, prepared, encoder)
    except Exception as error:
        report = {
            "success": False,
            "error": str(error),
            "strict_unmodified_assessment": strict,
            "individual_fixed_slot_assessment": individual,
            "whitespace_compaction": whitespace,
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return 1

    args.output.write_bytes(output)
    report = verify_output(
        args.source, args.output, prepared, encoder, capacities
    )
    report["strict_unmodified_assessment"] = strict
    report["individual_fixed_slot_assessment"] = individual
    report["whitespace_compaction"] = whitespace
    report["replacement_counts_by_block"] = dict(
        sorted(
            Counter(
                int(ID_PATTERN.match(record_id).group("block"))
                for record_id in prepared
                if ID_PATTERN.match(record_id)
            ).items()
        )
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["success"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
