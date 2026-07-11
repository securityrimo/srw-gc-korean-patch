#!/usr/bin/env python3
"""Build the Korean patch entirely on Japanese retail runtime structures."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
from pathlib import Path

import add01_tools
import add02_dol_tools
import add02_fixed_topology
import bpilot_tools
from canonical_protagonist_names import apply_patch as install_canonical_protagonist_names


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest().upper()


def load_map(path: Path) -> dict[str, str]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in document.items()
    ):
        raise ValueError(f"invalid replacement map: {path}")
    return document


def pad_to_retail(source: Path, data: bytes, name: str) -> bytes:
    target = source.stat().st_size
    if len(data) > target:
        raise RuntimeError(f"{name} exceeds Japanese retail size: {len(data)} > {target}")
    return data + bytes(target - len(data))


def apply_name_input_grid_japanese(
    dol: bytes,
    original_dol: Path,
    replacements_path: Path,
    encoder,
) -> tuple[bytes, dict[str, object]]:
    proposal = json.loads(replacements_path.read_text(encoding="utf-8"))
    replacements = proposal["replacements"]
    if len(replacements) != 235:
        raise RuntimeError(f"expected 235 name-input rows, got {len(replacements)}")
    pointer_records = {
        record["id"]: record
        for record in add02_dol_tools.dol_pointer_records(original_dol)
    }
    original = original_dol.read_bytes()
    output = bytearray(dol)
    changed = 0
    for stable_id, text in replacements.items():
        record = pointer_records[stable_id]
        offset = int(record["target_file_offset"])
        size = int(record["payload_size"])
        encoded = encoder(text)
        if size != 30 or len(encoded) != 30:
            raise RuntimeError(f"invalid Japanese name-input row size: {stable_id}")
        if output[offset : offset + size] != original[offset : offset + size]:
            raise RuntimeError(f"name-input row changed before fixed remap: {stable_id}")
        if output[offset + size] != 0:
            raise RuntimeError(f"name-input terminator drift: {stable_id}")
        changed += sum(
            left != right
            for left, right in zip(output[offset : offset + size], encoded)
        )
        output[offset : offset + size] = encoded
    return bytes(output), {
        "status": "pass",
        "base": "Japanese retail Start.dol",
        "rows": len(replacements),
        "payload_bytes_written": len(replacements) * 30,
        "changed_bytes": changed,
        "pointer_bytes_changed": 0,
        "terminators_preserved": True,
    }


def add01_alignment_report(data: bytes) -> dict[str, object]:
    container = add01_tools.parse_container(data)
    records = [record for block in container.blocks for record in block.records]
    report = {
        "record_count": len(records),
        "odd_command_offsets": sum(record.command_offset % 2 for record in records),
        "odd_payload_offsets": sum(record.payload_offset % 2 for record in records),
        "odd_end_control_offsets": sum(record.payload_end % 2 for record in records),
        "odd_payload_lengths": sum(len(record.payload) % 2 for record in records),
    }
    report["valid"] = all(
        report[key] == 0
        for key in (
            "odd_command_offsets",
            "odd_payload_offsets",
            "odd_end_control_offsets",
            "odd_payload_lengths",
        )
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--codebook", type=Path, required=True)
    parser.add_argument("--font", type=Path, required=True)
    parser.add_argument("--original-font", type=Path, required=True)
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--original-bpilot", type=Path, required=True)
    parser.add_argument("--add00", type=Path, required=True)
    parser.add_argument("--opening", type=Path, required=True)
    args = parser.parse_args()

    root = args.root.resolve()
    output = args.output.resolve()
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)

    add01_map = load_map(root / "add01_replacements.json")
    add02_map = load_map(root / "add02_replacements.json")
    bpilot_map = load_map(root / "bpilot_replacements.json")
    dol_map = load_map(root / "dol_all_replacements.json")

    codebook = add02_dol_tools.load_codebook(args.codebook)
    add01_encoder = add01_tools.make_codebook_encoder(codebook)
    general_encoder = add02_dol_tools.codebook_encoder(codebook)
    bpilot_encoder = bpilot_tools.make_codebook_encoder(args.codebook)

    original_add01 = args.original / "add01dat.bin"
    original_add02 = args.original / "add02dat.bin"
    original_dol = args.original / "Start.dol"
    original_pilotinfo = args.original / "pilotinfo.bin"

    semantic_add01 = add01_tools.repack(
        original_add01, add01_map, add01_encoder
    )
    built_add01 = pad_to_retail(original_add01, semantic_add01, "add01dat.bin")

    runtime_add02_map, add02_whitespace = add02_fixed_topology.prepare_replacements(
        add02_map, compact_library_whitespace=True
    )
    # The generic repacker starts from the Japanese block table and preserves
    # all Japanese record IDs/order while allowing updated block offsets.
    built_add02 = add02_dol_tools.repack(
        original_add02, runtime_add02_map, general_encoder
    )

    semantic_bpilot = bpilot_tools.repack(
        args.original_bpilot, bpilot_map, bpilot_encoder
    )
    built_bpilot = pad_to_retail(
        args.original_bpilot, semantic_bpilot, "bpilot.pak"
    )
    built_font = pad_to_retail(
        args.original_font, args.font.read_bytes(), "font.pak"
    )

    built_dol, dol_report = add02_dol_tools.build_japanese_data_pointer_dol_patch(
        original_dol, dol_map, general_encoder
    )
    built_dol, name_grid_report = apply_name_input_grid_japanese(
        built_dol,
        original_dol,
        root / "dol_name_input_replacements.json",
        general_encoder,
    )
    built_dol, protagonist_name_report = install_canonical_protagonist_names(
        built_dol,
        original_dol.read_bytes(),
        general_encoder,
        add02_dol_tools,
    )

    payloads = {
        "add01dat.bin": built_add01,
        "add02dat.bin": built_add02,
        "bpilot.pak": built_bpilot,
        "font.pak": built_font,
        "Start.dol": built_dol,
    }
    for name, data in payloads.items():
        (output / name).write_bytes(data)
    shutil.copy2(args.add00, output / "add00dat.bin")
    shutil.copy2(args.opening, output / "opening.bnr")

    add01_alignment = add01_alignment_report(built_add01)
    if not add01_alignment["valid"]:
        raise RuntimeError(f"add01 runtime alignment failed: {add01_alignment}")
    if add01_alignment["record_count"] != 22408:
        raise RuntimeError("Japanese add01 record count changed")

    add02_validation = add02_dol_tools.validate_add02_structure(
        output / "add02dat.bin", original_add02
    )
    if not add02_validation["valid"]:
        raise RuntimeError("Japanese add02 structure validation failed")

    original_dol_data = original_dol.read_bytes()
    code_differences = [
        offset
        for section in add02_dol_tools.dol_sections(original_dol_data)
        if section.kind == "text"
        for offset in range(section.file_offset, section.file_offset + section.size)
        if built_dol[offset] != original_dol_data[offset]
    ]
    expected_code_change_count = protagonist_name_report[
        "code_section_impact"
    ]["changed_bytes"]
    if (
        len(code_differences) != expected_code_change_count
        or protagonist_name_report["code_section_impact"][
            "unexpected_changed_bytes"
        ]
        != 0
    ):
        raise RuntimeError(
            "Japanese DOL code changed outside approved protagonist-name redirects"
        )
    if dol_report["unique_strings_relocated"] != len(dol_map):
        raise RuntimeError("Japanese DOL relocation did not consume every display string")

    original_pak = bpilot_tools.parse_pak(args.original_bpilot.read_bytes())
    rebuilt_pak = bpilot_tools.parse_pak(built_bpilot)
    if [member.name for member in original_pak.members] != [
        member.name for member in rebuilt_pak.members
    ]:
        raise RuntimeError("Japanese bpilot directory changed")

    file_report: dict[str, dict[str, object]] = {}
    for path in sorted(output.iterdir()):
        if path.is_file():
            file_report[path.name] = {
                "size": path.stat().st_size,
                "sha256": sha256_path(path),
            }

    report = {
        "status": "pass",
        "runtime_base": "Japanese retail",
        "english_runtime_dependencies": [],
        "pilotinfo_replacement": False,
        "original_pilotinfo": {
            "path": str(original_pilotinfo.resolve()),
            "size": original_pilotinfo.stat().st_size,
            "sha256": sha256_path(original_pilotinfo),
        },
        "replacement_counts": {
            "add01": len(add01_map),
            "add02": len(add02_map),
            "bpilot": len(bpilot_map),
            "dol_display": len(dol_map),
            "dol_name_grid": name_grid_report["rows"],
            "dol_protagonist_name_code_changes": len(
                protagonist_name_report["code_changes"]
            ),
        },
        "add01": {
            "base": str(original_add01.resolve()),
            "semantic_size": len(semantic_add01),
            "retail_padded_size": len(built_add01),
            "alignment": add01_alignment,
        },
        "add02": {
            "base": str(original_add02.resolve()),
            "output_size": len(built_add02),
            "retail_size": original_add02.stat().st_size,
            "whitespace_transform": add02_whitespace,
            "structure": add02_validation,
        },
        "dol": {
            **{key: value for key, value in dol_report.items() if key != "records"},
            "name_grid": name_grid_report,
            "protagonist_names": protagonist_name_report,
            "text_code_sections_identical_to_japanese": False,
            "approved_protagonist_name_code_byte_changes": len(
                code_differences
            ),
            "unapproved_code_byte_changes": 0,
            "font_loader_source": "Japanese retail Start.dol",
            "pilotinfo_dependency": False,
        },
        "files": file_report,
    }
    (output / "japanese_binary_build_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "runtime_base": report["runtime_base"],
                "english_runtime_dependencies": [],
                "pilotinfo_replacement": False,
                "add01_alignment": add01_alignment,
                "dol_pool": {
                    "capacity": dol_report["pool_capacity"],
                    "used": dol_report["pool_bytes_used"],
                    "remaining": dol_report["pool_bytes_remaining"],
                    "strings": dol_report["unique_strings_relocated"],
                    "pointers": dol_report["pointers_rewritten"],
                },
                "files": file_report,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
