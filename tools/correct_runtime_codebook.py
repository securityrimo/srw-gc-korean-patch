#!/usr/bin/env python3
"""Convert SRW GC font storage keys to the codes consumed by the JP renderer.

The first two bytes of each 164-byte font record are compact storage keys.
For Ft07..Ft20, a bank spans the tail of one Shift-JIS lead-byte page and
the compacted head of the next page.  Start.dol's lookup at 0x801503FC adds
0x40 to records in the second page before comparing them with text bytes.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

from srw_gc_font_codec import parse_inner_entries, read_font_pak


RECORD_SIZE = 164


def runtime_code(file_name: str, storage_code: int, first_storage_code: int) -> int:
    lowered = file_name.lower()
    if lowered == "ft30.font":
        return storage_code
    if lowered.startswith("ft") and lowered.endswith(".font"):
        try:
            bank = int(lowered[2:4])
        except ValueError as error:
            raise ValueError(f"unsupported font member: {file_name}") from error
        if 7 <= bank <= 20:
            if (storage_code >> 8) > (first_storage_code >> 8):
                return storage_code + 0x40
            return storage_code
    raise ValueError(f"no Japanese renderer mapping for {file_name}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest().upper()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--font-pak", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.report.exists():
        raise FileExistsError("output or report already exists")

    pak = read_font_pak(args.font_pak)
    entries = {entry.name: entry for entry in parse_inner_entries(pak.decompressed)}
    with args.input.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        original_fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if not rows or "target" not in original_fieldnames or "code" not in original_fieldnames:
        raise ValueError("invalid source codebook")

    first_codes: dict[str, int] = {}
    runtime_seen: dict[int, str] = {}
    changed: list[dict[str, object]] = []
    for row in rows:
        file_name = row["file"]
        entry = entries[file_name]
        index = int(row["index"])
        position = entry.offset + index * RECORD_SIZE
        storage = int.from_bytes(pak.decompressed[position : position + 2], "big")
        csv_storage = int(row["code"], 16)
        if storage != csv_storage:
            raise RuntimeError(
                f"storage guard mismatch for {row['target']}: "
                f"CSV 0x{csv_storage:04X}, font 0x{storage:04X}"
            )
        if file_name not in first_codes:
            first_codes[file_name] = int.from_bytes(
                pak.decompressed[entry.offset : entry.offset + 2], "big"
            )
        runtime = runtime_code(file_name, storage, first_codes[file_name])
        if runtime > 0xFFFF:
            raise RuntimeError(f"runtime code overflow for {row['target']}")
        if runtime in runtime_seen:
            raise RuntimeError(
                f"runtime collision 0x{runtime:04X}: "
                f"{runtime_seen[runtime]} and {row['target']}"
            )
        runtime_seen[runtime] = row["target"]
        row["storage_code"] = f"0x{storage:04X}"
        row["code"] = f"0x{runtime:04X}"
        row["runtime_transform"] = "+0x40" if runtime != storage else "identity"
        if runtime != storage:
            changed.append(
                {
                    "target": row["target"],
                    "file": file_name,
                    "index": index,
                    "storage_code": f"0x{storage:04X}",
                    "runtime_code": f"0x{runtime:04X}",
                }
            )

    fieldnames = original_fieldnames + [
        name for name in ("storage_code", "runtime_transform") if name not in original_fieldnames
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    report = {
        "status": "pass",
        "renderer_lookup": "Japanese Start.dol 0x801503FC",
        "source_codebook": str(args.input.resolve()),
        "font_pak": str(args.font_pak.resolve()),
        "font_pak_sha256": sha256(args.font_pak),
        "entries": len(rows),
        "unique_runtime_codes": len(runtime_seen),
        "identity_codes": len(rows) - len(changed),
        "corrected_plus_0x40_codes": len(changed),
        "changed": changed,
    }
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({key: value for key, value in report.items() if key != "changed"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
