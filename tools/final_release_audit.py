#!/usr/bin/env python3
"""Perform a read-only release audit of the final Japanese-base ISO."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from pathlib import Path


REPLACEMENTS = (
    "add00dat.bin",
    "add01dat.bin",
    "add02dat.bin",
    "bpilot.pak",
    "font.pak",
    "opening.bnr",
)

PATCH_MARKERS = (
    b"Super Robot Wars",
    b"English Translation",
    b"Translation v1.0",
    b"Arc Impulse",
    b"Bring Stablity",
    b"Bring Stability",
    b"Dashman",
    b"Oppai Missile",
    b"SteveO",
    b"SRW GC Korean",
    b"Korean Patch",
    b"Korean fan translation",
    b"fan translation",
    b"EN v1.0",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest().upper()


def sha256_region(path: Path, offset: int, size: int) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        handle.seek(offset)
        remaining = size
        while remaining:
            chunk = handle.read(min(8 * 1024 * 1024, remaining))
            if not chunk:
                raise ValueError(f"truncated region: {path}")
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest().upper()


def read_fst(path: Path) -> tuple[int, int, dict[str, tuple[int, int]]]:
    with path.open("rb") as handle:
        handle.seek(0x420)
        dol_offset, fst_offset, fst_size = struct.unpack(">III", handle.read(12))
        handle.seek(fst_offset)
        fst = handle.read(fst_size)
    count = struct.unpack_from(">I", fst, 8)[0]
    table_size = count * 12
    strings = fst[table_size:]
    entries: dict[str, tuple[int, int]] = {}
    for index in range(1, count):
        word0, offset, size = struct.unpack_from(">III", fst, index * 12)
        if word0 >> 24:
            continue
        name_offset = word0 & 0xFFFFFF
        end = strings.find(b"\0", name_offset)
        entries[strings[name_offset:end].decode("cp932")] = (offset, size)
    return dol_offset, fst_offset, entries


def dol_size(path: Path, offset: int) -> int:
    with path.open("rb") as handle:
        handle.seek(offset)
        header = handle.read(0x100)
    text_offsets = struct.unpack_from(">7I", header, 0x00)
    data_offsets = struct.unpack_from(">11I", header, 0x1C)
    text_sizes = struct.unpack_from(">7I", header, 0x90)
    data_sizes = struct.unpack_from(">11I", header, 0xAC)
    return max(
        offset_ + size
        for offset_, size in zip(text_offsets + data_offsets, text_sizes + data_sizes)
        if size
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--iso", type=Path, required=True)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--binary-audit", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    source_dol, source_fst, source_entries = read_fst(args.source)
    final_dol, final_fst, final_entries = read_fst(args.iso)
    failures: list[str] = []
    if source_fst != final_fst:
        failures.append("FST offset differs from Japanese retail")
    if set(source_entries) != set(final_entries):
        failures.append("FST filename set differs from Japanese retail")

    replacement_checks: dict[str, object] = {}
    marker_hits: dict[str, list[str]] = {}
    for name in REPLACEMENTS:
        offset, size = final_entries[name]
        expected = args.build / name
        embedded_hash = sha256_region(args.iso, offset, size)
        expected_hash = sha256_file(expected)
        exact = size == expected.stat().st_size and embedded_hash == expected_hash
        replacement_checks[name] = {
            "size": size,
            "sha256": embedded_hash,
            "matches_build": exact,
        }
        if not exact:
            failures.append(f"embedded replacement mismatch: {name}")
        data = expected.read_bytes().lower()
        hits = [marker.decode("ascii") for marker in PATCH_MARKERS if marker.lower() in data]
        if hits:
            marker_hits[name] = hits

    nonreplacement_failures: list[str] = []
    for name, (source_offset, source_size) in source_entries.items():
        if name in REPLACEMENTS:
            continue
        final_offset, final_size = final_entries[name]
        if (source_offset, source_size) != (final_offset, final_size):
            nonreplacement_failures.append(name)
            continue
        if sha256_region(args.source, source_offset, source_size) != sha256_region(
            args.iso, final_offset, final_size
        ):
            nonreplacement_failures.append(name)
    if nonreplacement_failures:
        failures.append("non-replacement Japanese retail files changed")

    expected_dol = args.build / "Start.dol"
    embedded_dol_size = dol_size(args.iso, final_dol)
    embedded_dol_hash = sha256_region(args.iso, final_dol, embedded_dol_size)
    expected_dol_hash = sha256_file(expected_dol)
    dol_exact = embedded_dol_size == expected_dol.stat().st_size and embedded_dol_hash == expected_dol_hash
    if not dol_exact:
        failures.append("embedded Start.dol mismatch")
    dol_data = expected_dol.read_bytes().lower()
    dol_markers = [marker.decode("ascii") for marker in PATCH_MARKERS if marker.lower() in dol_data]
    if dol_markers:
        marker_hits["Start.dol"] = dol_markers
    if marker_hits:
        failures.append("known English fan-patch marker remains")

    with args.source.open("rb") as source_handle, args.iso.open("rb") as final_handle:
        source_handle.seek(0)
        final_handle.seek(0)
        source_game_id = source_handle.read(6).decode("ascii")
        final_game_id = final_handle.read(6).decode("ascii")
    if final_game_id != "GRWJD9" or final_game_id != source_game_id:
        failures.append("unexpected game ID")

    source_bnr_offset, source_bnr_size = source_entries["opening.bnr"]
    final_bnr_offset, final_bnr_size = final_entries["opening.bnr"]
    with args.source.open("rb") as handle:
        handle.seek(source_bnr_offset)
        source_bnr = handle.read(source_bnr_size)
    with args.iso.open("rb") as handle:
        handle.seek(final_bnr_offset)
        final_bnr = handle.read(final_bnr_size)
    bnr_japanese_metadata = (
        final_bnr[:0x20] + final_bnr[0x1820:]
        == source_bnr[:0x20] + source_bnr[0x1820:]
    )
    if not bnr_japanese_metadata:
        failures.append("opening.bnr metadata differs from Japanese retail")

    binary_audit = json.loads(args.binary_audit.read_text(encoding="utf-8"))
    binary_ok = (
        binary_audit.get("status") == "pass"
        and binary_audit.get("success") is True
        and binary_audit.get("verification_failure_count") == 0
        and binary_audit.get("decoded_final_japanese_residual_count") == 0
        and not binary_audit.get("dol_unapproved_code_byte_changes")
    )
    if not binary_ok:
        failures.append("binary text audit failed")

    report = {
        "schema": "srw-gc-korean-final-release-audit-v1",
        "status": "pass" if not failures else "fail",
        "game_id": final_game_id,
        "source_sha256": sha256_file(args.source),
        "final_iso_sha256": sha256_file(args.iso),
        "final_iso_size": args.iso.stat().st_size,
        "replacement_payloads": replacement_checks,
        "nonreplacement_file_count": len(source_entries) - len(REPLACEMENTS),
        "nonreplacement_failures": nonreplacement_failures,
        "start_dol": {
            "size": embedded_dol_size,
            "sha256": embedded_dol_hash,
            "matches_build": dol_exact,
        },
        "known_english_patch_marker_hits": marker_hits,
        "opening_bnr_japanese_metadata": bnr_japanese_metadata,
        "binary_text_audit": {
            "total_verified_payloads": binary_audit.get("replacement_counts", {}).get(
                "total_verified_payloads"
            ),
            "failure_count": binary_audit.get("verification_failure_count"),
            "japanese_residual_count": binary_audit.get(
                "decoded_final_japanese_residual_count"
            ),
            "dol_approved_code_byte_change_count": binary_audit.get(
                "dol_approved_code_byte_change_count"
            ),
            "dol_unapproved_code_byte_change_count": len(
                binary_audit.get("dol_unapproved_code_byte_changes", [])
            ),
        },
        "failures": failures,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
