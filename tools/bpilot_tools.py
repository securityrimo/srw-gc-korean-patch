#!/usr/bin/env python3
"""Extract and losslessly rebuild Super Robot Wars GC ``bpilot.pak``.

The archive has a big-endian outer directory.  Its ``*.bin`` members are
little-endian ATMB containers made of an offset table followed by variable
length records.  Dialogue in those records is delimited by Japanese corner
or round brackets.  Rebuilding both offset tables removes the fixed-width
restriction used by the early smoke-test patcher.

Public integration API
----------------------

``extract_records(Path) -> list[dict]``
    Return every delimited bpilot text occurrence with a stable ID.

``repack(source, replacements, encoder) -> bytes``
    Replace stable IDs and rebuild changed ATMB members plus the outer PAK.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import struct
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence


PAK_ALIGNMENT = 0x20
TEXT_PAIRS = {
    bytes.fromhex("8175"): bytes.fromhex("8176"),  # Japanese corner quotes
    bytes.fromhex("8169"): bytes.fromhex("816A"),  # full-width parentheses
}
NORMALIZE_FOR_ENCODING = {
    "\u00a0": " ",
    "\u00b7": "\u30fb",
    "\u2014": "\u2015",
    "\u2049": "!?",
    "\u11ab": "\u3134",
    "\u11b7": "\u3141",
    "\u11bc": "\u3147",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def align(value: int, alignment: int = PAK_ALIGNMENT) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


@dataclass(frozen=True)
class PakMember:
    index: int
    name: str
    raw_name: bytes
    offset: int
    size: int
    data: bytes


@dataclass(frozen=True)
class ParsedPak:
    source: bytes
    members: tuple[PakMember, ...]
    table_end: int
    first_data_offset: int


@dataclass(frozen=True)
class ParsedAtmb:
    source: bytes
    offsets: tuple[int, ...]
    records: tuple[bytes, ...]


@dataclass(frozen=True)
class TextSpan:
    text_index: int
    start: int
    end: int
    raw: bytes
    text: str
    decode_ok: bool


def parse_pak(data: bytes) -> ParsedPak:
    if len(data) < 4:
        raise ValueError("truncated bpilot PAK header")
    count = struct.unpack_from(">I", data, 0)[0]
    table_end = 4 + count * 40
    if count <= 0 or table_end > len(data):
        raise ValueError("invalid bpilot PAK member count")

    members: list[PakMember] = []
    previous_end = table_end
    for index in range(count):
        position = 4 + index * 40
        raw_name = data[position : position + 32]
        try:
            name = raw_name.split(b"\0", 1)[0].decode("ascii").strip(" ")
        except UnicodeDecodeError as exc:
            raise ValueError(f"non-ASCII PAK member name at index {index}") from exc
        offset, size = struct.unpack_from(">II", data, position + 32)
        # The retail archive intentionally repeats the empty p000.bin/.hed
        # placeholders.  Dialogue-bearing member names are unique, so retain
        # directory indices and allow those duplicates instead of rejecting a
        # valid archive.
        if not name:
            raise ValueError(f"empty PAK member name at index {index}")
        if offset % PAK_ALIGNMENT:
            raise ValueError(f"unaligned PAK member {name}: 0x{offset:X}")
        if offset < table_end or offset < previous_end or offset + size > len(data):
            raise ValueError(f"invalid PAK member extent: {name}")
        members.append(PakMember(index, name, raw_name, offset, size, data[offset : offset + size]))
        previous_end = offset + size

    first_data_offset = members[0].offset
    if first_data_offset != align(table_end):
        raise ValueError(
            f"unexpected first PAK member offset: 0x{first_data_offset:X} "
            f"(expected 0x{align(table_end):X})"
        )
    return ParsedPak(data, tuple(members), table_end, first_data_offset)


def parse_atmb(data: bytes, *, member_name: str = "<ATMB>") -> ParsedAtmb:
    if len(data) < 8 or data[:4] != b"ATMB":
        raise ValueError(f"invalid ATMB signature in {member_name}")
    count = struct.unpack_from("<I", data, 4)[0]
    table_end = 8 + count * 4
    if table_end > len(data):
        raise ValueError(f"truncated ATMB offset table in {member_name}")
    if count:
        offsets = tuple(struct.unpack_from(f"<{count}I", data, 8))
        if offsets[0] != table_end:
            raise ValueError(
                f"unexpected first ATMB record offset in {member_name}: "
                f"0x{offsets[0]:X} != 0x{table_end:X}"
            )
        if any(left > right for left, right in zip(offsets, offsets[1:])):
            raise ValueError(f"unsorted ATMB offsets in {member_name}")
        if offsets[-1] > len(data):
            raise ValueError(f"ATMB record outside member in {member_name}")
    else:
        offsets = ()
        if len(data) != 8:
            raise ValueError(f"zero-record ATMB has trailing data in {member_name}")

    records = tuple(
        data[offset : offsets[index + 1] if index + 1 < count else len(data)]
        for index, offset in enumerate(offsets)
    )
    return ParsedAtmb(data, offsets, records)


def rebuild_atmb(records: Sequence[bytes]) -> bytes:
    count = len(records)
    cursor = 8 + count * 4
    offsets: list[int] = []
    for record in records:
        offsets.append(cursor)
        cursor += len(record)
        if cursor > 0xFFFFFFFF:
            raise OverflowError("ATMB member exceeds 32-bit offset range")
    output = bytearray(b"ATMB")
    output.extend(struct.pack("<I", count))
    if offsets:
        output.extend(struct.pack(f"<{count}I", *offsets))
    output.extend(b"".join(records))
    return bytes(output)


def rebuild_pak(parsed: ParsedPak | bytes, replacements: Mapping[str, bytes]) -> bytes:
    """Rebuild a PAK, preserving a no-change archive bit-for-bit."""

    if isinstance(parsed, bytes):
        parsed = parse_pak(parsed)
    unknown = set(replacements).difference(member.name for member in parsed.members)
    if unknown:
        raise KeyError(f"unknown PAK members: {sorted(unknown)!r}")

    output = bytearray(parsed.source[: parsed.first_data_offset])
    new_extents: list[tuple[int, int]] = []
    for index, member in enumerate(parsed.members):
        if len(output) % PAK_ALIGNMENT:
            raise AssertionError("internal PAK alignment error")
        member_offset = len(output)
        member_data = replacements.get(member.name, member.data)
        output.extend(member_data)
        new_extents.append((member_offset, len(member_data)))

        if index + 1 < len(parsed.members):
            new_end = align(len(output))
            new_padding_size = new_end - len(output)
            original_next = parsed.members[index + 1].offset
            original_padding = parsed.source[member.offset + member.size : original_next]
            if len(member_data) == member.size and len(original_padding) == new_padding_size:
                output.extend(original_padding)
            else:
                output.extend(b"\0" * new_padding_size)
        else:
            original_trailing = parsed.source[member.offset + member.size :]
            output.extend(original_trailing)

    for member, (offset, size) in zip(parsed.members, new_extents):
        struct.pack_into(">II", output, 4 + member.index * 40 + 32, offset, size)
    return bytes(output)


def extract_text_spans(record: bytes) -> list[TextSpan]:
    spans: list[TextSpan] = []
    position = 0
    while position + 1 < len(record):
        opener = record[position : position + 2]
        closer = TEXT_PAIRS.get(opener)
        if closer is None:
            position += 1
            continue
        closing_position = record.find(closer, position + 2)
        if closing_position < 0:
            position += 2
            continue
        end = closing_position + 2
        raw = record[position:end]
        try:
            text = raw.decode("cp932")
            decode_ok = True
        except UnicodeDecodeError:
            text = raw.decode("cp932", errors="replace")
            decode_ok = False
        spans.append(TextSpan(len(spans), position, end, raw, text, decode_ok))
        position = end
    return spans


def stable_id(member_name: str, record_index: int, text_index: int) -> str:
    return f"bpilot:{member_name}:r{record_index:05d}:t{text_index:02d}"


def _extract_from_bytes(data: bytes) -> list[dict[str, object]]:
    pak = parse_pak(data)
    output: list[dict[str, object]] = []
    for member in pak.members:
        if not member.name.lower().endswith(".bin"):
            continue
        atmb = parse_atmb(member.data, member_name=member.name)
        for record_index, (record_offset, record) in enumerate(zip(atmb.offsets, atmb.records)):
            record_hash = hashlib.sha1(record).hexdigest().upper()
            for span in extract_text_spans(record):
                member_relative_offset = record_offset + span.start
                output.append(
                    {
                        "id": stable_id(member.name, record_index, span.text_index),
                        "source_file": "bpilot.pak",
                        "container": "ATMB",
                        "member": member.name,
                        "member_index": member.index,
                        "record_index": record_index,
                        "text_index": span.text_index,
                        "pak_offset": member.offset + member_relative_offset,
                        "member_offset": member_relative_offset,
                        "record_offset": span.start,
                        "byte_length": len(span.raw),
                        "record_sha1": record_hash,
                        "japanese": span.text,
                        "raw_hex": span.raw.hex().upper(),
                        "decode_ok": span.decode_ok,
                    }
                )
    return output


def extract_records(source: Path) -> list[dict[str, object]]:
    """Extract all bpilot dialogue occurrences from *source*."""

    return _extract_from_bytes(Path(source).read_bytes())


def normalize_japanese(text: str) -> str:
    """Normalize layout-only differences without changing lexical content."""

    text = text.replace("\u00a0", " ").replace("\u2014", "\u2015")
    return "".join(character for character in text if not character.isspace())


def _translation_rows(csv_path: Path) -> list[dict[str, object]]:
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        if len(fieldnames) < 4:
            raise ValueError("bpilot translation CSV must have at least four columns")
        offset_key, japanese_key, korean_key = fieldnames[0], fieldnames[2], fieldnames[3]
        rows = []
        for row_number, row in enumerate(reader, 2):
            japanese = row.get(japanese_key, "")
            korean = row.get(korean_key, "")
            rows.append(
                {
                    "row": row_number,
                    "offsets": row.get(offset_key, ""),
                    "japanese": japanese,
                    "korean": korean,
                    "normalized": normalize_japanese(japanese),
                }
            )
    return rows


def annotate_translations(
    records: Sequence[Mapping[str, object]],
    csv_path: Path,
    *,
    fuzzy: bool = False,
    fuzzy_cutoff: float = 72.0,
) -> list[dict[str, object]]:
    """Attach exact/layout-normalized existing CSV translations to records.

    Fuzzy results are suggestions only; they are never promoted to an existing
    translation automatically.
    """

    rows = _translation_rows(csv_path)
    by_exact: dict[str, list[dict[str, object]]] = defaultdict(list)
    by_normalized: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        by_exact[str(row["japanese"])].append(row)
        by_normalized[str(row["normalized"])].append(row)

    fuzzy_cache: dict[str, list[dict[str, object]]] = {}
    normalized_choices = list(by_normalized)
    if fuzzy:
        try:
            from rapidfuzz import fuzz, process
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError("--fuzzy requires rapidfuzz") from exc

    annotated: list[dict[str, object]] = []
    for original in records:
        item = dict(original)
        japanese = str(item.get("japanese", ""))
        normalized = normalize_japanese(japanese)
        exact_hits = by_exact.get(japanese, [])
        hits = exact_hits or by_normalized.get(normalized, [])
        translations = sorted({str(hit["korean"]) for hit in hits if str(hit["korean"])})
        match_kind = "exact" if exact_hits else "normalized" if hits else "unmatched"
        if len(translations) > 1:
            match_kind = "conflict"

        item["existing_match"] = match_kind
        item["match_confidence"] = (
            1.0 if match_kind == "exact" else 0.995 if match_kind == "normalized" else 0.0
        )
        item["existing_korean"] = translations[0] if len(translations) == 1 else None
        item["translation_candidates"] = translations
        item["csv_rows"] = [int(hit["row"]) for hit in hits]
        item["csv_offsets"] = [str(hit["offsets"]) for hit in hits]

        if fuzzy and not hits and normalized:
            if normalized not in fuzzy_cache:
                suggestions: list[dict[str, object]] = []
                for choice, score, _ in process.extract(
                    normalized,
                    normalized_choices,
                    scorer=fuzz.ratio,
                    score_cutoff=fuzzy_cutoff,
                    limit=3,
                ):
                    candidate_rows = by_normalized[choice]
                    suggestions.append(
                        {
                            "score": round(float(score) / 100.0, 4),
                            "japanese": candidate_rows[0]["japanese"],
                            "korean_candidates": sorted(
                                {str(row["korean"]) for row in candidate_rows if str(row["korean"])}
                            ),
                            "csv_rows": [int(row["row"]) for row in candidate_rows],
                        }
                    )
                fuzzy_cache[normalized] = suggestions
            item["fuzzy_suggestions"] = fuzzy_cache[normalized]
        annotated.append(item)
    return annotated


def _align_reference_record(original: bytes, reference: bytes) -> list[bytes] | None:
    """Recover variable text fields using the unchanged bytecode between them."""

    spans = extract_text_spans(original)
    if not spans:
        return []
    static_segments: list[bytes] = []
    cursor = 0
    for span in spans:
        static_segments.append(original[cursor : span.start])
        cursor = span.end
    static_segments.append(original[cursor:])

    if not reference.startswith(static_segments[0]):
        return None
    cursor = len(static_segments[0])
    result: list[bytes] = []
    for index in range(len(spans)):
        separator = static_segments[index + 1]
        if index == len(spans) - 1:
            if separator:
                if not reference.endswith(separator):
                    return None
                end = len(reference) - len(separator)
            else:
                end = len(reference)
        else:
            end = reference.find(separator, cursor)
            if end < 0:
                return None
        if end < cursor:
            return None
        result.append(reference[cursor:end])
        cursor = end + len(separator)
    return result


def reference_text_map(source: Path, reference: Path) -> dict[str, dict[str, object]]:
    """Align a repacked reference PAK (notably the public English patch)."""

    original_pak = parse_pak(source.read_bytes())
    reference_pak = parse_pak(reference.read_bytes())
    if len(original_pak.members) != len(reference_pak.members):
        raise ValueError("reference PAK member count differs from original")

    result: dict[str, dict[str, object]] = {}
    for original_member, reference_member in zip(original_pak.members, reference_pak.members):
        if original_member.name != reference_member.name:
            raise ValueError(
                f"reference member mismatch at {original_member.index}: "
                f"{original_member.name!r} != {reference_member.name!r}"
            )
        if not original_member.name.lower().endswith(".bin"):
            continue
        original_atmb = parse_atmb(original_member.data, member_name=original_member.name)
        reference_atmb = parse_atmb(reference_member.data, member_name=reference_member.name)
        if len(original_atmb.records) != len(reference_atmb.records):
            raise ValueError(f"reference ATMB record count differs: {original_member.name}")
        for record_index, (original_record, reference_record) in enumerate(
            zip(original_atmb.records, reference_atmb.records)
        ):
            spans = extract_text_spans(original_record)
            if not spans:
                continue
            if original_record == reference_record:
                for span in spans:
                    result[stable_id(original_member.name, record_index, span.text_index)] = {
                        "reference_match": "unchanged",
                        "english_reference": None,
                        "reference_raw_hex": None,
                    }
                continue
            aligned = _align_reference_record(original_record, reference_record)
            if aligned is None or len(aligned) != len(spans):
                for span in spans:
                    result[stable_id(original_member.name, record_index, span.text_index)] = {
                        "reference_match": "failed",
                        "english_reference": None,
                        "reference_raw_hex": None,
                    }
                continue
            for span, raw in zip(spans, aligned):
                result[stable_id(original_member.name, record_index, span.text_index)] = {
                    "reference_match": "aligned",
                    "english_reference": raw.decode("cp932", errors="replace"),
                    "reference_raw_hex": raw.hex().upper(),
                }
    return result


def annotate_reference(
    records: Sequence[Mapping[str, object]], source: Path, reference: Path
) -> list[dict[str, object]]:
    mapping = reference_text_map(source, reference)
    annotated: list[dict[str, object]] = []
    for original in records:
        item = dict(original)
        item.update(
            mapping.get(
                str(item["id"]),
                {
                    "reference_match": "missing",
                    "english_reference": None,
                    "reference_raw_hex": None,
                },
            )
        )
        annotated.append(item)
    return annotated


def load_codebook(path: Path) -> dict[str, int]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = csv.DictReader(handle)
        result = {row["target"]: int(row["code"], 16) for row in rows}
    if not result:
        raise ValueError("empty Korean font codebook")
    return result


def make_codebook_encoder(codebook_path: Path) -> Callable[[str], bytes]:
    codebook = load_codebook(codebook_path)

    def encode(text: str) -> bytes:
        output = bytearray()
        for character in text:
            replacement = NORMALIZE_FOR_ENCODING.get(character, character)
            for normalized_character in replacement:
                if normalized_character in codebook:
                    output.extend(codebook[normalized_character].to_bytes(2, "big"))
                else:
                    output.extend(normalized_character.encode("cp932"))
        return bytes(output)

    return encode


def repack(
    source: Path,
    replacements: Mapping[str, str | bytes],
    encoder: Callable[[str], bytes],
) -> bytes:
    """Apply stable-ID replacements and return a fully rebuilt bpilot PAK."""

    source_data = Path(source).read_bytes()
    parsed = parse_pak(source_data)
    wanted = {key: value for key, value in replacements.items() if key.startswith("bpilot:")}
    if not wanted:
        return rebuild_pak(parsed, {})

    seen: set[str] = set()
    member_replacements: dict[str, bytes] = {}
    for member in parsed.members:
        if not member.name.lower().endswith(".bin"):
            continue
        atmb = parse_atmb(member.data, member_name=member.name)
        new_records: list[bytes] = []
        member_changed = False
        for record_index, record in enumerate(atmb.records):
            spans = extract_text_spans(record)
            applicable: list[tuple[TextSpan, str | bytes]] = []
            for span in spans:
                identifier = stable_id(member.name, record_index, span.text_index)
                if identifier in wanted:
                    applicable.append((span, wanted[identifier]))
                    seen.add(identifier)
            if not applicable:
                new_records.append(record)
                continue

            rebuilt_record = bytearray()
            cursor = 0
            for span, replacement in applicable:
                rebuilt_record.extend(record[cursor : span.start])
                encoded = replacement if isinstance(replacement, bytes) else encoder(replacement)
                rebuilt_record.extend(encoded)
                cursor = span.end
            rebuilt_record.extend(record[cursor:])
            new_records.append(bytes(rebuilt_record))
            member_changed = member_changed or bytes(rebuilt_record) != record
        if member_changed:
            member_replacements[member.name] = rebuild_atmb(new_records)

    missing = set(wanted).difference(seen)
    if missing:
        preview = sorted(missing)[:10]
        raise KeyError(f"replacement IDs not found ({len(missing)}): {preview!r}")
    return rebuild_pak(parsed, member_replacements)


def load_replacements(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and all(isinstance(value, str) for value in payload.values()):
        return {str(key): str(value) for key, value in payload.items()}
    if isinstance(payload, dict):
        for key in ("entries", "records", "texts"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list):
        raise ValueError("replacement JSON must be an ID map or an entry list")

    result: dict[str, str] = {}
    for item in payload:
        if not isinstance(item, dict) or not str(item.get("id", "")).startswith("bpilot:"):
            continue
        translation = None
        for key in ("final_korean", "korean", "translation", "existing_korean"):
            value = item.get(key)
            if isinstance(value, str) and value:
                translation = value
                break
        if translation is not None:
            result[str(item["id"])] = translation
    return result


def verification_report(source: Path, reference: Path | None = None) -> dict[str, object]:
    original = source.read_bytes()
    parsed = parse_pak(original)
    bin_members = [member for member in parsed.members if member.name.lower().endswith(".bin")]

    atmb_roundtrips = 0
    record_count = 0
    for member in bin_members:
        atmb = parse_atmb(member.data, member_name=member.name)
        record_count += len(atmb.records)
        if rebuild_atmb(atmb.records) != member.data:
            raise AssertionError(f"ATMB no-change round-trip failed: {member.name}")
        atmb_roundtrips += 1

    no_change = rebuild_pak(parsed, {})
    if no_change != original:
        raise AssertionError("PAK no-change round-trip is not bit-identical")

    entries = _extract_from_bytes(original)
    target = next(entry for entry in entries if entry["decode_ok"])
    old_text = str(target["japanese"])
    synthetic_text = old_text[:-1] + " TEST-EXPANSION-0123456789" + old_text[-1:]
    expanded = repack(source, {str(target["id"]): synthetic_text}, lambda value: value.encode("cp932"))
    expanded_parsed = parse_pak(expanded)
    expanded_entries = {str(item["id"]): item for item in _extract_from_bytes(expanded)}
    if str(target["id"]) not in expanded_entries:
        raise AssertionError("synthetic expansion target disappeared")
    if expanded_entries[str(target["id"])]["japanese"] != synthetic_text:
        raise AssertionError("synthetic expansion text mismatch")
    if rebuild_pak(expanded_parsed, {}) != expanded:
        raise AssertionError("expanded PAK does not round-trip")

    original_members = {member.name: member.data for member in parsed.members}
    expanded_members = {member.name: member.data for member in expanded_parsed.members}
    changed_members = [name for name in original_members if original_members[name] != expanded_members[name]]
    if changed_members != [str(target["member"])]:
        raise AssertionError(f"unexpected changed members: {changed_members!r}")
    if any(member.offset % PAK_ALIGNMENT for member in expanded_parsed.members):
        raise AssertionError("expanded PAK contains an unaligned member")
    if source.read_bytes() != original:
        raise AssertionError("verification modified the source file")

    decode_errors = sum(not bool(entry["decode_ok"]) for entry in entries)
    report: dict[str, object] = {
        "source": str(source),
        "source_size": len(original),
        "source_sha256": sha256(original),
        "pak_members": len(parsed.members),
        "bin_members": len(bin_members),
        "atmb_records": record_count,
        "text_occurrences": len(entries),
        "text_decode_errors": decode_errors,
        "no_change": {
            "atmb_members_bit_identical": atmb_roundtrips,
            "pak_bit_identical": no_change == original,
            "rebuilt_sha256": sha256(no_change),
        },
        "synthetic_expansion": {
            "target_id": target["id"],
            "target_member": target["member"],
            "old_byte_length": target["byte_length"],
            "new_byte_length": expanded_entries[str(target["id"])]["byte_length"],
            "pak_old_size": len(original),
            "pak_new_size": len(expanded),
            "changed_members": changed_members,
            "all_member_offsets_aligned": True,
            "expanded_archive_roundtrip": True,
        },
        "source_unchanged_after_tests": True,
    }
    if reference is not None:
        reference_data = reference.read_bytes()
        reference_pak = parse_pak(reference_data)
        names_equal = [member.name for member in parsed.members] == [
            member.name for member in reference_pak.members
        ]
        if not names_equal or len(reference_pak.members) != len(parsed.members):
            raise AssertionError("reference PAK directory differs from retail PAK")
        equality_by_extension: Counter[str] = Counter()
        record_count_mismatches: list[str] = []
        for original_member, reference_member in zip(parsed.members, reference_pak.members):
            extension = Path(original_member.name).suffix.lower()
            equality_by_extension[f"{extension}:{'same' if original_member.data == reference_member.data else 'changed'}"] += 1
            if extension == ".bin":
                original_count = len(
                    parse_atmb(original_member.data, member_name=original_member.name).records
                )
                reference_count = len(
                    parse_atmb(reference_member.data, member_name=reference_member.name).records
                )
                if original_count != reference_count:
                    record_count_mismatches.append(original_member.name)
        report["reference_comparison"] = {
            "reference": str(reference),
            "reference_size": len(reference_data),
            "reference_sha256": sha256(reference_data),
            "directory_names_and_order_equal": names_equal,
            "member_payloads": dict(sorted(equality_by_extension.items())),
            "all_atmb_record_counts_equal": not record_count_mismatches,
            "atmb_record_count_mismatches": record_count_mismatches,
            "non_bin_members_all_unchanged": all(
                original_member.data == reference_member.data
                for original_member, reference_member in zip(parsed.members, reference_pak.members)
                if not original_member.name.lower().endswith(".bin")
            ),
        }
    return report


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract_parser = subparsers.add_parser("extract", help="extract stable-ID text entries")
    extract_parser.add_argument("source", type=Path)
    extract_parser.add_argument("destination", type=Path)
    extract_parser.add_argument("--csv", type=Path, help="existing Korean translation CSV")
    extract_parser.add_argument("--reference", type=Path, help="aligned English/reference bpilot PAK")
    extract_parser.add_argument("--fuzzy", action="store_true", help="include fuzzy suggestions")
    extract_parser.add_argument("--fuzzy-cutoff", type=float, default=72.0)

    repack_parser = subparsers.add_parser("repack", help="apply replacements and rebuild PAK")
    repack_parser.add_argument("source", type=Path)
    repack_parser.add_argument("destination", type=Path)
    repack_parser.add_argument("--replacements", type=Path, required=True)
    repack_parser.add_argument("--codebook", type=Path, required=True)
    repack_parser.add_argument("--report", type=Path)

    verify_parser = subparsers.add_parser("verify", help="run no-change and expansion tests")
    verify_parser.add_argument("source", type=Path)
    verify_parser.add_argument("--report", type=Path, required=True)
    verify_parser.add_argument("--reference", type=Path, help="optional English/reference PAK")

    args = parser.parse_args()
    if args.command == "extract":
        records = extract_records(args.source)
        if args.csv:
            records = annotate_translations(
                records, args.csv, fuzzy=args.fuzzy, fuzzy_cutoff=args.fuzzy_cutoff
            )
        if args.reference:
            records = annotate_reference(records, args.source, args.reference)
        payload = {
            "format": "srw-gc-bpilot-text-v1",
            "source": str(args.source),
            "source_sha256": sha256(args.source.read_bytes()),
            "entry_count": len(records),
            "entries": records,
        }
        write_json(args.destination, payload)
        summary = Counter(str(item.get("existing_match", "unannotated")) for item in records)
        print(json.dumps({"entry_count": len(records), "matches": summary}, ensure_ascii=False, default=dict))
        return 0

    if args.command == "repack":
        replacements = load_replacements(args.replacements)
        encoder = make_codebook_encoder(args.codebook)
        result = repack(args.source, replacements, encoder)
        args.destination.parent.mkdir(parents=True, exist_ok=True)
        args.destination.write_bytes(result)
        report = {
            "source": str(args.source),
            "source_sha256": sha256(args.source.read_bytes()),
            "destination": str(args.destination),
            "destination_size": len(result),
            "destination_sha256": sha256(result),
            "replacement_count": len(replacements),
            "parsed_output_members": len(parse_pak(result).members),
        }
        if args.report:
            write_json(args.report, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    report = verification_report(args.source, args.reference)
    write_json(args.report, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
