#!/usr/bin/env python3
"""Build a Korean-banner BNR1 while restoring Japanese retail metadata.

Only the 96x32 RGB5A3 texture (0x20..0x181F) is copied from the current
Korean banner.  The header, reserved bytes, and all metadata fields come
byte-for-byte from the original Japanese BNR1.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


BNR1_SIZE = 0x1960
TEXTURE_OFFSET = 0x20
TEXTURE_SIZE = 0x1800
TEXTURE_END = TEXTURE_OFFSET + TEXTURE_SIZE

FIELDS = (
    ("short_title", 0x1820, 0x20),
    ("short_maker", 0x1840, 0x20),
    ("long_title", 0x1860, 0x40),
    ("long_maker", 0x18A0, 0x40),
    ("description", 0x18E0, 0x80),
)

ENGLISH_PATCH_MARKERS = (
    b"Super Robot Wars",
    b"SRW GC Korean",
    b"Korean Patch",
    b"Korean fan translation",
    b"heroic robots",
    b"EN v1.0",
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def validate(data: bytes, label: str) -> None:
    if len(data) != BNR1_SIZE:
        raise ValueError(
            f"{label}: expected 0x{BNR1_SIZE:X} bytes, got 0x{len(data):X}"
        )
    if data[:4] != b"BNR1":
        raise ValueError(f"{label}: expected BNR1 magic, got {data[:4]!r}")


def parse_metadata(data: bytes) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for name, offset, size in FIELDS:
        raw = data[offset : offset + size].split(b"\0", 1)[0]
        result[name] = {
            "offset_hex": f"0x{offset:04X}",
            "slot_size": size,
            "payload_size": len(raw),
            "raw_hex": raw.hex().upper(),
            "text_cp932": raw.decode("cp932", errors="strict"),
        }
    return result


def changed_byte_count(left: bytes, right: bytes) -> int:
    return sum(a != b for a, b in zip(left, right))


def main() -> None:
    here = Path(__file__).resolve().parent
    work = here.parent
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--original",
        type=Path,
        default=work / "srw_gc_complete_patch" / "opening_original_extracted.bnr",
    )
    parser.add_argument(
        "--korean",
        type=Path,
        default=work / "srw_gc_complete_build_v9_japanese" / "opening.bnr",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=here / "opening_korean_jp_metadata.bnr",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=here / "opening_korean_jp_metadata_report.json",
    )
    args = parser.parse_args()

    original = args.original.read_bytes()
    korean = args.korean.read_bytes()
    validate(original, "Japanese original")
    validate(korean, "current Korean banner")

    output = bytearray(original)
    output[TEXTURE_OFFSET:TEXTURE_END] = korean[TEXTURE_OFFSET:TEXTURE_END]
    output_bytes = bytes(output)
    validate(output_bytes, "output")

    original_outside_texture = original[:TEXTURE_OFFSET] + original[TEXTURE_END:]
    output_outside_texture = output_bytes[:TEXTURE_OFFSET] + output_bytes[TEXTURE_END:]
    markers = {
        marker.decode("ascii"): output_bytes.find(marker)
        for marker in ENGLISH_PATCH_MARKERS
    }
    metadata = parse_metadata(output_bytes)
    validations = {
        "exact_bnr1_size": len(output_bytes) == BNR1_SIZE,
        "bnr1_magic": output_bytes[:4] == b"BNR1",
        "texture_matches_current_korean": (
            output_bytes[TEXTURE_OFFSET:TEXTURE_END]
            == korean[TEXTURE_OFFSET:TEXTURE_END]
        ),
        "all_nontexture_bytes_match_japanese_original": (
            output_outside_texture == original_outside_texture
        ),
        "metadata_region_matches_japanese_original": (
            output_bytes[TEXTURE_END:] == original[TEXTURE_END:]
        ),
        "english_patch_markers_absent": all(offset < 0 for offset in markers.values()),
    }
    if not all(validations.values()):
        raise AssertionError(validations)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(output_bytes)
    report = {
        "format": "srw-gc-opening-bnr1-japanese-metadata-v1",
        "status": "complete",
        "method": (
            "Japanese original BNR1 is the base; only the RGB5A3 texture bytes "
            "at 0x0020..0x181F are copied from the current Korean banner."
        ),
        "inputs": {
            "japanese_original": {
                "path": str(args.original.resolve()),
                "size": len(original),
                "sha256": sha256(original),
            },
            "current_korean": {
                "path": str(args.korean.resolve()),
                "size": len(korean),
                "sha256": sha256(korean),
                "texture_sha256": sha256(korean[TEXTURE_OFFSET:TEXTURE_END]),
            },
        },
        "output": {
            "path": str(args.output.resolve()),
            "size": len(output_bytes),
            "sha256": sha256(output_bytes),
            "magic": output_bytes[:4].decode("ascii"),
            "texture_range": "0x0020..0x181F",
            "texture_sha256": sha256(output_bytes[TEXTURE_OFFSET:TEXTURE_END]),
            "metadata_sha256": sha256(output_bytes[TEXTURE_END:]),
            "metadata": metadata,
        },
        "validation": validations,
        "english_patch_marker_offsets": markers,
        "diff": {
            "changed_bytes_vs_japanese_original": changed_byte_count(
                original, output_bytes
            ),
            "changed_bytes_vs_current_korean": changed_byte_count(korean, output_bytes),
        },
        "note": (
            "BANPRESTO 2004 is retained because it is the official Japanese retail "
            "maker field, not an English translation-patch trace."
        ),
    }
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    # Keep the console summary ASCII-safe on Korean Windows (cp949), while the
    # report file above remains readable UTF-8 with native Japanese text.
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
