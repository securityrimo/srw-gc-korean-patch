#!/usr/bin/env python3
"""Install gender-aware canonical Korean protagonist names into the JP DOL.

Male   : 아카츠키 아키미
Female : 아카츠키 아케미

The Japanese executable stores the original defaults in 8/12-byte C-string
slots, which cannot hold the canonical Korean full name safely.  This patch keeps
the DOL section topology unchanged, places strings/tables in a guarded data cave,
places four small PPC helpers in a guarded executable cave, and redirects only the
verified default-name initialization/comparison/name-entry paths.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path
from typing import Callable


MALE_FULL = "\uc544\uce74\uce20\ud0a4 \uc544\ud0a4\ubbf8"
FEMALE_FULL = "\uc544\uce74\uce20\ud0a4 \uc544\ucf00\ubbf8"
SURNAME = "\uc544\uce74\uce20\ud0a4"
MALE_GIVEN = "\uc544\ud0a4\ubbf8"
FEMALE_GIVEN = "\uc544\ucf00\ubbf8"
MALE_HONORIFIC = "\uc544\ud0a4\ubbf8 \uc528"


CODE_CAVE_START = 0x80004BE0
CODE_CAVE_END = 0x800050AC
WRAPPER_ADDR = 0x80004BE0
APPLY_ADDR = 0x80004C20
GET_TABLE_ADDR = 0x80004CA0
GET_GIVEN_ADDR = 0x80004CE0

DATA_CAVE_ADDR = 0x802F4000
DATA_CAVE_FILE_OFFSET = 0x002F1000
DATA_CAVE_SIZE = 0x178
DATA_USED_SIZE = 0xD0

SURNAME_ADDR = DATA_CAVE_ADDR + 0x00
MALE_GIVEN_ADDR = DATA_CAVE_ADDR + 0x10
FEMALE_GIVEN_ADDR = DATA_CAVE_ADDR + 0x20
MALE_FULL_ADDR = DATA_CAVE_ADDR + 0x30
FEMALE_FULL_ADDR = DATA_CAVE_ADDR + 0x40
MALE_DESCRIPTOR_ADDR = DATA_CAVE_ADDR + 0x70
FEMALE_DESCRIPTOR_ADDR = DATA_CAVE_ADDR + 0x80
MALE_TABLE_ADDR = DATA_CAVE_ADDR + 0x90
FEMALE_TABLE_ADDR = DATA_CAVE_ADDR + 0xB0

GLOBAL_BASE = 0x80337CC4
GENDER_WORD_ADDR = GLOBAL_BASE + 0x180

ORIGINAL_POINTER_TABLE_FILE_OFFSET = 0x002EAAB8
SPACE8_ADDR = 0x802C7D10
SPACE6_ADDR = 0x802C7D24


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def d_form(opcode: int, rt: int, ra: int, immediate: int) -> int:
    return (
        ((opcode & 0x3F) << 26)
        | ((rt & 0x1F) << 21)
        | ((ra & 0x1F) << 16)
        | (immediate & 0xFFFF)
    )


def lis(rt: int, immediate: int) -> int:
    return d_form(15, rt, 0, immediate)


def addi(rt: int, ra: int, immediate: int) -> int:
    return d_form(14, rt, ra, immediate)


def lwz(rt: int, ra: int, immediate: int) -> int:
    return d_form(32, rt, ra, immediate)


def stw(rs: int, ra: int, immediate: int) -> int:
    return d_form(36, rs, ra, immediate)


def stwu(rs: int, ra: int, immediate: int) -> int:
    return d_form(37, rs, ra, immediate)


def andi_dot(ra: int, rs: int, immediate: int) -> int:
    return d_form(28, rs, ra, immediate)


def branch(source: int, target: int, *, link: bool = False) -> int:
    displacement = target - source
    if displacement % 4 or not -(1 << 25) <= displacement < (1 << 25):
        raise ValueError(f"invalid PPC branch: {source:08X} -> {target:08X}")
    return (18 << 26) | (displacement & 0x03FFFFFC) | int(link)


def beq(source: int, target: int) -> int:
    displacement = target - source
    if displacement % 4 or not -0x8000 <= displacement < 0x8000:
        raise ValueError(f"invalid PPC conditional branch: {source:08X} -> {target:08X}")
    return (16 << 26) | (12 << 21) | (2 << 16) | (displacement & 0xFFFC)


MFLR_R0 = 0x7C0802A6
MTLR_R0 = 0x7C0803A6
BLR = 0x4E800020
NOP = 0x60000000


def pack_words(words: list[int]) -> bytes:
    return b"".join(struct.pack(">I", word) for word in words)


def emit_wrapper() -> bytes:
    words: list[int] = []
    pc = WRAPPER_ADDR

    def put(word: int) -> None:
        nonlocal pc
        words.append(word)
        pc += 4

    put(stwu(1, 1, -0x10))
    put(MFLR_R0)
    put(stw(0, 1, 0x14))
    # Reproduce the overwritten first instruction at 0x8001BAB8.
    put(lis(3, 0x8033))
    put(branch(pc, 0x8001BABC, link=True))
    put(branch(pc, APPLY_ADDR, link=True))
    put(lwz(0, 1, 0x14))
    put(MTLR_R0)
    put(addi(1, 1, 0x10))
    put(BLR)
    return pack_words(words)


def emit_apply() -> bytes:
    words: list[int] = []
    pc = APPLY_ADDR

    def put(word: int) -> None:
        nonlocal pc
        words.append(word)
        pc += 4

    put(stwu(1, 1, -0x20))
    put(MFLR_R0)
    put(stw(0, 1, 0x24))
    put(stw(31, 1, 0x1C))
    put(lis(31, 0x8033))
    put(lwz(0, 31, 0x7E44))
    put(andi_dot(0, 0, 1))
    put(lis(31, 0x802F))
    male_label = APPLY_ADDR + 0x2C
    selected_label = APPLY_ADDR + 0x30
    put(beq(pc, male_label))
    put(addi(31, 31, 0x4080))
    put(branch(pc, selected_label))
    put(addi(31, 31, 0x4070))
    put(lwz(3, 31, 0x00))
    put(branch(pc, 0x80035D60, link=True))
    put(lwz(3, 31, 0x04))
    put(branch(pc, 0x80035D1C, link=True))
    put(lwz(3, 31, 0x08))
    put(branch(pc, 0x80035C8C, link=True))
    put(lwz(3, 31, 0x0C))
    put(branch(pc, 0x80035CD4, link=True))
    put(lwz(31, 1, 0x1C))
    put(lwz(0, 1, 0x24))
    put(MTLR_R0)
    put(addi(1, 1, 0x20))
    put(BLR)
    return pack_words(words)


def emit_selector(destination_register: int, male_address: int, female_address: int, start: int) -> bytes:
    # r12 is used as scratch.  This is essential for GET_TABLE: r0 still holds
    # the caller's LR between mflr at 0x800F05F4 and stw at 0x800F0600.
    words: list[int] = []
    pc = start

    def put(word: int) -> None:
        nonlocal pc
        words.append(word)
        pc += 4

    put(lis(destination_register, 0x8033))
    put(lwz(12, destination_register, 0x7E44))
    put(andi_dot(12, 12, 1))
    put(lis(destination_register, 0x802F))
    male_label = start + 0x1C
    put(beq(pc, male_label))
    put(addi(destination_register, destination_register, female_address & 0xFFFF))
    put(BLR)
    put(addi(destination_register, destination_register, male_address & 0xFFFF))
    put(BLR)
    return pack_words(words)


def build_code_blob() -> tuple[bytes, list[dict[str, object]]]:
    functions = [
        ("default_reset_wrapper", WRAPPER_ADDR, emit_wrapper()),
        ("apply_gender_names", APPLY_ADDR, emit_apply()),
        (
            "select_name_entry_table_r4",
            GET_TABLE_ADDR,
            emit_selector(4, MALE_TABLE_ADDR, FEMALE_TABLE_ADDR, GET_TABLE_ADDR),
        ),
        (
            "select_default_given_r6",
            GET_GIVEN_ADDR,
            emit_selector(6, MALE_GIVEN_ADDR, FEMALE_GIVEN_ADDR, GET_GIVEN_ADDR),
        ),
    ]
    used_end = max(address + len(payload) for _, address, payload in functions)
    blob = bytearray(used_end - CODE_CAVE_START)
    report: list[dict[str, object]] = []
    for name, address, payload in functions:
        offset = address - CODE_CAVE_START
        blob[offset : offset + len(payload)] = payload
        report.append(
            {
                "name": name,
                "address": address,
                "address_hex": f"0x{address:08X}",
                "size": len(payload),
                "end_hex": f"0x{address + len(payload):08X}",
                "words": [
                    f"0x{struct.unpack('>I', payload[i:i+4])[0]:08X}"
                    for i in range(0, len(payload), 4)
                ],
            }
        )
    return bytes(blob), report


def write_cstring(pool: bytearray, offset: int, size: int, text: str, encoder: Callable[[str], bytes]) -> dict[str, object]:
    encoded = encoder(text)
    if b"\x00" in encoded or len(encoded) + 1 > size:
        raise RuntimeError(f"invalid pool string {text!r}: {len(encoded)} bytes for slot {size}")
    payload = encoded + b"\x00" + bytes(size - len(encoded) - 1)
    pool[offset : offset + size] = payload
    return {
        "text": text,
        "address": DATA_CAVE_ADDR + offset,
        "address_hex": f"0x{DATA_CAVE_ADDR + offset:08X}",
        "slot_size": size,
        "encoded_bytes": len(encoded),
        "encoded_hex": encoded.hex().upper(),
        "slot_hex": payload.hex().upper(),
        "nul_terminated": payload[len(encoded)] == 0,
    }


def build_data_pool(encoder: Callable[[str], bytes]) -> tuple[bytes, dict[str, object]]:
    pool = bytearray(DATA_USED_SIZE)
    strings = {
        "surname": write_cstring(pool, 0x00, 0x10, SURNAME, encoder),
        "male_given": write_cstring(pool, 0x10, 0x10, MALE_GIVEN, encoder),
        "female_given": write_cstring(pool, 0x20, 0x10, FEMALE_GIVEN, encoder),
        "male_full": write_cstring(pool, 0x30, 0x10, MALE_FULL, encoder),
        "female_full": write_cstring(pool, 0x40, 0x10, FEMALE_FULL, encoder),
    }
    descriptors = {
        "male": [SURNAME_ADDR, MALE_GIVEN_ADDR, MALE_FULL_ADDR, MALE_GIVEN_ADDR],
        "female": [SURNAME_ADDR, FEMALE_GIVEN_ADDR, FEMALE_FULL_ADDR, FEMALE_GIVEN_ADDR],
    }
    struct.pack_into(">4I", pool, 0x70, *descriptors["male"])
    struct.pack_into(">4I", pool, 0x80, *descriptors["female"])
    tables = {
        "male": [
            SURNAME_ADDR,
            MALE_GIVEN_ADDR,
            MALE_GIVEN_ADDR,
            MALE_GIVEN_ADDR,
            SPACE8_ADDR,
            SPACE8_ADDR,
            SPACE6_ADDR,
            SPACE6_ADDR,
        ],
        "female": [
            SURNAME_ADDR,
            FEMALE_GIVEN_ADDR,
            FEMALE_GIVEN_ADDR,
            FEMALE_GIVEN_ADDR,
            SPACE8_ADDR,
            SPACE8_ADDR,
            SPACE6_ADDR,
            SPACE6_ADDR,
        ],
    }
    struct.pack_into(">8I", pool, 0x90, *tables["male"])
    struct.pack_into(">8I", pool, 0xB0, *tables["female"])
    return bytes(pool), {
        "address": DATA_CAVE_ADDR,
        "address_hex": f"0x{DATA_CAVE_ADDR:08X}",
        "file_offset_hex": f"0x{DATA_CAVE_FILE_OFFSET:08X}",
        "available_bytes": DATA_CAVE_SIZE,
        "used_bytes": len(pool),
        "strings": strings,
        "descriptors": {
            key: [f"0x{value:08X}" for value in values]
            for key, values in descriptors.items()
        },
        "name_entry_tables": {
            key: [f"0x{value:08X}" for value in values]
            for key, values in tables.items()
        },
    }


def find_all(data: bytes, needle: bytes) -> list[int]:
    result: list[int] = []
    start = 0
    while True:
        offset = data.find(needle, start)
        if offset < 0:
            return result
        result.append(offset)
        start = offset + 1


def apply_patch(
    source: bytes,
    original: bytes,
    encoder: Callable[[str], bytes],
    dol_tools,
) -> tuple[bytes, dict[str, object]]:
    if len(source) != len(original):
        raise ValueError("input/original DOL size mismatch")
    output = bytearray(source)
    allowed: set[int] = set()
    changes: list[dict[str, object]] = []

    def file_offset(address: int) -> int:
        result = dol_tools.dol_address_to_offset(original, address)
        if result is None:
            raise RuntimeError(f"address not mapped in DOL: 0x{address:08X}")
        return result

    def write_guarded(address: int, expected_hex: str, payload: bytes, label: str) -> None:
        offset = file_offset(address)
        expected = bytes.fromhex(expected_hex)
        if original[offset : offset + len(expected)] != expected:
            raise RuntimeError(f"original instruction guard failed: {label}")
        if source[offset : offset + len(expected)] != expected:
            raise RuntimeError(f"input instruction guard failed: {label}")
        if len(payload) != len(expected):
            raise RuntimeError(f"patch length mismatch: {label}")
        output[offset : offset + len(payload)] = payload
        allowed.update(range(offset, offset + len(payload)))
        changes.append(
            {
                "kind": "code_redirect",
                "label": label,
                "address_hex": f"0x{address:08X}",
                "file_offset_hex": f"0x{offset:08X}",
                "before_hex": expected.hex().upper(),
                "after_hex": payload.hex().upper(),
            }
        )

    code_blob, code_functions = build_code_blob()
    code_cave_offset = file_offset(CODE_CAVE_START)
    if original[code_cave_offset : file_offset(CODE_CAVE_END)] != bytes(CODE_CAVE_END - CODE_CAVE_START):
        raise RuntimeError("original executable cave is not all zero")
    if source[code_cave_offset : code_cave_offset + len(code_blob)] != bytes(len(code_blob)):
        raise RuntimeError("input executable cave is not all zero")
    output[code_cave_offset : code_cave_offset + len(code_blob)] = code_blob
    allowed.update(range(code_cave_offset, code_cave_offset + len(code_blob)))
    changes.append(
        {
            "kind": "code_cave",
            "label": "canonical protagonist-name helpers",
            "address_hex": f"0x{CODE_CAVE_START:08X}",
            "file_offset_hex": f"0x{code_cave_offset:08X}",
            "size": len(code_blob),
        }
    )

    write_guarded(
        0x8001BAB8,
        "3C608033",
        pack_words([branch(0x8001BAB8, WRAPPER_ADDR)]),
        "default reset entry -> gender-aware wrapper",
    )
    write_guarded(
        0x800F05F8,
        "3C80802F",
        pack_words([branch(0x800F05F8, GET_TABLE_ADDR, link=True)]),
        "name-entry init -> select male/female table",
    )
    write_guarded(
        0x800F0608,
        "3BA4DAB8",
        pack_words([addi(29, 4, 0)]),
        "name-entry table result r4 -> r29",
    )
    write_guarded(
        0x8002CB64,
        "38C28128",
        pack_words([branch(0x8002CB64, GET_GIVEN_ADDR, link=True)]),
        "default-name comparison 1 -> gender-aware given name",
    )
    write_guarded(
        0x8002CBD0,
        "38C28128",
        pack_words([branch(0x8002CBD0, GET_GIVEN_ADDR, link=True)]),
        "default-name comparison 2 -> gender-aware given name",
    )
    ed_original = "386292104BF48439386292184BF483ED3C60802C38637D944BF48399"
    ed_payload = pack_words(
        [branch(0x800ED924, APPLY_ADDR, link=True)] + [NOP] * 6
    )
    write_guarded(
        0x800ED924,
        ed_original,
        ed_payload,
        "secondary initializer -> canonical gender-aware names",
    )

    data_pool, data_pool_report = build_data_pool(encoder)
    if original[DATA_CAVE_FILE_OFFSET : DATA_CAVE_FILE_OFFSET + DATA_CAVE_SIZE] != bytes(DATA_CAVE_SIZE):
        raise RuntimeError("original data cave is not all zero")
    if source[DATA_CAVE_FILE_OFFSET : DATA_CAVE_FILE_OFFSET + len(data_pool)] != bytes(len(data_pool)):
        raise RuntimeError("input data cave is not all zero")
    output[DATA_CAVE_FILE_OFFSET : DATA_CAVE_FILE_OFFSET + len(data_pool)] = data_pool
    allowed.update(range(DATA_CAVE_FILE_OFFSET, DATA_CAVE_FILE_OFFSET + len(data_pool)))
    changes.append(
        {
            "kind": "data_cave",
            "label": "canonical strings/descriptors/name-entry tables",
            "address_hex": f"0x{DATA_CAVE_ADDR:08X}",
            "file_offset_hex": f"0x{DATA_CAVE_FILE_OFFSET:08X}",
            "size": len(data_pool),
        }
    )

    # Preserve the original pointer-table topology but direct its four name fields
    # to the canonical male defaults. F05F0 dynamically selects the female table.
    original_pointer_guard = bytes.fromhex(
        "80405CF080405CF880405CF880405D00"
        "802C7D10802C7D10802C7D24802C7D24"
    )
    if original[
        ORIGINAL_POINTER_TABLE_FILE_OFFSET : ORIGINAL_POINTER_TABLE_FILE_OFFSET + 32
    ] != original_pointer_guard:
        raise RuntimeError("Japanese name-entry pointer-table guard failed")
    old_pointer_words = list(
        struct.unpack(
            ">8I",
            source[
                ORIGINAL_POINTER_TABLE_FILE_OFFSET : ORIGINAL_POINTER_TABLE_FILE_OFFSET + 32
            ],
        )
    )
    replacement_pointer_words = [
        SURNAME_ADDR,
        MALE_GIVEN_ADDR,
        MALE_GIVEN_ADDR,
        MALE_GIVEN_ADDR,
        *old_pointer_words[4:8],
    ]
    if old_pointer_words[4:8] != [SPACE8_ADDR, SPACE8_ADDR, SPACE6_ADDR, SPACE6_ADDR]:
        raise RuntimeError("name-entry spacing-pointer topology changed")
    pointer_payload = struct.pack(">8I", *replacement_pointer_words)
    output[
        ORIGINAL_POINTER_TABLE_FILE_OFFSET : ORIGINAL_POINTER_TABLE_FILE_OFFSET + 32
    ] = pointer_payload
    allowed.update(
        range(ORIGINAL_POINTER_TABLE_FILE_OFFSET, ORIGINAL_POINTER_TABLE_FILE_OFFSET + 32)
    )
    changes.append(
        {
            "kind": "data_pointer_table",
            "label": "existing Japanese name-entry table -> canonical male defaults",
            "file_offset_hex": f"0x{ORIGINAL_POINTER_TABLE_FILE_OFFSET:08X}",
            "before_words": [f"0x{value:08X}" for value in old_pointer_words],
            "after_words": [f"0x{value:08X}" for value in replacement_pointer_words],
        }
    )

    # Canonicalize the old relocated/default pointer targets before orphaning them.
    # This prevents stale 秋水/シュスイ payloads from surviving in the translated pool.
    old_target_report: list[dict[str, object]] = []
    target_specs = [
        ("surname", old_pointer_words[0], SURNAME, 0x10),
        ("given", old_pointer_words[1], MALE_GIVEN, 0x08),
        ("reading", old_pointer_words[3], MALE_GIVEN, 0x08),
    ]
    for label, address, text, maximum in target_specs:
        offset = dol_tools.dol_address_to_offset(original, address)
        if offset is None:
            raise RuntimeError(f"old {label} pointer is outside DOL: 0x{address:08X}")
        encoded = encoder(text)
        replacement = encoded + b"\x00"
        original_target_address = struct.unpack_from(
            ">I", original, ORIGINAL_POINTER_TABLE_FILE_OFFSET + {"surname": 0, "given": 4, "reading": 12}[label]
        )[0]
        is_original_fixed_target = address == original_target_address
        if len(replacement) > maximum:
            raise RuntimeError(f"old {label} target replacement too long")
        if is_original_fixed_target and label == "surname" and len(replacement) > 8:
            # The original surname slot is only 8 bytes. It is now unreferenced;
            # clear it rather than introducing an unterminated string.
            replacement = bytes(8)
            write_size = 8
        else:
            write_size = max(len(replacement), 8 if is_original_fixed_target else len(replacement))
            replacement = replacement + bytes(write_size - len(replacement))
        before = bytes(output[offset : offset + write_size])
        output[offset : offset + write_size] = replacement
        allowed.update(range(offset, offset + write_size))
        old_target_report.append(
            {
                "role": label,
                "old_address_hex": f"0x{address:08X}",
                "file_offset_hex": f"0x{offset:08X}",
                "was_original_fixed_target": is_original_fixed_target,
                "before_hex": before.hex().upper(),
                "after_hex": replacement.hex().upper(),
                "final_text": "" if not any(replacement) else text,
            }
        )

    # Retire the other unpointed fixed defaults.  Every known DOL consumer is now
    # redirected; short male fallbacks remain only where an 8/12-byte C string is safe.
    fixed_slots = [
        (0x002BAD2C, 12, bytes(12), "legacy full-name slot retired"),
        (
            0x002C4D94,
            12,
            encoder(MALE_HONORIFIC) + b"\x00" + bytes(12 - len(encoder(MALE_HONORIFIC)) - 1),
            "legacy honorific slot canonicalized",
        ),
        (0x0030CAE0, 8, bytes(8), "legacy 8-byte surname slot retired"),
        (
            0x0030CAE8,
            8,
            encoder(MALE_GIVEN) + b"\x00" + bytes(8 - len(encoder(MALE_GIVEN)) - 1),
            "legacy given-name fallback canonicalized",
        ),
        (
            0x0030CAF0,
            8,
            encoder(MALE_GIVEN) + b"\x00" + bytes(8 - len(encoder(MALE_GIVEN)) - 1),
            "legacy reading fallback canonicalized",
        ),
    ]
    fixed_slot_report: list[dict[str, object]] = []
    for offset, size, payload, label in fixed_slots:
        if len(payload) != size:
            raise RuntimeError(f"fixed slot payload size mismatch: {label}")
        original_slice = original[offset : offset + size]
        input_slice = source[offset : offset + size]
        # Exact original guards are recorded even when the current input contains
        # the earlier v11 short-name patch.
        if not original_slice:
            raise RuntimeError(f"fixed slot outside DOL: {label}")
        output[offset : offset + size] = payload
        allowed.update(range(offset, offset + size))
        fixed_slot_report.append(
            {
                "label": label,
                "file_offset_hex": f"0x{offset:08X}",
                "size": size,
                "japanese_original_guard_hex": original_slice.hex().upper(),
                "input_hex": input_slice.hex().upper(),
                "output_hex": payload.hex().upper(),
                "nul_present": 0 in payload,
            }
        )

    result = bytes(output)
    changed_offsets = [
        offset
        for offset, (before, after) in enumerate(zip(source, result))
        if before != after
    ]
    unexpected = [offset for offset in changed_offsets if offset not in allowed]
    if unexpected:
        raise RuntimeError(f"unexpected changed offsets: {unexpected[:20]}")

    sections_before = [vars(section) for section in dol_tools.dol_sections(source)]
    sections_after = [vars(section) for section in dol_tools.dol_sections(result)]
    if sections_before != sections_after or source[:0x100] != result[:0x100]:
        raise RuntimeError("DOL header/section topology changed")

    text_ranges = [
        range(section.file_offset, section.file_offset + section.size)
        for section in dol_tools.dol_sections(source)
        if section.kind == "text"
    ]
    text_changed = [
        offset for offset in changed_offsets if any(offset in span for span in text_ranges)
    ]
    permitted_text = set(range(code_cave_offset, code_cave_offset + len(code_blob)))
    for record in changes:
        if record["kind"] != "code_redirect":
            continue
        offset = int(str(record["file_offset_hex"]), 16)
        permitted_text.update(range(offset, offset + len(bytes.fromhex(str(record["after_hex"])))))
    unexpected_text = [offset for offset in text_changed if offset not in permitted_text]
    if unexpected_text:
        raise RuntimeError(f"unexpected executable changes: {unexpected_text[:20]}")

    # Exact-name residual audit in the DOL.  光珠 is absent in the retail DOL, but
    # include it explicitly so the evidence is reproducible.
    source_patterns = {
        "赤月": "\u8d64\u6708".encode("cp932"),
        "秋水": "\u79cb\u6c34".encode("cp932"),
        "光珠": "\u5149\u73e0".encode("cp932"),
        "アキミ": "\u30a2\u30ad\u30df".encode("cp932"),
    }
    residual_audit = {
        key: {
            "original_offsets": [f"0x{offset:08X}" for offset in find_all(original, pattern)],
            "input_offsets": [f"0x{offset:08X}" for offset in find_all(source, pattern)],
            "output_offsets": [f"0x{offset:08X}" for offset in find_all(result, pattern)],
        }
        for key, pattern in source_patterns.items()
    }

    report = {
        "status": "pass",
        "runtime_base": "Japanese retail DOL topology",
        "canonical_names": {
            "gender_bit_0": MALE_FULL,
            "gender_bit_1": FEMALE_FULL,
            "surname": SURNAME,
            "male_given": MALE_GIVEN,
            "female_given": FEMALE_GIVEN,
        },
        "gender_flow": {
            "global_base_hex": f"0x{GLOBAL_BASE:08X}",
            "gender_word_hex": f"0x{GENDER_WORD_ADDR:08X}",
            "getter": "0x80035800 (bit 0)",
            "setter": "0x80035814",
            "two_choice_commit_call": "0x800ED780",
            "mapping": {"0": "male", "1": "female"},
        },
        "destination_buffers": {
            "surname": {"address": "0x80337CC4", "bytes_until_next": 18},
            "given": {"address": "0x80337CD6", "bytes_until_next": 18},
            "reading_or_alias": {"address": "0x80337CE8", "bytes_until_next": 14},
            "full_name": {"address": "0x80337CF6", "verified_payload_bytes": 15},
            "setters_write_two_zero_bytes": True,
            "canonical_payloads_fit": True,
        },
        "code_cave": {
            "start_hex": f"0x{CODE_CAVE_START:08X}",
            "end_hex": f"0x{CODE_CAVE_END:08X}",
            "available_bytes": CODE_CAVE_END - CODE_CAVE_START,
            "used_bytes": len(code_blob),
            "original_all_zero": True,
            "input_used_range_all_zero": True,
            "preexisting_branch_targets_in_cave": 0,
            "preexisting_literal_pointers_into_cave": 0,
            "functions": code_functions,
        },
        "data_pool": data_pool_report,
        "pointer_table": {
            "file_offset_hex": f"0x{ORIGINAL_POINTER_TABLE_FILE_OFFSET:08X}",
            "old_words": [f"0x{value:08X}" for value in old_pointer_words],
            "new_words": [f"0x{value:08X}" for value in replacement_pointer_words],
            "spacing_words_preserved": True,
            "old_targets_canonicalized": old_target_report,
        },
        "fixed_slots": fixed_slot_report,
        "code_changes": changes,
        "code_section_impact": {
            "changed_bytes": len(text_changed),
            "unexpected_changed_bytes": len(unexpected_text),
            "all_other_text_code_bytes_identical": not unexpected_text,
            "header_and_section_table_identical": True,
        },
        "whole_file_impact": {
            "input_size": len(source),
            "output_size": len(result),
            "size_identical": len(source) == len(result),
            "changed_bytes": len(changed_offsets),
            "unexpected_changed_offsets": unexpected,
        },
        "original_name_occurrences": residual_audit,
        "female_original_static_name": {
            "text": "光珠",
            "cp932_hex": source_patterns["光珠"].hex().upper(),
            "retail_dol_occurrences": len(find_all(original, source_patterns["光珠"])),
            "finding": "not stored in the retail DOL default-name tables",
        },
    }
    return result, report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dol", type=Path, required=True)
    parser.add_argument("--original-dol", type=Path, required=True)
    parser.add_argument("--codebook", type=Path, required=True)
    parser.add_argument("--tools-root", type=Path, required=True)
    parser.add_argument("--name-grid", type=Path, required=True)
    parser.add_argument("--output-dol", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    sys.path.insert(0, str(args.tools_root.resolve()))
    import add02_dol_tools  # type: ignore

    source = args.input_dol.read_bytes()
    original = args.original_dol.read_bytes()
    codebook = add02_dol_tools.load_codebook(args.codebook)
    encoder = add02_dol_tools.codebook_encoder(codebook)
    output, report = apply_patch(source, original, encoder, add02_dol_tools)

    required = sorted(set(SURNAME + MALE_GIVEN + FEMALE_GIVEN))
    name_grid_doc = json.loads(args.name_grid.read_text(encoding="utf-8"))
    name_grid = "".join(name_grid_doc["replacements"].values())
    report["name_grid"] = {
        "row_count": len(name_grid_doc["replacements"]),
        "required_characters": required,
        "runtime_codebook_codes": {
            character: f"0x{codebook[character]:04X}" for character in required
        },
        "occurrences_in_235_row_grid": {
            character: name_grid.count(character) for character in required
        },
        "all_required_characters_available": all(
            character in codebook and character in name_grid for character in required
        ),
    }
    if not report["name_grid"]["all_required_characters_available"]:
        raise RuntimeError("canonical-name character missing from codebook/name grid")

    report.update(
        {
            "input_dol": str(args.input_dol.resolve()),
            "input_sha256": sha256(source),
            "original_dol": str(args.original_dol.resolve()),
            "original_sha256": sha256(original),
            "codebook": str(args.codebook.resolve()),
            "codebook_sha256": sha256(args.codebook.read_bytes()),
            "output_dol": str(args.output_dol.resolve()),
            "output_sha256": sha256(output),
        }
    )

    args.output_dol.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.output_dol.write_bytes(output)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "canonical_names": report["canonical_names"],
                "input_sha256": report["input_sha256"],
                "output_sha256": report["output_sha256"],
                "code_changed_bytes": report["code_section_impact"]["changed_bytes"],
                "whole_file_changed_bytes": report["whole_file_impact"]["changed_bytes"],
                "unexpected_changes": report["whole_file_impact"]["unexpected_changed_offsets"],
                "name_grid_ready": report["name_grid"]["all_required_characters_available"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
