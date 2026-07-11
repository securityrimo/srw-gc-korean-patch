#!/usr/bin/env python3
"""SRW GC add02/DOL extraction, alignment, and length-safe repacking tools.

The public API intentionally stays small:

    extract_records(Path) -> list[dict]
    repack(source: Path, replacements: dict[str, str], encoder) -> bytes

Replacement keys may be stable record IDs or exact Japanese source strings.  The
module also contains helpers for the executable string pool and the English
patch's pilotinfo-backed relocation scheme.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import struct
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping


TOP_COUNT = 128
TOP_SIZE = TOP_COUNT * 4
TOP_ALIGNMENT = 0x20

# Top-level add02 blocks containing user-visible text.
SIMPLE_TEXT_BLOCKS = {0, 1, 5, 8, 13, 21, 22, 23, 25, 26, 28, 29, 33}
INNER_TABLE_BLOCK = 38
LIBRARY_BLOCK_FIELDS = {40: 3, 41: 4}

CATEGORY_BY_BLOCK = {
    0: "p_name",
    1: "p_name",
    5: "r_name",
    8: "skill",
    13: "terrain",
    21: "spirit",
    22: "spirit",
    23: "spirit",
    25: "epart",
    26: "epart",
    28: "p_part",
    29: "p_part",
    33: "other",
    38: "other",
    40: "lib",
    41: "lib",
}

CSV_BY_CATEGORY = {
    "p_name": "1p_name_1.csv",
    "r_name": "1r_name_2.csv",
    "spirit": "1spirit_3.csv",
    "skill": "1skill_4.csv",
    "epart": "1epart_5.csv",
    "terrain": "1terrain_6.csv",
    "p_part": "1p_part_7.csv",
    "other": "1other_8.csv",
    "lib": "1lib_9.csv",
}

# The English patch loads pilotinfo.bin at this fixed address and redirects
# executable string pointers into the appended pool.
ENGLISH_PILOTINFO_LOAD_BASE = 0x809D2BE0
ENGLISH_POOL_LAST_USED = 0x2A480
SAFE_KOREAN_POOL_START = 0x2A500

Encoder = Callable[[str], bytes]

CP932_TRANSLATION_SUBSTITUTIONS = {
    "\u00B7": "\u30FB",  # middle dot -> Japanese middle dot
    "\u2014": "\u2015",  # em dash -> horizontal bar available in CP932
    "\u11BC": "\u3147",  # isolated Hangul jongseong IEUNG -> compatibility jamo
    "\u11AB": "\u3134",  # isolated Hangul jongseong NIEUN
    "\u11B7": "\u3141",  # isolated Hangul jongseong MIEUM
}


def _be32(data: bytes, offset: int) -> int:
    return struct.unpack_from(">I", data, offset)[0]


def _align(value: int, alignment: int) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def _decode(raw: bytes) -> tuple[str, bool]:
    core = raw.rstrip(b"\0")
    try:
        return core.decode("cp932"), True
    except UnicodeDecodeError:
        return core.decode("cp932", errors="replace"), False


def _is_japanese_char(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3040 <= codepoint <= 0x30FF
        or 0x3400 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
    )


def has_japanese(text: str) -> bool:
    return any(_is_japanese_char(character) for character in text)


def normalize_text(text: str) -> str:
    """Conservative normalization used to align the supplied CSV files."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.strip("\0")
    if text.startswith('"') and not text.endswith('"'):
        text = text[1:]
    return text


def compact_text(text: str) -> str:
    text = normalize_text(text)
    return "".join(character for character in text if not character.isspace())


def _printable_text(text: str) -> bool:
    return bool(text) and all(
        character.isprintable() or character in "\r\n\t \u3000"
        for character in text
    )


def _coherent_japanese(text: str) -> bool:
    if len(text) < 2 or not _printable_text(text):
        return False
    kana = sum(0x3040 <= ord(character) <= 0x30FF for character in text)
    cjk = sum(
        0x3400 <= ord(character) <= 0x9FFF
        or 0xF900 <= ord(character) <= 0xFAFF
        for character in text
    )
    fullwidth = sum(0xFF01 <= ord(character) <= 0xFF60 for character in text)
    return kana >= 1 or cjk >= 2 or (cjk >= 1 and fullwidth >= 1)


def _top_blocks(data: bytes) -> tuple[list[int], list[tuple[int, int, int]]]:
    if len(data) < TOP_SIZE:
        raise ValueError("add02 is smaller than its 128-entry top table")
    values = list(struct.unpack_from(">128I", data, 0))
    nonzero = [(index, value) for index, value in enumerate(values) if value]
    if not nonzero:
        raise ValueError("add02 has no top-level blocks")
    if any(value < TOP_SIZE or value >= len(data) for _, value in nonzero):
        raise ValueError("add02 contains an out-of-range top-level pointer")
    if any(left[1] >= right[1] for left, right in zip(nonzero, nonzero[1:])):
        raise ValueError("add02 top-level pointers are not strictly increasing")
    blocks: list[tuple[int, int, int]] = []
    for ordinal, (table_index, start) in enumerate(nonzero):
        end = nonzero[ordinal + 1][1] if ordinal + 1 < len(nonzero) else len(data)
        blocks.append((table_index, start, end))
    return values, blocks


def _u32_pointer_table(segment: bytes) -> list[int]:
    if len(segment) < 4:
        raise ValueError("truncated pointer-table block")
    first = _be32(segment, 0)
    if first < 4 or first % 4 or first > len(segment):
        raise ValueError(f"invalid first pointer 0x{first:X}")
    count = first // 4
    pointers = list(struct.unpack_from(f">{count}I", segment, 0))
    if any(pointer < first or pointer >= len(segment) for pointer in pointers):
        raise ValueError("inner pointer outside its block")
    if any(left > right for left, right in zip(pointers, pointers[1:])):
        raise ValueError("inner pointers are not monotonic")
    return pointers


def _stable_id(block: int, record: int, field: int = 0) -> str:
    return f"add02:b{block:03d}:r{record:04d}:f{field}"


def _record_dict(
    *,
    block: int,
    record: int,
    field: int,
    file_offset: int,
    block_offset: int,
    raw: bytes,
    structure: str,
) -> dict:
    text, valid = _decode(raw)
    core = raw.rstrip(b"\0")
    return {
        "id": _stable_id(block, record, field),
        "source": "add02dat.bin",
        "container": "add02",
        "category": CATEGORY_BY_BLOCK.get(block, "unknown"),
        "structure": structure,
        "block_index": block,
        "record_index": record,
        "field_index": field,
        "file_offset": file_offset,
        "file_offset_hex": f"0x{file_offset:08X}",
        "block_offset": block_offset,
        "block_offset_hex": f"0x{block_offset:06X}",
        "slot_size": len(raw),
        "payload_size": len(core),
        "raw_hex": core.hex().upper(),
        "japanese": text,
        "normalized_japanese": normalize_text(text),
        "has_japanese": has_japanese(text),
        "cp932_valid": valid,
    }


def extract_records(source: Path) -> list[dict]:
    """Extract every structured text field from add02dat.bin."""
    data = Path(source).read_bytes()
    _, blocks = _top_blocks(data)
    records: list[dict] = []

    for block, start, end in blocks:
        if block not in SIMPLE_TEXT_BLOCKS | {INNER_TABLE_BLOCK} | set(LIBRARY_BLOCK_FIELDS):
            continue
        segment = data[start:end]
        pointers = _u32_pointer_table(segment)

        if block in SIMPLE_TEXT_BLOCKS:
            for record, relative in enumerate(pointers):
                relative_end = pointers[record + 1] if record + 1 < len(pointers) else len(segment)
                records.append(
                    _record_dict(
                        block=block,
                        record=record,
                        field=0,
                        file_offset=start + relative,
                        block_offset=relative,
                        raw=segment[relative:relative_end],
                        structure="u32_string_table",
                    )
                )
            continue

        if block == INNER_TABLE_BLOCK:
            for record, relative in enumerate(pointers):
                relative_end = pointers[record + 1] if record + 1 < len(pointers) else len(segment)
                raw_record = segment[relative:relative_end]
                if len(raw_record) < 2:
                    raise ValueError(f"block {block} record {record} is truncated")
                first = struct.unpack_from(">H", raw_record, 0)[0]
                if first < 2 or first % 2 or first > len(raw_record):
                    raise ValueError(f"block {block} record {record} has bad field table")
                field_count = first // 2
                offsets = list(struct.unpack_from(f">{field_count}H", raw_record, 0))
                if any(offset < first or offset >= len(raw_record) for offset in offsets):
                    raise ValueError(f"block {block} record {record} has bad field offset")
                for field, field_offset in enumerate(offsets):
                    field_end = offsets[field + 1] if field + 1 < len(offsets) else len(raw_record)
                    records.append(
                        _record_dict(
                            block=block,
                            record=record,
                            field=field,
                            file_offset=start + relative + field_offset,
                            block_offset=relative + field_offset,
                            raw=raw_record[field_offset:field_end],
                            structure="u32_records_with_u16_string_table",
                        )
                    )
            continue

        field_count = LIBRARY_BLOCK_FIELDS[block]
        header_size = (field_count + 1) * 2
        for record, relative in enumerate(pointers):
            relative_end = pointers[record + 1] if record + 1 < len(pointers) else len(segment)
            raw_record = segment[relative:relative_end]
            if len(raw_record) < header_size:
                raise ValueError(f"library block {block} record {record} is truncated")
            header = struct.unpack_from(f">{field_count + 1}H", raw_record, 0)
            offsets = list(header[1:])
            if any(offset < header_size or offset >= len(raw_record) for offset in offsets):
                raise ValueError(f"library block {block} record {record} has bad field offset")
            for field, field_offset in enumerate(offsets):
                field_end = offsets[field + 1] if field + 1 < len(offsets) else len(raw_record)
                records.append(
                    _record_dict(
                        block=block,
                        record=record,
                        field=field,
                        file_offset=start + relative + field_offset,
                        block_offset=relative + field_offset,
                        raw=raw_record[field_offset:field_end],
                        structure=f"library_record_{field_count}_fields",
                    )
                )
    return records


def _terminated_even(core: bytes) -> bytes:
    output = bytearray(core)
    output.append(0)
    if len(output) % 2:
        output.append(0)
    return bytes(output)


def _resolve_replacement(record: dict, replacements: Mapping[str, str]) -> str | None:
    if record["id"] in replacements:
        return replacements[record["id"]]
    if record["japanese"] in replacements:
        return replacements[record["japanese"]]
    normalized = record["normalized_japanese"]
    if normalized in replacements:
        return replacements[normalized]
    return None


def _pack_u32_table(payloads: Iterable[bytes]) -> bytes:
    payload_list = list(payloads)
    cursor = len(payload_list) * 4
    pointers: list[int] = []
    body = bytearray()
    for payload in payload_list:
        pointers.append(cursor + len(body))
        body.extend(payload)
    return struct.pack(f">{len(pointers)}I", *pointers) + body


def _pad_block_to_original(rebuilt: bytes, original_size: int) -> bytes:
    if len(rebuilt) < original_size:
        return rebuilt + b"\0" * (original_size - len(rebuilt))
    return rebuilt


def _fit_library_records_to_original_slots(
    minimal_records: list[bytes], old_lengths: list[int]
) -> list[bytes]:
    """Use existing record reserve so the library block keeps its old size.

    Record boundaries remain unchanged until an individual replacement exceeds
    its slot.  Overflow is paid back from zero reserve in following records,
    then (only if necessary) from earlier reserve.  If the total semantic data
    does not fit, the block is allowed to grow.
    """
    target = sum(old_lengths)
    minimum = sum(map(len, minimal_records))
    if minimum > target:
        return minimal_records

    allocated: list[int] = []
    debt = 0
    for minimal, old_length in zip(minimal_records, old_lengths):
        minimum_length = len(minimal)
        if minimum_length >= old_length:
            allocated.append(minimum_length)
            debt += minimum_length - old_length
            continue
        available = old_length - minimum_length
        consumed = min(debt, available)
        allocated.append(old_length - consumed)
        debt -= consumed

    if debt:
        for index in range(len(allocated) - 1, -1, -1):
            reducible = allocated[index] - len(minimal_records[index])
            consumed = min(debt, reducible)
            allocated[index] -= consumed
            debt -= consumed
            if not debt:
                break
    if debt:
        return minimal_records

    # Any reserve not assigned above (possible when all records shrink) goes at
    # the end of the final record, matching the block's zero-padding semantics.
    remainder = target - sum(allocated)
    allocated[-1] += remainder
    return [
        record + b"\0" * (length - len(record))
        for record, length in zip(minimal_records, allocated)
    ]


def repack(
    source: Path,
    replacements: Mapping[str, str],
    encoder: Encoder | None = None,
) -> bytes:
    """Rebuild add02 with recalculated inner and top-level offsets.

    Unchanged blocks stay byte-identical.  Changed text fields may grow without
    an original-slot restriction; every affected pointer table is regenerated.
    """
    encoder = encoder or (lambda text: text.encode("cp932"))
    data = Path(source).read_bytes()
    top_values, blocks = _top_blocks(data)
    extracted = extract_records(Path(source))
    by_id = {record["id"]: record for record in extracted}
    ids_by_block: dict[int, list[str]] = defaultdict(list)
    for record in extracted:
        ids_by_block[record["block_index"]].append(record["id"])

    rebuilt_blocks: dict[int, bytes] = {}
    for block, start, end in blocks:
        segment = data[start:end]
        if block not in ids_by_block:
            rebuilt_blocks[block] = segment
            continue
        block_records = [by_id[record_id] for record_id in ids_by_block[block]]
        changed = any(_resolve_replacement(record, replacements) is not None for record in block_records)
        if not changed:
            rebuilt_blocks[block] = segment
            continue

        pointers = _u32_pointer_table(segment)
        if block in SIMPLE_TEXT_BLOCKS:
            payloads: list[bytes] = []
            old_lengths: list[int] = []
            for record_index, relative in enumerate(pointers):
                relative_end = pointers[record_index + 1] if record_index + 1 < len(pointers) else len(segment)
                raw = segment[relative:relative_end]
                old_lengths.append(len(raw))
                record = by_id[_stable_id(block, record_index, 0)]
                replacement = _resolve_replacement(record, replacements)
                if replacement is None:
                    payloads.append(raw)
                else:
                    encoded = encoder(replacement)
                    payloads.append(raw if encoded == raw.rstrip(b"\0") else _terminated_even(encoded))
            fitted_payloads = _fit_library_records_to_original_slots(payloads, old_lengths)
            rebuilt_blocks[block] = _pad_block_to_original(
                _pack_u32_table(fitted_payloads), len(segment)
            )
            continue

        if block == INNER_TABLE_BLOCK:
            packed_records: list[bytes] = []
            old_record_lengths: list[int] = []
            for record_index, relative in enumerate(pointers):
                relative_end = pointers[record_index + 1] if record_index + 1 < len(pointers) else len(segment)
                raw_record = segment[relative:relative_end]
                old_record_lengths.append(len(raw_record))
                first = struct.unpack_from(">H", raw_record, 0)[0]
                field_count = first // 2
                offsets = list(struct.unpack_from(f">{field_count}H", raw_record, 0))
                header_size = field_count * 2
                fields: list[bytes] = []
                new_offsets: list[int] = []
                cursor = header_size
                for field, field_offset in enumerate(offsets):
                    field_end = offsets[field + 1] if field + 1 < len(offsets) else len(raw_record)
                    raw = raw_record[field_offset:field_end]
                    item = by_id[_stable_id(block, record_index, field)]
                    replacement = _resolve_replacement(item, replacements)
                    if replacement is None:
                        packed = raw
                    else:
                        encoded = encoder(replacement)
                        packed = raw if encoded == raw.rstrip(b"\0") else _terminated_even(encoded)
                    new_offsets.append(cursor)
                    fields.append(packed)
                    cursor += len(packed)
                if cursor > 0xFFFF:
                    raise ValueError(f"block {block} record {record_index} exceeds u16 offsets")
                packed_records.append(struct.pack(f">{field_count}H", *new_offsets) + b"".join(fields))
            fitted_records = _fit_library_records_to_original_slots(
                packed_records, old_record_lengths
            )
            rebuilt_blocks[block] = _pad_block_to_original(
                _pack_u32_table(fitted_records), len(segment)
            )
            continue

        field_count = LIBRARY_BLOCK_FIELDS[block]
        minimal_records: list[bytes] = []
        old_record_lengths: list[int] = []
        for record_index, relative in enumerate(pointers):
            relative_end = pointers[record_index + 1] if record_index + 1 < len(pointers) else len(segment)
            raw_record = segment[relative:relative_end]
            header = list(struct.unpack_from(f">{field_count + 1}H", raw_record, 0))
            old_offsets = header[1:]
            header_size = (field_count + 1) * 2
            fields = []
            new_offsets = []
            final_texts: list[str] = []
            cursor = header_size
            for field, field_offset in enumerate(old_offsets):
                field_end = old_offsets[field + 1] if field + 1 < field_count else len(raw_record)
                raw = raw_record[field_offset:field_end]
                item = by_id[_stable_id(block, record_index, field)]
                replacement = _resolve_replacement(item, replacements)
                final_texts.append(item["japanese"] if replacement is None else replacement)
                if replacement is None:
                    # Library records have substantial per-record zero reserve.
                    # Rebuild the semantic string minimally, then restore reserve
                    # at the record level below.  This lets a preceding field grow
                    # without spuriously growing the whole record.
                    packed = _terminated_even(raw.rstrip(b"\0"))
                else:
                    encoded = encoder(replacement)
                    packed = raw if encoded == raw.rstrip(b"\0") else _terminated_even(encoded)
                new_offsets.append(cursor)
                fields.append(packed)
                cursor += len(packed)
            if cursor > 0xFFFF:
                raise ValueError(f"library block {block} record {record_index} exceeds u16 offsets")
            # The first u16 is not an ID: it is the number of U+2192 arrow
            # markers in the final biography/description field.  The game uses
            # it while paging library prose.  Keeping the Japanese value after
            # a translation can make the reader walk beyond the new string.
            line_break_count = final_texts[-1].count("\u2192")
            packed_record = (
                struct.pack(f">{field_count + 1}H", line_break_count, *new_offsets)
                + b"".join(fields)
            )
            minimal_records.append(packed_record)
            old_record_lengths.append(len(raw_record))
        fitted_records = _fit_library_records_to_original_slots(
            minimal_records, old_record_lengths
        )
        rebuilt_blocks[block] = _pad_block_to_original(
            _pack_u32_table(fitted_records), len(segment)
        )

    output = bytearray(TOP_SIZE)
    new_top = list(top_values)
    for block, _, _ in blocks:
        while len(output) % TOP_ALIGNMENT:
            output.append(0)
        new_top[block] = len(output)
        output.extend(rebuilt_blocks[block])
    while len(output) % TOP_ALIGNMENT:
        output.append(0)
    struct.pack_into(">128I", output, 0, *new_top)
    return bytes(output)


def verify_identity_repack(source: Path) -> dict:
    records = extract_records(source)
    identity: dict[str, str] = {}
    skipped_invalid = 0
    for record in records:
        if not record["cp932_valid"]:
            skipped_invalid += 1
            continue
        identity[record["id"]] = record["japanese"]
    original = Path(source).read_bytes()
    rebuilt = repack(source, identity)
    return {
        "source": str(Path(source).resolve()),
        "source_size": len(original),
        "rebuilt_size": len(rebuilt),
        "source_sha256": _sha256(original),
        "rebuilt_sha256": _sha256(rebuilt),
        "byte_identical": rebuilt == original,
        "structured_fields": len(records),
        "invalid_cp932_fields_preserved": skipped_invalid,
    }


def validate_add02_structure(source: Path, reference: Path | None = None) -> dict:
    """Validate all known add02 tables and library record invariants.

    When ``reference`` is supplied, every non-text top-level block must remain
    byte-identical and every structured text block must retain its record count.
    """
    path = Path(source)
    data = path.read_bytes()
    errors: list[dict] = []
    top_values, blocks = _top_blocks(data)
    block_map = {index: (start, end) for index, start, end in blocks}
    records = extract_records(path)

    if any(start % TOP_ALIGNMENT for _, start, _ in blocks):
        errors.append({"kind": "top_alignment", "message": "one or more top blocks are not 0x20 aligned"})
    if len(blocks) != 123:
        errors.append({"kind": "top_count", "actual": len(blocks), "expected": 123})

    pointer_counts: dict[int, int] = {}
    for block in sorted(SIMPLE_TEXT_BLOCKS | {INNER_TABLE_BLOCK} | set(LIBRARY_BLOCK_FIELDS)):
        start, end = block_map[block]
        segment = data[start:end]
        pointers = _u32_pointer_table(segment)
        pointer_counts[block] = len(pointers)

    arrow = "\u2192"
    library_records_checked = 0
    for block, field_count in LIBRARY_BLOCK_FIELDS.items():
        start, end = block_map[block]
        segment = data[start:end]
        pointers = _u32_pointer_table(segment)
        for record_index, record_offset in enumerate(pointers):
            record_end = pointers[record_index + 1] if record_index + 1 < len(pointers) else len(segment)
            raw_record = segment[record_offset:record_end]
            header = struct.unpack_from(f">{field_count + 1}H", raw_record, 0)
            field_offsets = header[1:]
            if any(left >= right for left, right in zip(field_offsets, field_offsets[1:])):
                errors.append(
                    {"kind": "library_field_order", "block": block, "record": record_index}
                )
                continue
            last_offset = field_offsets[-1]
            final_text, _ = _decode(raw_record[last_offset:])
            expected_count = final_text.count(arrow)
            if header[0] != expected_count:
                errors.append(
                    {
                        "kind": "library_arrow_count",
                        "block": block,
                        "record": record_index,
                        "stored": header[0],
                        "expected": expected_count,
                    }
                )
            library_records_checked += 1

    nontext_blocks_checked = 0
    if reference is not None:
        reference_data = Path(reference).read_bytes()
        _, reference_blocks = _top_blocks(reference_data)
        reference_map = {index: (start, end) for index, start, end in reference_blocks}
        if set(reference_map) != set(block_map):
            errors.append({"kind": "reference_block_set"})
        for block in sorted(set(block_map) & set(reference_map)):
            start, end = block_map[block]
            ref_start, ref_end = reference_map[block]
            if block in SIMPLE_TEXT_BLOCKS | {INNER_TABLE_BLOCK} | set(LIBRARY_BLOCK_FIELDS):
                try:
                    candidate_count = len(_u32_pointer_table(data[start:end]))
                    reference_count = len(_u32_pointer_table(reference_data[ref_start:ref_end]))
                except ValueError as error:
                    errors.append({"kind": "reference_pointer_table", "block": block, "error": str(error)})
                    continue
                if candidate_count != reference_count:
                    errors.append(
                        {
                            "kind": "reference_record_count",
                            "block": block,
                            "candidate": candidate_count,
                            "reference": reference_count,
                        }
                    )
            else:
                nontext_blocks_checked += 1
                if data[start:end] != reference_data[ref_start:ref_end]:
                    errors.append({"kind": "nontext_block_changed", "block": block})

    return {
        "source": str(path.resolve()),
        "source_size": len(data),
        "source_sha256": _sha256(data),
        "top_nonzero_blocks": len(blocks),
        "structured_fields": len(records),
        "pointer_counts": {str(key): value for key, value in pointer_counts.items()},
        "library_records_checked": library_records_checked,
        "nontext_blocks_checked": nontext_blocks_checked,
        "reference": str(Path(reference).resolve()) if reference is not None else None,
        "valid": not errors,
        "errors": errors,
    }


def load_codebook(path: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            result[row["target"]] = int(row["code"], 16)
    return result


def codebook_encoder(codebook: Mapping[str, int]) -> Encoder:
    def encode(text: str) -> bytes:
        output = bytearray()
        for character in text:
            character = CP932_TRANSLATION_SUBSTITUTIONS.get(character, character)
            if character in codebook:
                output.extend(codebook[character].to_bytes(2, "big"))
            else:
                output.extend(character.encode("cp932"))
        return bytes(output)

    return encode


def load_csv_rows(root: Path) -> dict[str, list[dict]]:
    tables: dict[str, list[dict]] = {}
    for category, filename in CSV_BY_CATEGORY.items():
        path = Path(root) / filename
        rows: list[dict] = []
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            next(reader, None)
            for index, row in enumerate(reader):
                if len(row) < 4:
                    continue
                rows.append(
                    {
                        "id": f"csv:{filename}:{index:04d}",
                        "file": filename,
                        "row_index": index,
                        "legacy_offsets": row[0],
                        "legacy_length": row[1],
                        "japanese": row[2],
                        "normalized_japanese": normalize_text(row[2]),
                        "korean": row[3],
                        "legacy_mapping": row[4] if len(row) > 4 else "",
                    }
                )
        tables[category] = rows
    return tables


def _ordered_csv_candidate(record: dict, rows: list[dict]) -> dict | None:
    block = record["block_index"]
    index = record["record_index"]
    target: int | None = None
    if block == 26:
        target = index
    elif block == 25:
        target = 44 + index
    elif block == 29:
        target = index
    elif block == 28:
        target = 37 + index
    elif block == 21:
        target = index
    elif block == 23:
        target = 89 + index
    if target is not None and 0 <= target < len(rows):
        return rows[target]
    return None


def align_add02_csv(
    source: Path,
    csv_root: Path,
    encoder: Encoder | None = None,
) -> list[dict]:
    records = extract_records(source)
    tables = load_csv_rows(csv_root)
    try:
        from rapidfuzz import fuzz, process
    except ImportError:  # pragma: no cover - fallback for a bare Python install
        fuzz = None
        process = None

    indexes: dict[str, dict] = {}
    for category, rows in tables.items():
        exact: dict[str, list[dict]] = defaultdict(list)
        compact_rows: list[str] = []
        for row in rows:
            exact[row["normalized_japanese"]].append(row)
            compact_rows.append(compact_text(row["japanese"]))
        indexes[category] = {"rows": rows, "exact": exact, "compact": compact_rows}

    fuzzy_cache: dict[tuple[str, str], tuple[dict | None, str, float]] = {}
    aligned_records: list[dict] = []
    for record in records:
        category_index = indexes.get(record["category"], {"rows": [], "exact": {}, "compact": []})
        rows = category_index["rows"]
        exact = category_index["exact"]
        source_text = record["normalized_japanese"]
        selected: dict | None = None
        status = "unmatched"
        confidence = 0.0

        if source_text in exact:
            candidates = exact[source_text]
            selected = candidates[0]
            status = "exact"
            confidence = 1.0
        else:
            ordered = _ordered_csv_candidate(record, rows)
            if ordered is not None:
                selected = ordered
                status = "ordered_layout"
                if compact_text(source_text) == compact_text(ordered["japanese"]):
                    confidence = 0.99
                elif len(compact_text(source_text)) >= 8:
                    confidence = 0.82
                else:
                    confidence = 0.70

            if selected is None and process is not None and source_text:
                query = compact_text(source_text)
                cache_key = (record["category"], query)
                if cache_key in fuzzy_cache:
                    selected, status, confidence = fuzzy_cache[cache_key]
                else:
                    all_choices = category_index["compact"]
                    candidate_indexes = [
                        index
                        for index, choice in enumerate(all_choices)
                        if choice
                        and (
                            len(query) <= 24
                            or abs(len(choice) - len(query)) <= max(16, int(len(query) * 0.45))
                        )
                    ]
                    # Long library prose is highly distinctive at its beginning.
                    if len(query) > 80:
                        same_prefix = [
                            index
                            for index in candidate_indexes
                            if all_choices[index][:2] == query[:2]
                        ]
                        if same_prefix:
                            candidate_indexes = same_prefix
                    choices = [all_choices[index] for index in candidate_indexes]
                    results = process.extract(
                        query,
                        choices,
                        scorer=fuzz.ratio,
                        limit=2,
                        score_cutoff=80,
                    )
                    if results:
                        _, best_score, local_index = results[0]
                        runner_up = results[1][1] if len(results) > 1 else 0.0
                        if best_score >= 97 or (best_score >= 91 and best_score - runner_up >= 3):
                            selected = rows[candidate_indexes[local_index]]
                            status = "fuzzy"
                            confidence = round(best_score / 100.0, 4)
                    fuzzy_cache[cache_key] = (selected, status, confidence)

        aligned = dict(record)
        aligned["match_status"] = status
        aligned["match_confidence"] = confidence
        aligned["existing_korean"] = selected["korean"] if selected else ""
        aligned["csv_id"] = selected["id"] if selected else None
        aligned["csv_japanese"] = selected["japanese"] if selected else None
        aligned["csv_legacy_offsets"] = selected["legacy_offsets"] if selected else None
        if selected and encoder is not None:
            try:
                encoded = encoder(selected["korean"])
                aligned["korean_encoded_size"] = len(encoded)
                aligned["fits_original_slot"] = len(encoded) + 1 <= record["slot_size"]
            except UnicodeEncodeError as error:
                aligned["encoding_error"] = str(error)
                aligned["fits_original_slot"] = False
        aligned_records.append(aligned)
    return aligned_records


@dataclass(frozen=True)
class DolSection:
    kind: str
    index: int
    file_offset: int
    address: int
    size: int


def dol_sections(data: bytes) -> list[DolSection]:
    if len(data) < 0x100:
        raise ValueError("truncated DOL header")
    text_offsets = struct.unpack_from(">7I", data, 0x00)
    data_offsets = struct.unpack_from(">11I", data, 0x1C)
    text_addresses = struct.unpack_from(">7I", data, 0x48)
    data_addresses = struct.unpack_from(">11I", data, 0x64)
    text_sizes = struct.unpack_from(">7I", data, 0x90)
    data_sizes = struct.unpack_from(">11I", data, 0xAC)
    sections: list[DolSection] = []
    for kind, offsets, addresses, sizes in (
        ("text", text_offsets, text_addresses, text_sizes),
        ("data", data_offsets, data_addresses, data_sizes),
    ):
        for index, (offset, address, size) in enumerate(zip(offsets, addresses, sizes)):
            if not size:
                continue
            if offset + size > len(data):
                raise ValueError(f"DOL {kind}{index} extends beyond the file")
            sections.append(DolSection(kind, index, offset, address, size))
    return sections


def dol_address_to_offset(data: bytes, address: int) -> int | None:
    for section in dol_sections(data):
        if section.address <= address < section.address + section.size:
            return section.file_offset + address - section.address
    return None


def dol_offset_to_address(data: bytes, offset: int) -> int | None:
    for section in dol_sections(data):
        if section.file_offset <= offset < section.file_offset + section.size:
            return section.address + offset - section.file_offset
    return None


def _cstring(data: bytes, offset: int, limit: int = 8192) -> tuple[str, bytes] | None:
    if not 0 <= offset < len(data):
        return None
    end = data.find(b"\0", offset, min(len(data), offset + limit))
    if end < 0 or end == offset:
        return None
    raw = data[offset:end]
    try:
        text = raw.decode("cp932")
    except UnicodeDecodeError:
        return None
    if not _printable_text(text):
        return None
    return text, raw


def scan_dol_records(source: Path) -> list[dict]:
    """Scan DOL data sections for high-confidence null-terminated Japanese text."""
    data = Path(source).read_bytes()
    results: dict[int, dict] = {}
    for section in dol_sections(data):
        if section.kind != "data":
            continue
        start = section.file_offset
        end = section.file_offset + section.size
        cursor = start
        while cursor < end:
            zero = data.find(b"\0", cursor, end)
            if zero < 0:
                break
            raw = data[cursor:zero]
            if 1 <= len(raw) <= 8192:
                try:
                    text = raw.decode("cp932")
                except UnicodeDecodeError:
                    pass
                else:
                    if _coherent_japanese(text):
                        padded_end = zero + 1
                        while padded_end < min(end, _align(zero + 1, 4)) and data[padded_end] == 0:
                            padded_end += 1
                        address = dol_offset_to_address(data, cursor)
                        results[cursor] = {
                            "id": f"dol:off{cursor:08X}",
                            "source": "Start.dol",
                            "container": "dol",
                            "section": f"{section.kind}{section.index}",
                            "file_offset": cursor,
                            "file_offset_hex": f"0x{cursor:08X}",
                            "runtime_address": address,
                            "runtime_address_hex": f"0x{address:08X}" if address is not None else None,
                            "slot_size": padded_end - cursor,
                            "payload_size": len(raw),
                            "japanese": text,
                            "normalized_japanese": normalize_text(text),
                            "has_japanese": has_japanese(text),
                        }
            cursor = zero + 1
    return list(results.values())


def dol_pointer_records(
    original_dol: Path,
    english_dol: Path | None = None,
    english_pilotinfo: Path | None = None,
) -> list[dict]:
    """Find aligned DOL pointers to Japanese strings and optional English targets."""
    original = Path(original_dol).read_bytes()
    english = Path(english_dol).read_bytes() if english_dol else None
    pilot = Path(english_pilotinfo).read_bytes() if english_pilotinfo else None
    grouped: dict[int, dict] = {}
    sections = dol_sections(original)

    for pointer_offset in range(0, len(original) - 3, 4):
        original_pointer = _be32(original, pointer_offset)
        target_offset = dol_address_to_offset(original, original_pointer)
        if target_offset is None:
            continue
        target_section = next(
            (
                section
                for section in sections
                if section.file_offset <= target_offset < section.file_offset + section.size
            ),
            None,
        )
        # Executable instructions frequently contain 32-bit values that happen
        # to equal text-section addresses.  User-visible strings live in DOL
        # data sections, so excluding text targets removes those false pointers.
        if target_section is None or target_section.kind != "data":
            continue
        decoded = _cstring(original, target_offset)
        if decoded is None:
            continue
        text, raw = decoded
        if not has_japanese(text):
            continue

        item = grouped.setdefault(
            target_offset,
            {
                "id": f"dol:off{target_offset:08X}",
                "source": "Start.dol",
                "container": "dol_pointer_table",
                "target_file_offset": target_offset,
                "target_file_offset_hex": f"0x{target_offset:08X}",
                "target_runtime_address": original_pointer,
                "target_runtime_address_hex": f"0x{original_pointer:08X}",
                "payload_size": len(raw),
                "japanese": text,
                "normalized_japanese": normalize_text(text),
                "pointer_locations": [],
                "english_targets": [],
            },
        )
        item["pointer_locations"].append(pointer_offset)

        if english is not None and pointer_offset + 4 <= len(english):
            english_pointer = _be32(english, pointer_offset)
            english_target: dict = {
                "pointer_location": pointer_offset,
                "pointer_location_hex": f"0x{pointer_offset:08X}",
                "runtime_address": english_pointer,
                "runtime_address_hex": f"0x{english_pointer:08X}",
            }
            if pilot is not None and ENGLISH_PILOTINFO_LOAD_BASE <= english_pointer < ENGLISH_PILOTINFO_LOAD_BASE + len(pilot):
                pilot_offset = english_pointer - ENGLISH_PILOTINFO_LOAD_BASE
                english_target["pilotinfo_offset"] = pilot_offset
                english_target["pilotinfo_offset_hex"] = f"0x{pilot_offset:08X}"
                english_decoded = _cstring(pilot, pilot_offset)
                if english_decoded is not None:
                    english_target["text"] = english_decoded[0]
                    english_target["payload_size"] = len(english_decoded[1])
                    english_target["relocated"] = True
            else:
                english_offset = dol_address_to_offset(english, english_pointer)
                if english_offset is not None:
                    english_decoded = _cstring(english, english_offset)
                    if english_decoded is not None:
                        english_target["dol_offset"] = english_offset
                        english_target["text"] = english_decoded[0]
                        english_target["payload_size"] = len(english_decoded[1])
                        english_target["relocated"] = False
            item["english_targets"].append(english_target)

    for item in grouped.values():
        item["pointer_locations_hex"] = [f"0x{offset:08X}" for offset in item["pointer_locations"]]
        item["pointer_count"] = len(item["pointer_locations"])
        relocated = [target for target in item["english_targets"] if target.get("relocated")]
        item["english_relocated_pointer_count"] = len(relocated)
        if relocated:
            item["english_text"] = relocated[0].get("text")
            item["english_pilotinfo_offset"] = relocated[0].get("pilotinfo_offset")
    return sorted(grouped.values(), key=lambda item: item["target_file_offset"])


def align_dol_csv(
    original_dol: Path,
    english_dol: Path,
    english_pilotinfo: Path,
    csv_root: Path,
    encoder: Encoder | None = None,
) -> list[dict]:
    """Align executable pointer targets to all nine supplied translation CSVs."""
    records = dol_pointer_records(original_dol, english_dol, english_pilotinfo)
    tables = load_csv_rows(csv_root)
    csv_rows = [row for rows in tables.values() for row in rows]
    exact: dict[str, list[dict]] = defaultdict(list)
    compact_rows: list[str] = []
    for row in csv_rows:
        exact[row["normalized_japanese"]].append(row)
        compact_rows.append(compact_text(row["japanese"]))

    pilot = Path(english_pilotinfo).read_bytes()
    pilot_offsets = sorted(
        {
            target["pilotinfo_offset"]
            for record in records
            for target in record.get("english_targets", [])
            if target.get("relocated") and target.get("text") is not None
        }
    )
    slot_by_offset: dict[int, int] = {}
    for index, offset in enumerate(pilot_offsets):
        decoded = _cstring(pilot, offset)
        if decoded is None:
            continue
        minimum_end = offset + len(decoded[1]) + 1
        next_offset = pilot_offsets[index + 1] if index + 1 < len(pilot_offsets) else _align(minimum_end, 2)
        if next_offset >= minimum_end and not any(pilot[minimum_end:next_offset]):
            slot_by_offset[offset] = next_offset - offset
        else:
            slot_by_offset[offset] = _align(len(decoded[1]) + 1, 2)

    try:
        from rapidfuzz import fuzz, process
    except ImportError:  # pragma: no cover
        fuzz = None
        process = None

    fuzzy_cache: dict[str, tuple[dict | None, float]] = {}
    aligned_records: list[dict] = []
    for record in records:
        source = record["normalized_japanese"]
        selected: dict | None = None
        status = "unmatched"
        confidence = 0.0
        conflicts: list[str] = []
        if source in exact:
            candidates = exact[source]
            selected = candidates[0]
            status = "exact"
            confidence = 1.0
            conflicts = sorted({candidate["korean"] for candidate in candidates})
        elif process is not None and len(compact_text(source)) >= 3:
            query = compact_text(source)
            if query in fuzzy_cache:
                selected, confidence = fuzzy_cache[query]
            else:
                candidate_indexes = [
                    index
                    for index, choice in enumerate(compact_rows)
                    if choice
                    and (
                        len(query) <= 24
                        or abs(len(choice) - len(query)) <= max(16, int(len(query) * 0.40))
                    )
                ]
                if len(query) > 80:
                    same_prefix = [
                        index for index in candidate_indexes if compact_rows[index][:2] == query[:2]
                    ]
                    if same_prefix:
                        candidate_indexes = same_prefix
                choices = [compact_rows[index] for index in candidate_indexes]
                results = process.extract(
                    query,
                    choices,
                    scorer=fuzz.ratio,
                    limit=2,
                    score_cutoff=88,
                )
                if results:
                    _, score, local_index = results[0]
                    runner_up = results[1][1] if len(results) > 1 else 0.0
                    if score >= 98 or (score >= 94 and score - runner_up >= 3):
                        selected = csv_rows[candidate_indexes[local_index]]
                        confidence = round(score / 100.0, 4)
                fuzzy_cache[query] = (selected, confidence)
            if selected is not None:
                status = "fuzzy"

        aligned = dict(record)
        aligned["match_status"] = status
        aligned["match_confidence"] = confidence
        aligned["existing_korean"] = selected["korean"] if selected else ""
        aligned["csv_id"] = selected["id"] if selected else None
        aligned["csv_japanese"] = selected["japanese"] if selected else None
        aligned["csv_translation_conflicts"] = conflicts if len(conflicts) > 1 else []
        relocated_targets = [
            target
            for target in record.get("english_targets", [])
            if target.get("relocated") and target.get("pilotinfo_offset") in slot_by_offset
        ]
        if relocated_targets:
            offset = relocated_targets[0]["pilotinfo_offset"]
            aligned["english_pilotinfo_slot_offset"] = offset
            aligned["english_pilotinfo_slot_offset_hex"] = f"0x{offset:08X}"
            aligned["english_pilotinfo_slot_size"] = slot_by_offset[offset]
        if selected and encoder is not None:
            try:
                encoded = encoder(selected["korean"])
                aligned["korean_encoded_size"] = len(encoded)
                aligned["fits_english_relocated_slot"] = bool(relocated_targets) and (
                    len(encoded) + 1 <= slot_by_offset[relocated_targets[0]["pilotinfo_offset"]]
                )
                aligned["relocated_pool_size"] = len(_terminated_even(encoded))
            except UnicodeEncodeError as error:
                aligned["encoding_error"] = str(error)
                aligned["fits_english_relocated_slot"] = False
        aligned_records.append(aligned)
    return aligned_records


def build_relocated_dol_patch(
    original_dol: Path,
    english_dol: Path,
    english_pilotinfo: Path,
    replacements: Mapping[str, str],
    encoder: Encoder,
    pool_start: int = SAFE_KOREAN_POOL_START,
) -> tuple[bytes, bytes, dict]:
    """Redirect every aligned Japanese DOL pointer to a new pilotinfo text pool.

    This deliberately starts from the English-patched DOL because it contains the
    loader changes that make pilotinfo-backed absolute pointers valid.  Existing
    English strings remain intact; the Korean pool is placed in the large zero
    reserve following the English pool.
    """
    pointer_records = dol_pointer_records(original_dol, english_dol, english_pilotinfo)
    dol = bytearray(Path(english_dol).read_bytes())
    pilot = bytearray(Path(english_pilotinfo).read_bytes())
    cursor = _align(pool_start, 2)
    if cursor < ENGLISH_POOL_LAST_USED:
        raise ValueError("Korean pool overlaps the English patch's active string pool")
    report_records: list[dict] = []

    skipped_non_text_targets = 0
    for record in pointer_records:
        replacement = (
            replacements.get(record["id"])
            or replacements.get(record["japanese"])
            or replacements.get(record["normalized_japanese"])
        )
        # Only explicitly supplied targets are display strings selected by the
        # master audit.  The broader DOL pointer scan also sees character/font
        # lookup tables; relocating those as prose would corrupt the game.
        if replacement is None:
            skipped_non_text_targets += 1
            continue
        final_text = replacement
        encoded = encoder(final_text)
        packed = _terminated_even(encoded)
        if cursor + len(packed) > len(pilot):
            raise ValueError(
                f"pilotinfo reserve exhausted at {record['id']}; need 0x{cursor + len(packed):X}, "
                f"have 0x{len(pilot):X}"
            )
        pilot[cursor : cursor + len(packed)] = packed
        runtime_address = ENGLISH_PILOTINFO_LOAD_BASE + cursor
        # Start from the proven English executable and rewrite only locations
        # that the English patch itself redirected into pilotinfo.  Raw scans
        # also see the same 32-bit value in the DOL header (section addresses)
        # and in unrelated tables; touching those corrupts the loader.
        patch_locations = [
            target["pointer_location"]
            for target in record["english_targets"]
            if target.get("relocated")
        ]
        if not patch_locations:
            skipped_non_text_targets += 1
            continue
        for pointer_location in patch_locations:
            struct.pack_into(">I", dol, pointer_location, runtime_address)
        report_records.append(
            {
                "id": record["id"],
                "japanese": record["japanese"],
                "final_text": final_text,
                "translation_supplied": replacement is not None,
                "pilotinfo_offset": cursor,
                "pilotinfo_offset_hex": f"0x{cursor:08X}",
                "runtime_address": runtime_address,
                "runtime_address_hex": f"0x{runtime_address:08X}",
                "encoded_size": len(encoded),
                "allocated_size": len(packed),
                "pointer_count": len(patch_locations),
                "raw_pointer_candidate_count": record["pointer_count"],
            }
        )
        cursor += len(packed)

    report = {
        "original_dol": str(Path(original_dol).resolve()),
        "english_dol": str(Path(english_dol).resolve()),
        "english_pilotinfo": str(Path(english_pilotinfo).resolve()),
        "pilotinfo_load_base_hex": f"0x{ENGLISH_PILOTINFO_LOAD_BASE:08X}",
        "pool_start_hex": f"0x{pool_start:08X}",
        "pool_end_hex": f"0x{cursor:08X}",
        "pool_bytes_used": cursor - pool_start,
        "pool_bytes_remaining": len(pilot) - cursor,
        "unique_strings_relocated": len(report_records),
        "pointers_rewritten": sum(record["pointer_count"] for record in report_records),
        "translations_supplied": sum(record["translation_supplied"] for record in report_records),
        "non_text_or_unselected_targets_skipped": skipped_non_text_targets,
        "dol_sha256": _sha256(bytes(dol)),
        "pilotinfo_sha256": _sha256(bytes(pilot)),
        "records": report_records,
    }
    return bytes(dol), bytes(pilot), report


def build_data_pointer_dol_patch(
    original_dol: Path,
    english_dol: Path,
    english_pilotinfo: Path,
    replacements: Mapping[str, str],
    encoder: Encoder,
    pool_start: int = SAFE_KOREAN_POOL_START,
) -> tuple[bytes, bytes, dict]:
    """Relocate selected DOL strings by rewriting only data-section pointers.

    ``dol_pointer_records`` intentionally scans the whole executable.  A raw
    pointer value can also occur in the DOL header or in PowerPC instructions,
    so treating every match as a writable reference corrupts the executable.
    The safe invariant used here is stricter: the pointer *location* itself
    must be inside one of the original DOL's data sections.  This covers both
    the 978 strings relocated by the English patch and additional untranslated
    UI strings whose pointers still target the original DOL string pool.

    Only records explicitly present in ``replacements`` are relocated.  This
    lets callers keep name-entry and collation character tables byte-identical
    while still including those functional records in the canonical audit.
    """

    pointer_records = dol_pointer_records(original_dol, english_dol, english_pilotinfo)
    original = Path(original_dol).read_bytes()
    dol = bytearray(Path(english_dol).read_bytes())
    pilot = bytearray(Path(english_pilotinfo).read_bytes())
    if len(dol) != len(original):
        raise ValueError("original and English DOL layouts differ")

    data_sections = [section for section in dol_sections(original) if section.kind == "data"]

    def pointer_section(offset: int) -> DolSection | None:
        return next(
            (
                section
                for section in data_sections
                if section.file_offset <= offset < section.file_offset + section.size
            ),
            None,
        )

    cursor = _align(pool_start, 2)
    if cursor < ENGLISH_POOL_LAST_USED:
        raise ValueError("Korean pool overlaps the English patch's active string pool")

    report_records: list[dict] = []
    skipped_ids: list[str] = []
    for record in pointer_records:
        replacement = (
            replacements.get(record["id"])
            or replacements.get(record["japanese"])
            or replacements.get(record["normalized_japanese"])
        )
        if replacement is None:
            skipped_ids.append(record["id"])
            continue

        patch_locations = [
            offset for offset in record["pointer_locations"] if pointer_section(offset) is not None
        ]
        if not patch_locations:
            raise ValueError(f"selected record has no data-section pointer: {record['id']}")

        encoded = encoder(replacement)
        packed = _terminated_even(encoded)
        if cursor + len(packed) > len(pilot):
            raise ValueError(
                f"pilotinfo reserve exhausted at {record['id']}; need 0x{cursor + len(packed):X}, "
                f"have 0x{len(pilot):X}"
            )
        pilot[cursor : cursor + len(packed)] = packed
        runtime_address = ENGLISH_PILOTINFO_LOAD_BASE + cursor

        pointer_details: list[dict] = []
        for pointer_location in patch_locations:
            section = pointer_section(pointer_location)
            assert section is not None
            original_value = _be32(original, pointer_location)
            if original_value != record["target_runtime_address"]:
                raise ValueError(
                    f"pointer record drift at {record['id']} location 0x{pointer_location:X}"
                )
            english_value = _be32(dol, pointer_location)
            struct.pack_into(">I", dol, pointer_location, runtime_address)
            pointer_details.append(
                {
                    "file_offset": pointer_location,
                    "file_offset_hex": f"0x{pointer_location:08X}",
                    "section": f"{section.kind}{section.index}",
                    "english_value_before": english_value,
                    "english_value_before_hex": f"0x{english_value:08X}",
                }
            )

        report_records.append(
            {
                "id": record["id"],
                "japanese": record["japanese"],
                "final_text": replacement,
                "pilotinfo_offset": cursor,
                "pilotinfo_offset_hex": f"0x{cursor:08X}",
                "runtime_address": runtime_address,
                "runtime_address_hex": f"0x{runtime_address:08X}",
                "encoded_size": len(encoded),
                "allocated_size": len(packed),
                "data_pointer_count": len(patch_locations),
                "raw_pointer_candidate_count": record["pointer_count"],
                "pointer_details": pointer_details,
            }
        )
        cursor += len(packed)

    report = {
        "builder": "build_data_pointer_dol_patch",
        "original_dol": str(Path(original_dol).resolve()),
        "english_dol": str(Path(english_dol).resolve()),
        "english_pilotinfo": str(Path(english_pilotinfo).resolve()),
        "pilotinfo_load_base_hex": f"0x{ENGLISH_PILOTINFO_LOAD_BASE:08X}",
        "pool_start_hex": f"0x{pool_start:08X}",
        "pool_end_hex": f"0x{cursor:08X}",
        "pool_bytes_used": cursor - pool_start,
        "pool_bytes_remaining": len(pilot) - cursor,
        "unique_strings_relocated": len(report_records),
        "pointers_rewritten": sum(record["data_pointer_count"] for record in report_records),
        "records_intentionally_unselected": len(skipped_ids),
        "unselected_ids": skipped_ids,
        "pointer_location_policy": "original DOL data sections only",
        "dol_sha256": _sha256(bytes(dol)),
        "pilotinfo_sha256": _sha256(bytes(pilot)),
        "records": report_records,
    }
    return bytes(dol), bytes(pilot), report


def build_japanese_data_pointer_dol_patch(
    original_dol: Path,
    replacements: Mapping[str, str],
    encoder: Encoder,
) -> tuple[bytes, dict]:
    """Relocate selected UI strings inside the Japanese retail DOL.

    The retail executable already contains 31 KiB of selected Japanese display
    strings.  Their slots, plus only zero padding between adjacent slots, form
    safe reusable clusters.  Korean strings are packed into those clusters and
    original data-section pointers are rewritten to the new retail-DOL
    addresses.  No English executable, pilotinfo loader, or hard-coded English
    font pointer is involved.
    """

    original_path = Path(original_dol)
    original = original_path.read_bytes()
    dol = bytearray(original)
    pointer_records = dol_pointer_records(original_path)
    by_id = {str(record["id"]): record for record in pointer_records}
    missing = sorted(set(replacements) - set(by_id))
    if missing:
        raise ValueError(f"Japanese DOL records missing for replacements: {missing[:8]}")

    data_sections = [section for section in dol_sections(original) if section.kind == "data"]

    def containing_data_section(start: int, end: int) -> DolSection | None:
        return next(
            (
                section
                for section in data_sections
                if section.file_offset <= start and end <= section.file_offset + section.size
            ),
            None,
        )

    selected: list[dict] = []
    for stable_id, final_text in replacements.items():
        record = by_id[stable_id]
        start = int(record["target_file_offset"])
        end = start + int(record["payload_size"]) + 1
        section = containing_data_section(start, end)
        if section is None:
            raise ValueError(f"selected Japanese string is outside a data section: {stable_id}")
        if original[end - 1] != 0:
            raise ValueError(f"Japanese string terminator drift: {stable_id}")
        encoded = encoder(final_text)
        packed = _terminated_even(encoded)
        patch_locations = [
            offset
            for offset in record["pointer_locations"]
            if containing_data_section(int(offset), int(offset) + 4) is not None
        ]
        if not patch_locations:
            raise ValueError(f"selected Japanese string has no data pointer: {stable_id}")
        selected.append(
            {
                "id": stable_id,
                "record": record,
                "source_start": start,
                "source_end": end,
                "section": section,
                "final_text": final_text,
                "encoded": encoded,
                "packed": packed,
                "patch_locations": patch_locations,
            }
        )

    # Merge only adjacent selected slots separated exclusively by zero bytes,
    # and never cross a DOL section boundary.
    clusters: list[dict] = []
    for item in sorted(selected, key=lambda value: int(value["source_start"])):
        start = int(item["source_start"])
        end = int(item["source_end"])
        section = item["section"]
        if (
            clusters
            and clusters[-1]["section"] == section
            and start >= int(clusters[-1]["end"])
            and not any(original[int(clusters[-1]["end"]) : start])
        ):
            clusters[-1]["end"] = end
            clusters[-1]["source_ids"].append(item["id"])
        else:
            clusters.append(
                {
                    "start": start,
                    "end": end,
                    "cursor": _align(start, 2),
                    "section": section,
                    "source_ids": [item["id"]],
                }
            )

    # A pointer table must never be erased as part of a reclaimed text cluster.
    all_pointer_locations = {
        int(offset)
        for item in selected
        for offset in item["patch_locations"]
    }
    for cluster in clusters:
        start = int(cluster["start"])
        end = int(cluster["end"])
        overlap = sorted(offset for offset in all_pointer_locations if start <= offset < end)
        if overlap:
            raise ValueError(
                f"Japanese string cluster overlaps pointer table at 0x{overlap[0]:X}"
            )
        dol[start:end] = bytes(end - start)

    # Best-fit decreasing allocation keeps fragmentation bounded across the
    # small safe clusters while remaining deterministic.
    placements: dict[str, dict] = {}
    for item in sorted(selected, key=lambda value: (-len(value["packed"]), value["id"])):
        size = len(item["packed"])
        candidates = []
        for index, cluster in enumerate(clusters):
            cursor = int(cluster["cursor"])
            usable_end = int(cluster["end"]) & ~1
            remaining = usable_end - cursor
            if size <= remaining:
                candidates.append((remaining - size, index))
        if not candidates:
            raise ValueError(f"Japanese DOL string clusters exhausted at {item['id']}")
        _, selected_cluster_index = min(candidates)
        cluster = clusters[selected_cluster_index]
        offset = int(cluster["cursor"])
        end = offset + size
        if containing_data_section(offset, end) is None:
            raise ValueError(f"Japanese DOL placement crosses a section: {item['id']}")
        dol[offset:end] = item["packed"]
        cluster["cursor"] = end
        runtime_address = dol_offset_to_address(original, offset)
        if runtime_address is None:
            raise ValueError(f"Japanese DOL placement has no runtime address: {item['id']}")
        placements[item["id"]] = {
            "offset": offset,
            "runtime_address": runtime_address,
            "allocated_size": size,
            "cluster_index": selected_cluster_index,
        }

    report_records: list[dict] = []
    for item in selected:
        placement = placements[item["id"]]
        runtime_address = int(placement["runtime_address"])
        pointer_details = []
        for pointer_location in item["patch_locations"]:
            pointer_location = int(pointer_location)
            original_value = _be32(original, pointer_location)
            if original_value != int(item["record"]["target_runtime_address"]):
                raise ValueError(
                    f"Japanese pointer drift at {item['id']} location 0x{pointer_location:X}"
                )
            struct.pack_into(">I", dol, pointer_location, runtime_address)
            pointer_details.append(
                {
                    "file_offset": pointer_location,
                    "file_offset_hex": f"0x{pointer_location:08X}",
                    "original_value_hex": f"0x{original_value:08X}",
                }
            )
        report_records.append(
            {
                "id": item["id"],
                "japanese": item["record"]["japanese"],
                "final_text": item["final_text"],
                "source_offset_hex": f"0x{int(item['source_start']):08X}",
                "new_offset": placement["offset"],
                "new_offset_hex": f"0x{int(placement['offset']):08X}",
                "runtime_address_hex": f"0x{runtime_address:08X}",
                "encoded_size": len(item["encoded"]),
                "allocated_size": placement["allocated_size"],
                "data_pointer_count": len(item["patch_locations"]),
                "pointer_details": pointer_details,
            }
        )

    # Byte-for-byte post-build verification of every payload and pointer.
    for item in selected:
        placement = placements[item["id"]]
        offset = int(placement["offset"])
        packed = item["packed"]
        if dol[offset : offset + len(packed)] != packed:
            raise ValueError(f"Japanese DOL payload verification failed: {item['id']}")
        for pointer_location in item["patch_locations"]:
            if _be32(dol, int(pointer_location)) != int(placement["runtime_address"]):
                raise ValueError(f"Japanese DOL pointer verification failed: {item['id']}")

    used_bytes = sum(len(item["packed"]) for item in selected)
    capacity = sum((int(cluster["end"]) & ~1) - _align(int(cluster["start"]), 2) for cluster in clusters)
    report = {
        "builder": "build_japanese_data_pointer_dol_patch",
        "base": "Japanese retail Start.dol",
        "original_dol": str(original_path.resolve()),
        "pool_policy": "selected retail string slots plus zero-only adjacent padding",
        "cluster_count": len(clusters),
        "pool_capacity": capacity,
        "pool_bytes_used": used_bytes,
        "pool_bytes_remaining": capacity - used_bytes,
        "unique_strings_relocated": len(report_records),
        "pointers_rewritten": sum(record["data_pointer_count"] for record in report_records),
        "dol_sha256": _sha256(bytes(dol)),
        "clusters": [
            {
                "start_hex": f"0x{int(cluster['start']):08X}",
                "end_hex": f"0x{int(cluster['end']):08X}",
                "capacity": (int(cluster["end"]) & ~1) - _align(int(cluster["start"]), 2),
                "used": int(cluster["cursor"]) - _align(int(cluster["start"]), 2),
                "source_record_count": len(cluster["source_ids"]),
            }
            for cluster in clusters
        ],
        "records": report_records,
    }
    return bytes(dol), report


def extract_banner_records(data: bytes, path: str = "opening.bnr") -> list[dict]:
    if len(data) < 0x1960 or data[:4] not in {b"BNR1", b"BNR2"}:
        return []
    fields = [
        ("short_title", 0x1820, 0x20),
        ("short_maker", 0x1840, 0x20),
        ("long_title", 0x1860, 0x40),
        ("long_maker", 0x18A0, 0x40),
        ("description", 0x18E0, 0x80),
    ]
    records: list[dict] = []
    for name, offset, size in fields:
        raw = data[offset : offset + size].split(b"\0", 1)[0]
        try:
            text = raw.decode("cp932")
        except UnicodeDecodeError:
            continue
        if not has_japanese(text):
            continue
        records.append(
            {
                "id": f"fst:{path}:{name}",
                "source": path,
                "container": "gamecube_banner",
                "field": name,
                "file_offset": offset,
                "file_offset_hex": f"0x{offset:08X}",
                "slot_size": size,
                "payload_size": len(raw),
                "japanese": text,
                "normalized_japanese": normalize_text(text),
                "has_japanese": True,
                "patchability": "fixed_field",
            }
        )
    return records


def scan_iso_fst(iso: Path) -> dict:
    """High-confidence whole-FST audit; compressed/audio false positives are excluded."""
    tools_dir = Path(__file__).resolve().parents[1] / "srw_gc_patch_tools"
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))
    from gc_iso_fst import read_fst

    entries, fst_offset, fst_size = read_fst(Path(iso))
    known_direct = {"add01dat.bin", "add02dat.bin", "bpilot.pak", "opening.bnr"}
    records: list[dict] = []
    notes: list[dict] = []
    with Path(iso).open("rb") as handle:
        for entry in entries:
            if entry.is_dir:
                continue
            if entry.path == "opening.bnr":
                handle.seek(entry.offset)
                for record in extract_banner_records(handle.read(entry.size), entry.path):
                    record["iso_offset"] = entry.offset + record["file_offset"]
                    record["fst_index"] = entry.index
                    records.append(record)
            elif entry.path in known_direct:
                notes.append(
                    {
                        "path": entry.path,
                        "fst_index": entry.index,
                        "iso_offset": entry.offset,
                        "size": entry.size,
                        "status": "handled_by_structured_extractor",
                    }
                )
            elif entry.path in {"voice.pak", "wave.pak", "srwse.samp"}:
                notes.append(
                    {
                        "path": entry.path,
                        "fst_index": entry.index,
                        "iso_offset": entry.offset,
                        "size": entry.size,
                        "status": "audio_payload_excluded",
                    }
                )
    return {
        "iso": str(Path(iso).resolve()),
        "fst_offset": fst_offset,
        "fst_size": fst_size,
        "entry_count": len(entries) + 1,
        "high_confidence_records": records,
        "notes": notes,
        "finding": (
            "Outside add01/add02/bpilot and Start.dol, the only direct high-confidence "
            "Japanese metadata is opening.bnr. Scene/map/add00 PAK payloads are binary or "
            "compressed; raw Shift-JIS-like runs are not reliable text records."
        ),
    }


def _json_dump(path: Path, payload: object) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_replacements(path: Path) -> dict[str, str]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        if isinstance(payload.get("records"), list):
            payload = payload["records"]
        else:
            return {str(key): str(value) for key, value in payload.items() if value is not None}
    replacements: dict[str, str] = {}
    if isinstance(payload, list):
        for record in payload:
            if not isinstance(record, dict) or "id" not in record:
                continue
            value = record.get("final_korean") or record.get("existing_korean") or record.get("korean")
            if value:
                replacements[str(record["id"])] = str(value)
    return replacements


def _summary(records: list[dict]) -> dict:
    return {
        "record_count": len(records),
        "japanese_record_count": sum(record.get("has_japanese", False) for record in records),
        "category_counts": dict(Counter(record.get("category", "unknown") for record in records)),
        "match_status_counts": dict(Counter(record.get("match_status", "n/a") for record in records)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract_parser = subparsers.add_parser("extract-add02")
    extract_parser.add_argument("source", type=Path)
    extract_parser.add_argument("output", type=Path)

    align_parser = subparsers.add_parser("align-add02")
    align_parser.add_argument("source", type=Path)
    align_parser.add_argument("csv_root", type=Path)
    align_parser.add_argument("output", type=Path)
    align_parser.add_argument("--codebook", type=Path)

    verify_parser = subparsers.add_parser("verify-add02")
    verify_parser.add_argument("source", type=Path)
    verify_parser.add_argument("--output", type=Path)

    validate_parser = subparsers.add_parser("validate-add02")
    validate_parser.add_argument("source", type=Path)
    validate_parser.add_argument("--reference", type=Path)
    validate_parser.add_argument("--output", type=Path)

    repack_parser = subparsers.add_parser("repack-add02")
    repack_parser.add_argument("source", type=Path)
    repack_parser.add_argument("replacements", type=Path)
    repack_parser.add_argument("output", type=Path)
    repack_parser.add_argument("--codebook", type=Path)

    dol_parser = subparsers.add_parser("scan-dol")
    dol_parser.add_argument("source", type=Path)
    dol_parser.add_argument("output", type=Path)

    correspondence_parser = subparsers.add_parser("dol-correspondence")
    correspondence_parser.add_argument("original_dol", type=Path)
    correspondence_parser.add_argument("english_dol", type=Path)
    correspondence_parser.add_argument("english_pilotinfo", type=Path)
    correspondence_parser.add_argument("output", type=Path)

    dol_align_parser = subparsers.add_parser("align-dol")
    dol_align_parser.add_argument("original_dol", type=Path)
    dol_align_parser.add_argument("english_dol", type=Path)
    dol_align_parser.add_argument("english_pilotinfo", type=Path)
    dol_align_parser.add_argument("csv_root", type=Path)
    dol_align_parser.add_argument("output", type=Path)
    dol_align_parser.add_argument("--codebook", type=Path)

    relocate_parser = subparsers.add_parser("build-relocated-dol")
    relocate_parser.add_argument("original_dol", type=Path)
    relocate_parser.add_argument("english_dol", type=Path)
    relocate_parser.add_argument("english_pilotinfo", type=Path)
    relocate_parser.add_argument("replacements", type=Path)
    relocate_parser.add_argument("output_dol", type=Path)
    relocate_parser.add_argument("output_pilotinfo", type=Path)
    relocate_parser.add_argument("report", type=Path)
    relocate_parser.add_argument("--codebook", type=Path, required=True)
    relocate_parser.add_argument("--pool-start", type=lambda value: int(value, 0), default=SAFE_KOREAN_POOL_START)

    fst_parser = subparsers.add_parser("scan-iso-fst")
    fst_parser.add_argument("iso", type=Path)
    fst_parser.add_argument("output", type=Path)

    args = parser.parse_args()

    if args.command == "extract-add02":
        records = extract_records(args.source)
        _json_dump(args.output, {"summary": _summary(records), "records": records})
    elif args.command == "align-add02":
        encoder = codebook_encoder(load_codebook(args.codebook)) if args.codebook else None
        records = align_add02_csv(args.source, args.csv_root, encoder)
        _json_dump(args.output, {"summary": _summary(records), "records": records})
    elif args.command == "verify-add02":
        report = verify_identity_repack(args.source)
        if args.output:
            _json_dump(args.output, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["byte_identical"] else 1
    elif args.command == "validate-add02":
        report = validate_add02_structure(args.source, args.reference)
        if args.output:
            _json_dump(args.output, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["valid"] else 1
    elif args.command == "repack-add02":
        encoder = codebook_encoder(load_codebook(args.codebook)) if args.codebook else None
        output = repack(args.source, _load_replacements(args.replacements), encoder)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(output)
    elif args.command == "scan-dol":
        records = scan_dol_records(args.source)
        _json_dump(args.output, {"summary": _summary(records), "records": records})
    elif args.command == "dol-correspondence":
        records = dol_pointer_records(args.original_dol, args.english_dol, args.english_pilotinfo)
        summary = {
            "unique_original_targets": len(records),
            "pointer_locations": sum(record["pointer_count"] for record in records),
            "english_relocated_targets": sum(bool(record.get("english_relocated_pointer_count")) for record in records),
            "english_relocated_pointers": sum(record.get("english_relocated_pointer_count", 0) for record in records),
            "pilotinfo_load_base_hex": f"0x{ENGLISH_PILOTINFO_LOAD_BASE:08X}",
        }
        _json_dump(args.output, {"summary": summary, "records": records})
    elif args.command == "align-dol":
        encoder = codebook_encoder(load_codebook(args.codebook)) if args.codebook else None
        records = align_dol_csv(
            args.original_dol,
            args.english_dol,
            args.english_pilotinfo,
            args.csv_root,
            encoder,
        )
        summary = {
            "record_count": len(records),
            "pointer_locations": sum(record["pointer_count"] for record in records),
            "match_status_counts": dict(Counter(record["match_status"] for record in records)),
            "matched_records": sum(bool(record["existing_korean"]) for record in records),
            "fixed_english_slot_fit_counts": dict(
                Counter(record.get("fits_english_relocated_slot") for record in records if record["existing_korean"])
            ),
            "new_pool_bytes_for_matched_records": sum(record.get("relocated_pool_size", 0) for record in records),
        }
        _json_dump(args.output, {"summary": summary, "records": records})
    elif args.command == "build-relocated-dol":
        encoder = codebook_encoder(load_codebook(args.codebook))
        dol, pilot, report = build_relocated_dol_patch(
            args.original_dol,
            args.english_dol,
            args.english_pilotinfo,
            _load_replacements(args.replacements),
            encoder,
            args.pool_start,
        )
        args.output_dol.parent.mkdir(parents=True, exist_ok=True)
        args.output_pilotinfo.parent.mkdir(parents=True, exist_ok=True)
        args.output_dol.write_bytes(dol)
        args.output_pilotinfo.write_bytes(pilot)
        _json_dump(args.report, report)
    elif args.command == "scan-iso-fst":
        _json_dump(args.output, scan_iso_fst(args.iso))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
