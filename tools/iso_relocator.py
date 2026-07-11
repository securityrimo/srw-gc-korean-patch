#!/usr/bin/env python3
"""Relocate replacement files into a GameCube ISO and update its FST.

The original image is copied first and is never modified.  Replacement files
are placed in the verified unused area between the system FST and the first
disc file, avoiding the need to shift the 1.46 GB filesystem payload.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
from pathlib import Path
from typing import Mapping


def align(value: int, boundary: int) -> int:
    return (value + boundary - 1) & ~(boundary - 1)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _read_fst(iso: Path) -> tuple[int, int, int, list[dict[str, object]]]:
    with iso.open("rb") as handle:
        handle.seek(0x420)
        dol_offset, fst_offset, fst_size = struct.unpack(">III", handle.read(12))
        handle.seek(fst_offset)
        fst = handle.read(fst_size)
    count = struct.unpack_from(">I", fst, 8)[0]
    table_size = count * 12
    if count < 1 or table_size > len(fst):
        raise ValueError("invalid GameCube FST")
    strings = fst[table_size:]
    raw = [struct.unpack_from(">III", fst, index * 12) for index in range(count)]

    def name_at(offset: int) -> str:
        end = strings.find(b"\0", offset)
        if end < 0:
            raise ValueError("unterminated FST name")
        return strings[offset:end].decode("shift_jis")

    entries: list[dict[str, object]] = []
    stack: list[tuple[str, int]] = [("", raw[0][2])]
    for index in range(1, count):
        while stack and index >= stack[-1][1]:
            stack.pop()
        parent = stack[-1][0] if stack else ""
        word0, word1, word2 = raw[index]
        name = name_at(word0 & 0xFFFFFF)
        path = f"{parent}/{name}" if parent else name
        is_dir = bool(word0 >> 24)
        if is_dir:
            stack.append((path, word2))
        entries.append(
            {
                "index": index,
                "path": path,
                "is_dir": is_dir,
                "offset": word1,
                "size": word2,
            }
        )
    return dol_offset, fst_offset, fst_size, entries


def build_iso(
    source: Path,
    destination: Path,
    replacements: Mapping[str, Path],
    *,
    dol: Path | None = None,
    report_path: Path | None = None,
    alignment: int = 0x800,
) -> dict[str, object]:
    if destination.exists():
        raise FileExistsError(destination)
    dol_offset, fst_offset, fst_size, entries = _read_fst(source)
    by_path = {str(entry["path"]).casefold(): entry for entry in entries if not entry["is_dir"]}
    unknown = [path for path in replacements if path.casefold() not in by_path]
    if unknown:
        raise KeyError(f"replacement paths absent from FST: {unknown}")

    first_file = min(int(entry["offset"]) for entry in entries if not entry["is_dir"])
    cursor = align(fst_offset + fst_size, alignment)
    placements: list[dict[str, object]] = []
    for path, replacement in replacements.items():
        data_size = replacement.stat().st_size
        cursor = align(cursor, alignment)
        if cursor + data_size > first_file:
            raise RuntimeError("replacement area would overlap the original filesystem")
        entry = by_path[path.casefold()]
        placements.append(
            {
                "path": str(entry["path"]),
                "fst_index": int(entry["index"]),
                "old_offset": int(entry["offset"]),
                "old_size": int(entry["size"]),
                "new_offset": cursor,
                "new_size": data_size,
                "source": str(replacement),
                "sha256": sha256(replacement),
            }
        )
        cursor += data_size

    if dol is not None:
        original_dol_size = fst_offset - dol_offset
        if dol.stat().st_size > original_dol_size:
            raise RuntimeError("replacement DOL would overlap the FST")

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    with destination.open("r+b") as output:
        if dol is not None:
            data = dol.read_bytes()
            output.seek(dol_offset)
            output.write(data)
            output.write(bytes(original_dol_size - len(data)))
        for placement in placements:
            replacement = Path(str(placement["source"]))
            output.seek(int(placement["new_offset"]))
            with replacement.open("rb") as handle:
                shutil.copyfileobj(handle, output, 1024 * 1024)
            table_position = fst_offset + int(placement["fst_index"]) * 12
            output.seek(table_position + 4)
            output.write(struct.pack(">II", int(placement["new_offset"]), int(placement["new_size"])))
        output.flush()
        os.fsync(output.fileno())

    # Reparse the built FST and verify every written region byte-for-byte.
    _, _, _, built_entries = _read_fst(destination)
    built_by_path = {
        str(entry["path"]).casefold(): entry for entry in built_entries if not entry["is_dir"]
    }
    with destination.open("rb") as built:
        for placement in placements:
            entry = built_by_path[str(placement["path"]).casefold()]
            if int(entry["offset"]) != placement["new_offset"] or int(entry["size"]) != placement["new_size"]:
                raise RuntimeError(f"FST verification failed for {placement['path']}")
            built.seek(int(placement["new_offset"]))
            written = built.read(int(placement["new_size"]))
            if hashlib.sha256(written).hexdigest().upper() != placement["sha256"]:
                raise RuntimeError(f"payload verification failed for {placement['path']}")

    report: dict[str, object] = {
        "source": str(source),
        "source_sha256": sha256(source),
        "destination": str(destination),
        "destination_sha256": sha256(destination),
        "dol_offset": f"0x{dol_offset:08X}",
        "fst_offset": f"0x{fst_offset:08X}",
        "fst_size": fst_size,
        "replacement_area_start": f"0x{align(fst_offset + fst_size, alignment):08X}",
        "replacement_area_end": f"0x{align(cursor, alignment):08X}",
        "first_original_file": f"0x{first_file:08X}",
        "dol": str(dol) if dol else None,
        "placements": placements,
    }
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
