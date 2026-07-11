#!/usr/bin/env python3
"""Extract and safely repack SRW GC ``add01dat.bin`` text.

The container begins with a big-endian table of absolute file offsets.  Each
non-zero table entry identifies one 0x20-aligned script block.  Text-bearing
script commands observed in both the Japanese retail file and the public
English patch are::

    70 34 <4 parameter bytes> <CP932/control text> 45 45
    70 35 <6 parameter bytes> <CP932/control text> 45 45

``45 45`` is the ASCII ``EE`` end control and ``4B 4B`` (``KK``) is the line
control.  Controls must be scanned at CP932 character boundaries: a naive
``bytes.find(b"EE")`` can mistake the trail byte of U+30FB (81 45) for the
first E.  The parser below is boundary-aware for this reason.

The public API intended for the unified translation builder is:

``extract_records(Path) -> list[dict]``
    Return stable record IDs and decoded source text.

``repack(Path, replacements: dict[str, str], encoder=None) -> bytes``
    Replace records by stable ID, rebuild every affected block, realign it,
    and rewrite all outer absolute offsets.  Text may grow without an
    in-place slot limit.  ``encoder`` is called once per visible line; when it
    is omitted, CP932 is used.

No operation writes the source file.  The CLI also refuses to overwrite an
output unless ``--force`` is explicitly supplied.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence


ALIGNMENT = 0x20
CONTAINER_NAME = "add01dat.bin"
END_CONTROL = b"EE"
LINE_CONTROL = b"KK"
COMMAND_HEADERS: dict[bytes, int] = {
    b"\x70\x34": 6,  # opcode + four parameter bytes
    b"\x70\x35": 8,  # opcode + six parameter bytes
}
NORMALIZE = {
    "\u00a0": " ",
    "\u00b7": "\u30fb",
    "\u2014": "\u2015",
    "\u2049": "!?",
    "\u11ab": "\u3134",
    "\u11b7": "\u3141",
    "\u11bc": "\u3147",
    "\uff63": "\u300d",
}
STRUCTURAL_TEXT_TOKENS = {
    "<AA>": b"AA",
    "<FF>": b"FF",
    "<TT>": b"TT",
}
QUOTE_OPENERS = ("\u300c", "\u300e", "\uff08", "(")


class Add01FormatError(ValueError):
    """Raised when an add01 container fails a structural guard."""


@dataclass(frozen=True)
class TextRecord:
    block_index: int
    table_slot: int
    record_index: int
    command_offset: int
    payload_offset: int
    payload_end: int
    opcode: bytes
    header: bytes
    payload: bytes

    @property
    def stable_id(self) -> str:
        return record_id(self.block_index, self.record_index)


@dataclass(frozen=True)
class ScriptBlock:
    block_index: int
    table_slot: int
    file_offset: int
    data: bytes
    records: tuple[TextRecord, ...]


@dataclass(frozen=True)
class Add01Container:
    source: bytes
    table_size: int
    pointers: tuple[int, ...]
    blocks: tuple[ScriptBlock, ...]


def _read_source(source: Path | str | bytes | bytearray | memoryview) -> bytes:
    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source)
    return Path(source).read_bytes()


def _is_cp932_lead(value: int) -> bool:
    return 0x81 <= value <= 0x9F or 0xE0 <= value <= 0xFC


def _scan_end_control(data: bytes, start: int) -> int:
    """Return the EE offset, considering controls only at char boundaries."""

    cursor = start
    while cursor < len(data):
        if data[cursor : cursor + 2] == END_CONTROL:
            return cursor
        if _is_cp932_lead(data[cursor]):
            if cursor + 1 >= len(data):
                raise Add01FormatError(
                    f"truncated CP932/custom double-byte character at 0x{cursor:X}"
                )
            cursor += 2
        else:
            cursor += 1
    raise Add01FormatError(f"text command at 0x{start:X} has no boundary-aligned EE")


def _split_line_controls(payload: bytes) -> list[bytes]:
    parts: list[bytes] = []
    start = 0
    cursor = 0
    while cursor < len(payload):
        if payload[cursor : cursor + 2] == LINE_CONTROL:
            parts.append(payload[start:cursor])
            cursor += 2
            start = cursor
            continue
        if _is_cp932_lead(payload[cursor]):
            if cursor + 1 >= len(payload):
                raise Add01FormatError("truncated double-byte character in text payload")
            cursor += 2
        else:
            cursor += 1
    parts.append(payload[start:])
    return parts


def decode_payload(payload: bytes) -> tuple[str, list[str]]:
    """Decode visible lines and return both the KK form and a line list."""

    raw_lines = _split_line_controls(payload)
    try:
        lines = [part.decode("cp932") for part in raw_lines]
    except UnicodeDecodeError as exc:
        raise Add01FormatError(f"invalid CP932 text payload: {exc}") from exc
    return "KK".join(lines), lines


def _normalize_text(text: str) -> str:
    return "".join(NORMALIZE.get(character, character) for character in text)


def _escape_literal_controls(text: str) -> tuple[str, list[str]]:
    """Avoid accidental ASCII EE/KK inside one visible line.

    Literal controls are rendered with visually equivalent full-width Latin
    letters.  Intentional line breaks have already been split before this
    function is called.
    """

    escaped: list[str] = []
    for token, replacement in (("EE", "\uff25\uff25"), ("KK", "\uff2b\uff2b")):
        if token in text:
            text = text.replace(token, replacement)
            escaped.append(token)
    return text, escaped


def encode_text(
    text: str,
    encoder: Callable[[str], bytes] | None = None,
    *,
    escape_literal_controls: bool = True,
) -> tuple[bytes, list[str]]:
    """Encode replacement text, converting newlines/literal KK to controls."""

    normalized = _normalize_text(text).replace("\r\n", "\n").replace("\r", "\n")
    # The supplied Japanese CSV convention uses literal KK.  Korean fields use
    # real newlines.  Supporting both keeps the public API easy to integrate.
    normalized = normalized.replace("KK", "\n")
    lines = normalized.split("\n")
    encode_line = encoder or (lambda value: value.encode("cp932"))
    encoded_lines: list[bytes] = []
    escaped_tokens: list[str] = []
    for line in lines:
        if escape_literal_controls:
            line, escaped = _escape_literal_controls(line)
            escaped_tokens.extend(escaped)
        try:
            encoded = bytes(encode_line(line))
        except (UnicodeEncodeError, KeyError) as exc:
            raise Add01FormatError(f"cannot encode replacement line {line!r}: {exc}") from exc
        # Validate that no encoder-generated byte sequence becomes a control at
        # a character boundary.  This also catches malformed custom encoders.
        cursor = 0
        while cursor < len(encoded):
            if encoded[cursor : cursor + 2] in (END_CONTROL, LINE_CONTROL):
                raise Add01FormatError(
                    f"encoder produced reserved control {encoded[cursor:cursor+2]!r} "
                    f"inside visible text {line!r}"
                )
            if _is_cp932_lead(encoded[cursor]):
                if cursor + 1 >= len(encoded):
                    raise Add01FormatError("encoder produced a truncated double-byte code")
                cursor += 2
            else:
                cursor += 1
        encoded_lines.append(encoded)
    return LINE_CONTROL.join(encoded_lines), escaped_tokens


def record_id(block_index: int, record_index: int) -> str:
    """Return a stable ID independent of current byte offsets/string lengths."""

    return f"add01:{block_index:04d}:{record_index:04d}"


def _find_next_command(data: bytes, start: int) -> tuple[int, bytes, int] | None:
    candidates: list[tuple[int, bytes, int]] = []
    for opcode, header_size in COMMAND_HEADERS.items():
        position = data.find(opcode, start)
        if position >= 0:
            candidates.append((position, opcode, header_size))
    return min(candidates, key=lambda item: item[0]) if candidates else None


def _parse_block_records(
    block_data: bytes,
    block_index: int,
    table_slot: int,
) -> tuple[TextRecord, ...]:
    records: list[TextRecord] = []
    cursor = 0
    while True:
        found = _find_next_command(block_data, cursor)
        if found is None:
            break
        command_offset, opcode, header_size = found
        payload_offset = command_offset + header_size
        payload_end = _scan_end_control(block_data, payload_offset)
        payload = block_data[payload_offset:payload_end]
        # Strict decoding is an important false-positive guard for the opcode
        # search.  Controls are removed before decoding visible segments.
        decode_payload(payload)
        records.append(
            TextRecord(
                block_index=block_index,
                table_slot=table_slot,
                record_index=len(records),
                command_offset=command_offset,
                payload_offset=payload_offset,
                payload_end=payload_end,
                opcode=opcode,
                header=block_data[command_offset:payload_offset],
                payload=payload,
            )
        )
        cursor = payload_end + len(END_CONTROL)
    return tuple(records)


def parse_container(source: Path | str | bytes | bytearray | memoryview) -> Add01Container:
    data = _read_source(source)
    if len(data) < 4:
        raise Add01FormatError("file is too small for an add01 offset table")
    table_size = struct.unpack_from(">I", data, 0)[0]
    if not (4 <= table_size <= len(data)) or table_size % 4:
        raise Add01FormatError(f"invalid first pointer/table size 0x{table_size:X}")
    pointers = struct.unpack_from(f">{table_size // 4}I", data, 0)
    nonzero = [(slot, pointer) for slot, pointer in enumerate(pointers) if pointer]
    if not nonzero:
        raise Add01FormatError("offset table contains no data blocks")
    values = [pointer for _, pointer in nonzero]
    if values[0] != table_size:
        raise Add01FormatError(
            f"first data pointer 0x{values[0]:X} does not equal table size 0x{table_size:X}"
        )
    if len(values) != len(set(values)) or any(a >= b for a, b in zip(values, values[1:])):
        raise Add01FormatError("non-zero block pointers must be unique and strictly increasing")
    if any(pointer % ALIGNMENT for pointer in values):
        raise Add01FormatError("one or more script block pointers are not 0x20-aligned")
    if values[-1] >= len(data):
        raise Add01FormatError("last script block pointer is outside the file")

    blocks: list[ScriptBlock] = []
    for block_index, (table_slot, pointer) in enumerate(nonzero):
        end = values[block_index + 1] if block_index + 1 < len(values) else len(data)
        block_data = data[pointer:end]
        records = _parse_block_records(block_data, block_index, table_slot)
        blocks.append(
            ScriptBlock(
                block_index=block_index,
                table_slot=table_slot,
                file_offset=pointer,
                data=block_data,
                records=records,
            )
        )
    return Add01Container(
        source=data,
        table_size=table_size,
        pointers=tuple(pointers),
        blocks=tuple(blocks),
    )


def _looks_like_speaker(lines: Sequence[str]) -> str | None:
    if len(lines) < 2:
        return None
    candidate = lines[0].strip("\u3000 ")
    following = lines[1].lstrip("\u3000 ")
    if not candidate or len(candidate) > 32:
        return None
    if any(candidate.startswith(mark) for mark in QUOTE_OPENERS):
        return None
    if any(following.startswith(mark) for mark in QUOTE_OPENERS):
        return candidate
    return None


def _contains_japanese(text: str) -> bool:
    return bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff]", text))


def extract_records(
    source: Path | str | bytes | bytearray | memoryview,
) -> list[dict[str, object]]:
    """Extract all add01 script text with stable IDs and fresh offsets."""

    container = parse_container(source)
    extracted: list[dict[str, object]] = []
    for block in container.blocks:
        for record in block.records:
            payload_japanese, payload_lines = decode_payload(record.payload)
            speaker = _looks_like_speaker(payload_lines)
            dialogue_lines = payload_lines[1:] if speaker is not None else payload_lines
            japanese = "KK".join(dialogue_lines)
            extracted.append(
                {
                    "id": record.stable_id,
                    "container": CONTAINER_NAME,
                    "block": record.block_index,
                    "table_slot": record.table_slot,
                    "record": record.record_index,
                    "offset": block.file_offset + record.payload_offset,
                    "offset_hex": f"0x{block.file_offset + record.payload_offset:08X}",
                    "command_offset": block.file_offset + record.command_offset,
                    "opcode": f"0x{int.from_bytes(record.opcode, 'big'):04X}",
                    "parameters_hex": record.header[2:].hex().upper(),
                    "japanese": japanese,
                    "display_text": "\n".join(dialogue_lines),
                    "speaker": speaker,
                    "speaker_embedded": speaker is not None,
                    "payload_japanese": payload_japanese,
                    "payload_display_text": "\n".join(payload_lines),
                    "line_control": "KK" if len(payload_lines) > 1 else None,
                    "line_count": len(dialogue_lines),
                    "lines": dialogue_lines,
                    "payload_lines": payload_lines,
                    "raw_hex": record.payload.hex().upper(),
                    "contains_japanese": _contains_japanese(payload_japanese),
                }
            )
    return extracted


def _structural_skeleton(block: bytes) -> bytes:
    records = _parse_block_records(block, 0, 0)
    result = bytearray()
    cursor = 0
    for record in records:
        result.extend(block[cursor : record.payload_offset])
        result.extend(b"<ADD01_TEXT>")
        cursor = record.payload_end
    result.extend(block[cursor:])
    # Only outer alignment padding is immaterial.  Meaningful zero parameters
    # before the final non-zero command byte remain part of the comparison.
    return bytes(result).rstrip(b"\x00")


def _coerce_replacement_bytes(
    value: str | bytes | bytearray | memoryview,
    encoder: Callable[[str], bytes] | None,
) -> tuple[bytes, list[str]]:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value), []
    return encode_text(str(value), encoder)


def repack(
    source: Path | str | bytes | bytearray | memoryview,
    replacements: Mapping[str, str | bytes | bytearray | memoryview],
    encoder: Callable[[str], bytes] | None = None,
) -> bytes:
    """Rebuild add01 with length-unlimited replacements keyed by stable ID."""

    container = parse_container(source)
    known_ids = {
        record.stable_id
        for block in container.blocks
        for record in block.records
    }
    unknown = sorted(set(replacements) - known_ids)
    if unknown:
        preview = ", ".join(unknown[:8])
        raise Add01FormatError(f"replacement IDs not present in source: {preview}")

    output = bytearray(container.source[: container.table_size])
    new_offsets: dict[int, int] = {}
    expected_payloads: dict[str, bytes] = {}
    for block in container.blocks:
        while len(output) % ALIGNMENT:
            output.append(0)
        new_offsets[block.table_slot] = len(output)
        rebuilt = bytearray()
        cursor = 0
        for record in block.records:
            rebuilt.extend(block.data[cursor : record.payload_offset])
            if record.stable_id in replacements:
                payload, _ = _coerce_replacement_bytes(
                    replacements[record.stable_id], encoder
                )
            else:
                payload = record.payload
            rebuilt.extend(payload)
            expected_payloads[record.stable_id] = payload
            cursor = record.payload_end
        rebuilt.extend(block.data[cursor:])
        while len(rebuilt) % ALIGNMENT:
            rebuilt.append(0)
        output.extend(rebuilt)

    for table_slot, old_pointer in enumerate(container.pointers):
        pointer = new_offsets[table_slot] if old_pointer else 0
        struct.pack_into(">I", output, table_slot * 4, pointer)

    rebuilt_container = parse_container(output)
    if len(rebuilt_container.blocks) != len(container.blocks):
        raise Add01FormatError("post-build block count changed")
    for old_block, new_block in zip(container.blocks, rebuilt_container.blocks):
        if len(old_block.records) != len(new_block.records):
            raise Add01FormatError(
                f"post-build text count changed in block {old_block.block_index}"
            )
        if _structural_skeleton(old_block.data) != _structural_skeleton(new_block.data):
            raise Add01FormatError(
                f"non-text script bytes changed in block {old_block.block_index}"
            )
        for new_record in new_block.records:
            if new_record.payload != expected_payloads[new_record.stable_id]:
                raise Add01FormatError(
                    f"post-build payload mismatch for {new_record.stable_id}"
                )
    return bytes(output)


def load_codebook(path: Path | str) -> dict[str, int]:
    result: dict[str, int] = {}
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            target = row.get("target")
            code = row.get("code")
            if target and code:
                result[target] = int(code, 16)
    if not result:
        raise Add01FormatError(f"no target/code mappings found in {path}")
    return result


def make_codebook_encoder(codebook: Mapping[str, int]) -> Callable[[str], bytes]:
    def encode_line(text: str) -> bytes:
        output = bytearray()
        normalized = _normalize_text(text)
        cursor = 0
        while cursor < len(normalized):
            # add01's renderer consumes two-byte units.  AA/FF/TT are native
            # two-byte runtime tokens (dynamic names / presentation control),
            # so they must remain raw.  Every other visible ASCII character is
            # converted to its CP932 full-width form; emitting a one-byte
            # digit, space, or punctuation mark shifts KK/EE off the engine's
            # two-byte boundary and makes it display subsequent script opcodes
            # as dialogue (for example "EEp4").
            token = next(
                (
                    candidate
                    for candidate in STRUCTURAL_TEXT_TOKENS
                    if normalized.startswith(candidate, cursor)
                ),
                None,
            )
            if token is not None:
                output.extend(STRUCTURAL_TEXT_TOKENS[token])
                cursor += len(token)
                continue

            character = normalized[cursor]
            if character in codebook:
                encoded = int(codebook[character]).to_bytes(2, "big")
            else:
                codepoint = ord(character)
                if character == " ":
                    character = "\u3000"
                elif 0x21 <= codepoint <= 0x7E:
                    character = chr(codepoint + 0xFEE0)
                encoded = character.encode("cp932")
            if len(encoded) != 2:
                raise Add01FormatError(
                    f"add01 visible character is not double-byte aligned: {character!r} "
                    f"-> {encoded.hex().upper()}"
                )
            output.extend(encoded)
            cursor += 1
        if len(output) % 2:
            raise Add01FormatError("add01 encoded line has odd byte length")
        return bytes(output)

    return encode_line


def _load_replacement_document(path: Path | str) -> dict[str, str | bytes]:
    document = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(document, dict) and all(
        isinstance(key, str) and key.startswith("add01:") for key in document
    ):
        return {str(key): value for key, value in document.items()}

    if isinstance(document, dict):
        for field in ("records", "entries", "translations", "texts"):
            if isinstance(document.get(field), list):
                document = document[field]
                break
    if not isinstance(document, list):
        raise Add01FormatError("replacement JSON must be an ID map or a record list")

    result: dict[str, str | bytes] = {}
    for entry in document:
        if not isinstance(entry, dict):
            continue
        stable_id = entry.get("id")
        if not isinstance(stable_id, str) or not stable_id.startswith("add01:"):
            continue
        value: object | None = None
        for field in (
            "final_korean",
            "korean",
            "translation",
            "replacement",
            "target_text",
        ):
            candidate = entry.get(field)
            if isinstance(candidate, str) and candidate:
                value = candidate
                break
        for field in ("encoded_hex", "replacement_hex"):
            candidate = entry.get(field)
            if isinstance(candidate, str) and candidate:
                value = bytes.fromhex(candidate)
                break
        if value is None:
            continue
        if stable_id in result and result[stable_id] != value:
            raise Add01FormatError(f"conflicting duplicate replacement for {stable_id}")
        result[stable_id] = value  # type: ignore[assignment]
    return result


def compare_reference(
    original: Path | str | bytes,
    translated: Path | str | bytes,
) -> dict[str, object]:
    """Compare two builds and prove which structure changes are text-only."""

    first = parse_container(original)
    second = parse_container(translated)
    if len(first.blocks) != len(second.blocks):
        raise Add01FormatError("reference build has a different block count")
    skeleton_equal: list[int] = []
    changed: list[dict[str, object]] = []
    payload_reconstruction_equal = 0
    for old, new in zip(first.blocks, second.blocks):
        old_skeleton = _structural_skeleton(old.data)
        new_skeleton = _structural_skeleton(new.data)
        if old_skeleton == new_skeleton:
            skeleton_equal.append(old.block_index)
            rebuilt = bytearray()
            cursor = 0
            for old_record, new_record in zip(old.records, new.records):
                rebuilt.extend(old.data[cursor : old_record.payload_offset])
                rebuilt.extend(new_record.payload)
                cursor = old_record.payload_end
            rebuilt.extend(old.data[cursor:])
            if bytes(rebuilt).rstrip(b"\x00") == new.data.rstrip(b"\x00"):
                payload_reconstruction_equal += 1
        else:
            changed.append(
                {
                    "block": old.block_index,
                    "original_size": len(old.data),
                    "translated_size": len(new.data),
                    "original_records": len(old.records),
                    "translated_records": len(new.records),
                }
            )

    def properties(container: Add01Container) -> dict[str, object]:
        nonzero = [pointer for pointer in container.pointers if pointer]
        return {
            "size": len(container.source),
            "sha256": hashlib.sha256(container.source).hexdigest().upper(),
            "table_size": container.table_size,
            "table_slots": len(container.pointers),
            "zero_slots": [
                slot for slot, pointer in enumerate(container.pointers) if not pointer
            ],
            "blocks": len(container.blocks),
            "text_records": sum(len(block.records) for block in container.blocks),
            "opcode_counts": {
                "0x7034": sum(
                    record.opcode == b"\x70\x34"
                    for block in container.blocks
                    for record in block.records
                ),
                "0x7035": sum(
                    record.opcode == b"\x70\x35"
                    for block in container.blocks
                    for record in block.records
                ),
            },
            "strictly_increasing": all(a < b for a, b in zip(nonzero, nonzero[1:])),
            "all_block_offsets_0x20_aligned": all(
                pointer % ALIGNMENT == 0 for pointer in nonzero
            ),
            "file_size_0x20_aligned": len(container.source) % ALIGNMENT == 0,
        }

    return {
        "original": properties(first),
        "translated_reference": properties(second),
        "same_block_count": True,
        "text_only_structure_blocks": len(skeleton_equal),
        "payload_only_exact_reconstructions": payload_reconstruction_equal,
        "structurally_changed_blocks": len(changed),
        "changed_block_details": changed,
        "inference": (
            "For every text-only block, substituting only command payloads in the "
            "original reproduces the translated block exactly after outer zero "
            "alignment padding is ignored. Therefore text growth requires outer "
            "block realignment and absolute-offset-table updates; no per-string "
            "length or pointer field exists in these commands."
        ),
    }


def _write_json(path: Path, document: object, force: bool) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"refusing to overwrite {path}; pass --force")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _summary(container: Add01Container) -> dict[str, object]:
    return {
        "size": len(container.source),
        "sha256": hashlib.sha256(container.source).hexdigest().upper(),
        "table_size": container.table_size,
        "table_slots": len(container.pointers),
        "blocks": len(container.blocks),
        "records": sum(len(block.records) for block in container.blocks),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract_parser = subparsers.add_parser("extract")
    extract_parser.add_argument("source", type=Path)
    extract_parser.add_argument("output", type=Path)
    extract_parser.add_argument("--force", action="store_true")

    roundtrip_parser = subparsers.add_parser("roundtrip")
    roundtrip_parser.add_argument("source", type=Path)
    roundtrip_parser.add_argument("output", type=Path)
    roundtrip_parser.add_argument("--force", action="store_true")

    repack_parser = subparsers.add_parser("repack")
    repack_parser.add_argument("source", type=Path)
    repack_parser.add_argument("output", type=Path)
    repack_parser.add_argument("--replacements", type=Path, required=True)
    repack_parser.add_argument("--codebook", type=Path)
    repack_parser.add_argument("--report", type=Path)
    repack_parser.add_argument("--force", action="store_true")

    compare_parser = subparsers.add_parser("compare")
    compare_parser.add_argument("original", type=Path)
    compare_parser.add_argument("translated", type=Path)
    compare_parser.add_argument("--report", type=Path, required=True)
    compare_parser.add_argument("--force", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "extract":
        container = parse_container(args.source)
        document = {
            "container": CONTAINER_NAME,
            "source": str(args.source.resolve()),
            **_summary(container),
            "records": extract_records(container.source),
        }
        _write_json(args.output, document, args.force)
        print(json.dumps({key: value for key, value in document.items() if key != "records"}, ensure_ascii=True))
        return 0

    if args.command == "roundtrip":
        if args.output.exists() and not args.force:
            raise FileExistsError(f"refusing to overwrite {args.output}; pass --force")
        source = args.source.read_bytes()
        result = repack(source, {})
        if result != source:
            raise Add01FormatError("no-change repack is not byte-identical")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(result)
        print(json.dumps({"byte_identical": True, "sha256": hashlib.sha256(result).hexdigest().upper()}))
        return 0

    if args.command == "repack":
        if args.output.exists() and not args.force:
            raise FileExistsError(f"refusing to overwrite {args.output}; pass --force")
        replacements = _load_replacement_document(args.replacements)
        encoder = (
            make_codebook_encoder(load_codebook(args.codebook))
            if args.codebook
            else None
        )
        original = args.source.read_bytes()
        result = repack(original, replacements, encoder)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(result)
        report = {
            "source": str(args.source.resolve()),
            "source_size": len(original),
            "source_sha256": hashlib.sha256(original).hexdigest().upper(),
            "output": str(args.output.resolve()),
            "output_size": len(result),
            "output_sha256": hashlib.sha256(result).hexdigest().upper(),
            "replacement_count": len(replacements),
            "block_count": len(parse_container(result).blocks),
            "record_count": len(extract_records(result)),
            "structural_verification": "passed",
        }
        if args.report:
            _write_json(args.report, report, args.force)
        print(json.dumps(report, ensure_ascii=True))
        return 0

    if args.command == "compare":
        report = compare_reference(args.original, args.translated)
        _write_json(args.report, report, args.force)
        print(json.dumps({key: value for key, value in report.items() if key != "changed_block_details"}, ensure_ascii=True))
        return 0

    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
