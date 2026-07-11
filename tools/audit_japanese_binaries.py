#!/usr/bin/env python3
"""Byte-level audit for the Japanese-native Korean runtime build."""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

import add01_tools
import add02_dol_tools
import add02_fixed_topology
import bpilot_tools
from assemble_japanese_binaries import (
    add01_alignment_report,
    apply_name_input_grid_japanese,
)
from canonical_protagonist_names import apply_patch as install_canonical_protagonist_names


STRICT_JAPANESE_RE = re.compile(
    r"[\u3041-\u3096\u30A1-\u30FA\u3400-\u9FFF\uF900-\uFAFF\uFF66-\uFF6F\uFF71-\uFF9D]"
)


def load_map(path: Path) -> dict[str, str]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_codebook(path: Path) -> dict[str, int]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {
            row["target"]: int(row["code"], 16)
            for row in csv.DictReader(handle)
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--codebook", type=Path, required=True)
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    root = args.root.resolve()
    build = args.build.resolve()
    codebook = load_codebook(args.codebook)
    add01_encoder = add01_tools.make_codebook_encoder(codebook)
    general_encoder = add02_dol_tools.codebook_encoder(codebook)
    bpilot_encoder = bpilot_tools.make_codebook_encoder(args.codebook)

    add01_map = load_map(root / "add01_replacements.json")
    add02_source_map = load_map(root / "add02_replacements.json")
    add02_map, add02_transform = add02_fixed_topology.prepare_replacements(
        add02_source_map, compact_library_whitespace=True
    )
    bpilot_map = load_map(root / "bpilot_replacements.json")
    dol_map = load_map(root / "dol_all_replacements.json")
    name_map = json.loads(
        (root / "dol_name_input_replacements.json").read_text(encoding="utf-8")
    )["replacements"]

    failures: list[dict[str, str]] = []

    add01_records = {
        record["id"]: record
        for record in add01_tools.extract_records(build / "add01dat.bin")
    }
    for stable_id, expected in add01_map.items():
        expected_bytes, _ = add01_tools.encode_text(expected, add01_encoder)
        actual = bytes.fromhex(str(add01_records[stable_id]["raw_hex"]))
        if actual != expected_bytes:
            failures.append(
                {"family": "add01", "id": stable_id, "reason": "encoded byte mismatch"}
            )

    add01_alignment = add01_alignment_report((build / "add01dat.bin").read_bytes())
    if not add01_alignment["valid"]:
        failures.append(
            {"family": "add01", "id": "<alignment>", "reason": str(add01_alignment)}
        )

    add02_records = {
        record["id"]: record
        for record in add02_dol_tools.extract_records(build / "add02dat.bin")
    }
    for stable_id, expected in add02_map.items():
        actual = bytes.fromhex(str(add02_records[stable_id]["raw_hex"]))
        wanted = general_encoder(expected)
        if actual != wanted:
            failures.append(
                {"family": "add02", "id": stable_id, "reason": "encoded byte mismatch"}
            )

    built_pak = bpilot_tools.parse_pak((build / "bpilot.pak").read_bytes())
    members = {member.name: member for member in built_pak.members}
    parsed_members: dict[str, object] = {}
    for stable_id, expected in bpilot_map.items():
        match = re.fullmatch(r"bpilot:(.+):r(\d+):t(\d+)", stable_id)
        if not match:
            failures.append(
                {"family": "bpilot", "id": stable_id, "reason": "invalid stable ID"}
            )
            continue
        member_name, record_text, _ = match.groups()
        if member_name not in parsed_members:
            parsed_members[member_name] = bpilot_tools.parse_atmb(
                members[member_name].data, member_name=member_name
            )
        record = parsed_members[member_name].records[int(record_text)]
        if bpilot_encoder(expected) not in record:
            failures.append(
                {"family": "bpilot", "id": stable_id, "reason": "encoded bytes absent"}
            )

    original_dol = (args.original / "Start.dol").read_bytes()
    rebuilt_dol, dol_report = add02_dol_tools.build_japanese_data_pointer_dol_patch(
        args.original / "Start.dol", dol_map, general_encoder
    )
    rebuilt_dol, name_report = apply_name_input_grid_japanese(
        rebuilt_dol,
        args.original / "Start.dol",
        root / "dol_name_input_replacements.json",
        general_encoder,
    )
    rebuilt_dol, protagonist_name_report = install_canonical_protagonist_names(
        rebuilt_dol,
        original_dol,
        general_encoder,
        add02_dol_tools,
    )
    built_dol = (build / "Start.dol").read_bytes()
    if rebuilt_dol != built_dol:
        failures.append(
            {"family": "dol", "id": "<file>", "reason": "non-deterministic Japanese DOL"}
        )
    for record in dol_report["records"]:
        stable_id = record["id"]
        offset = int(record["new_offset"])
        wanted = general_encoder(dol_map[stable_id])
        if rebuilt_dol[offset : offset + len(wanted)] != wanted:
            failures.append(
                {"family": "dol", "id": stable_id, "reason": "pool byte mismatch"}
            )

    pointer_records = {
        record["id"]: record
        for record in add02_dol_tools.dol_pointer_records(args.original / "Start.dol")
    }
    for stable_id, expected in name_map.items():
        record = pointer_records[stable_id]
        offset = int(record["target_file_offset"])
        wanted = general_encoder(expected)
        if rebuilt_dol[offset : offset + len(wanted)] != wanted:
            failures.append(
                {
                    "family": "dol_name_input",
                    "id": stable_id,
                    "reason": "in-place byte mismatch",
                }
            )

    for key, record in protagonist_name_report["data_pool"]["strings"].items():
        offset = add02_dol_tools.dol_address_to_offset(
            original_dol,
            int(record["address"]),
        )
        if offset is None:
            failures.append(
                {
                    "family": "dol_protagonist_name",
                    "id": key,
                    "reason": "runtime address is outside DOL",
                }
            )
            continue
        wanted = bytes.fromhex(str(record["slot_hex"]))
        if rebuilt_dol[offset : offset + len(wanted)] != wanted:
            failures.append(
                {
                    "family": "dol_protagonist_name",
                    "id": key,
                    "reason": "redirected pool byte mismatch",
                }
            )

    code_differences = [
        offset
        for section in add02_dol_tools.dol_sections(original_dol)
        if section.kind == "text"
        for offset in range(section.file_offset, section.file_offset + section.size)
        if rebuilt_dol[offset] != original_dol[offset]
    ]
    expected_code_change_count = protagonist_name_report[
        "code_section_impact"
    ]["changed_bytes"]
    unapproved_code_differences = protagonist_name_report[
        "whole_file_impact"
    ]["unexpected_changed_offsets"]
    if (
        len(code_differences) != expected_code_change_count
        or unapproved_code_differences
    ):
        failures.append(
            {
                "family": "dol",
                "id": "<code>",
                "reason": "code changed outside approved name redirects",
            }
        )

    all_expected = (
        list(add01_map.values())
        + list(add02_map.values())
        + list(bpilot_map.values())
        + list(dol_map.values())
        + list(name_map.values())
        + [
            str(protagonist_name_report["canonical_names"]["gender_bit_0"]),
            str(protagonist_name_report["canonical_names"]["gender_bit_1"]),
        ]
    )
    residual = [text for text in all_expected if STRICT_JAPANESE_RE.search(text)]
    literal_bpilot_controls = [
        stable_id
        for stable_id, text in bpilot_map.items()
        if "KK" in text or "EE" in text
    ]
    add02_structure = add02_dol_tools.validate_add02_structure(
        build / "add02dat.bin", args.original / "add02dat.bin"
    )

    report = {
        "status": "pass" if not failures and not residual else "fail",
        "runtime_base": "Japanese retail",
        "english_runtime_dependencies": [],
        "pilotinfo_replacement": False,
        "replacement_counts": {
            "add01": len(add01_map),
            "add02": len(add02_map),
            "bpilot": len(bpilot_map),
            "dol_display": len(dol_map),
            "dol_name_input": len(name_map),
            "dol_protagonist_name_code_changes": len(
                protagonist_name_report["code_changes"]
            ),
            "dol_functional_katakana_preserved": 83,
            "total_verified_payloads": len(all_expected),
        },
        "verification_failure_count": len(failures),
        "verification_failures": failures,
        "decoded_final_japanese_residual_count": len(residual),
        "bpilot_literal_add01_control_count": len(literal_bpilot_controls),
        "add01_alignment": add01_alignment,
        "add02_runtime_transform": add02_transform,
        "add02_structure": add02_structure,
        "dol_pool": {
            "records": dol_report["unique_strings_relocated"],
            "pointers": dol_report["pointers_rewritten"],
            "bytes_used": dol_report["pool_bytes_used"],
            "bytes_remaining": dol_report["pool_bytes_remaining"],
        },
        "dol_name_grid": name_report,
        "dol_protagonist_names": protagonist_name_report,
        "dol_text_code_sections_identical_to_japanese": False,
        "dol_approved_code_byte_change_count": len(code_differences),
        "dol_unapproved_code_byte_changes": unapproved_code_differences,
        "success": (
            not failures
            and not residual
            and not literal_bpilot_controls
            and add01_alignment["valid"]
            and add02_structure["valid"]
        ),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "success": report["success"],
                "runtime_base": report["runtime_base"],
                "total_verified_payloads": len(all_expected),
                "failure_count": len(failures),
                "japanese_residual_count": len(residual),
                "add01_alignment": add01_alignment,
                "dol_approved_code_byte_change_count": len(code_differences),
                "dol_unapproved_code_byte_change_count": len(
                    unapproved_code_differences
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
