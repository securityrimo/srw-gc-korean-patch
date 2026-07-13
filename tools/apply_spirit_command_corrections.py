#!/usr/bin/env python3
"""정신기 명칭·축약·설명을 일본판 안정 ID 순서로 교정한다."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import Any


MAP_FILES = (
    "add01_replacements.json",
    "add02_replacements.json",
    "bpilot_replacements.json",
    "dol_all_replacements.json",
    "dol_name_input_replacements.json",
)
JAPANESE_RE = re.compile(
    r"[\u3005\u3041-\u3096\u30a1-\u30fa\u3400-\u9fff\uff66-\uff9f]"
)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def indexed_id(block: int, index: int) -> str:
    return f"add02:b{block:03d}:r{index:04d}:f0"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_records", type=Path)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--dol-records",
        type=Path,
        help="DOL 원문 레코드 JSON(기본값: add02 레코드와 같은 폴더)",
    )
    parser.add_argument(
        "--corrections",
        type=Path,
        default=Path(__file__).resolve().parent.parent
        / "data"
        / "spirit_command_corrections.json",
    )
    args = parser.parse_args()

    source_records = args.source_records.resolve()
    dol_records = (
        args.dol_records.resolve()
        if args.dol_records
        else source_records.with_name("dol_all_records.json")
    )
    input_dir = args.input.resolve()
    output_dir = args.output.resolve()
    corrections_path = args.corrections.resolve()
    if output_dir.exists():
        raise FileExistsError(output_dir)

    document = load_json(corrections_path)
    if document.get("schema") != "srw-gc-spirit-command-corrections-v1":
        raise ValueError("지원하지 않는 정신기 교정 스키마")
    add02_path = input_dir / "add02_replacements.json"
    dol_all_path = input_dir / "dol_all_replacements.json"
    if sha256(add02_path) != document["expected_input_add02_sha256"]:
        raise ValueError("정신기 교정 입력 add02 맵의 SHA-256이 다릅니다")
    if sha256(dol_all_path) != document["expected_input_dol_all_sha256"]:
        raise ValueError("정신기 교정 입력 DOL 맵의 SHA-256이 다릅니다")
    if sha256(source_records) != document["expected_source_records_sha256"]:
        raise ValueError("일본판 add02 레코드 원본의 SHA-256이 다릅니다")
    if sha256(dol_records) != document["expected_dol_source_records_sha256"]:
        raise ValueError("일본판 DOL 레코드 원본의 SHA-256이 다릅니다")

    source_document = load_json(source_records)
    source_rows = (
        source_document
        if isinstance(source_document, list)
        else source_document.get("records", [])
    )
    source_by_id = {str(row["id"]): row for row in source_rows}
    dol_source_document = load_json(dol_records)
    dol_source_rows = (
        dol_source_document
        if isinstance(dol_source_document, list)
        else dol_source_document.get("records", [])
    )
    source_by_id.update({str(row["id"]): row for row in dol_source_rows})
    mapping = {str(key): str(value) for key, value in load_json(add02_path).items()}
    dol_mapping = {
        str(key): str(value) for key, value in load_json(dol_all_path).items()
    }

    correction_rows = document["corrections"]
    if len(correction_rows) != int(document["expected_correction_count"]):
        raise ValueError("정신기 교정 건수가 고정값과 다릅니다")
    kinds = Counter(str(row["kind"]) for row in correction_rows)
    if kinds["short_label"] != int(
        document["expected_short_label_correction_count"]
    ):
        raise ValueError("정신기 축약 교정 건수가 고정값과 다릅니다")
    if kinds["description"] != int(
        document["expected_description_correction_count"]
    ):
        raise ValueError("정신기 설명 교정 건수가 고정값과 다릅니다")
    if kinds["dol_label"] != int(document["expected_dol_label_correction_count"]):
        raise ValueError("DOL 정신기 라벨 교정 건수가 고정값과 다릅니다")

    seen: set[str] = set()
    reports: list[dict[str, str]] = []
    for row in correction_rows:
        stable_id = str(row["id"])
        if stable_id in seen:
            raise ValueError(f"중복 정신기 교정 ID: {stable_id}")
        seen.add(stable_id)
        source = source_by_id.get(stable_id)
        if source is None or str(source.get("japanese")) != str(row["japanese"]):
            raise ValueError(f"{stable_id}: 일본판 원문 또는 순서가 달라졌습니다")
        before = str(row["before"])
        after = str(row["after"])
        target_mapping = dol_mapping if row["kind"] == "dol_label" else mapping
        if target_mapping.get(stable_id) != before:
            raise ValueError(f"{stable_id}: 교정 전 한국어가 예상과 다릅니다")
        if before == after:
            raise ValueError(f"{stable_id}: 실질적인 교정이 없습니다")
        if JAPANESE_RE.search(after):
            raise ValueError(f"{stable_id}: 교정문에 일본어 문자가 남았습니다")
        target_mapping[stable_id] = after
        reports.append(
            {
                "kind": str(row["kind"]),
                "id": stable_id,
                "japanese": str(row["japanese"]),
                "before": before,
                "after": after,
            }
        )

    expected_names = [str(value) for value in document["full_names_korean"]]
    expected_short = [str(value) for value in document["short_labels_korean"]]
    expected_descriptions = [
        str(value) for value in document["descriptions_korean"]
    ]
    if (
        len(expected_names) != 30
        or len(expected_short) != 30
        or len(expected_descriptions) != 29
    ):
        raise ValueError("정신기 명칭·축약·설명 고정 목록은 각각 30·30·29건이어야 합니다")
    actual_names = [mapping[indexed_id(21, index)] for index in range(30)]
    actual_short = [mapping[indexed_id(22, index)] for index in range(30)]
    if actual_names != expected_names:
        raise ValueError("정신기 전체 명칭 순서가 일본판 기준과 다릅니다")
    if actual_short != expected_short:
        raise ValueError("정신기 축약 명칭 순서가 일본판 기준과 다릅니다")

    description_ids = [indexed_id(23, index) for index in range(29)]
    if any(stable_id not in mapping for stable_id in description_ids):
        raise ValueError("정신기 설명 29건 중 누락된 안정 ID가 있습니다")
    actual_descriptions = [mapping[stable_id] for stable_id in description_ids]
    if actual_descriptions != expected_descriptions:
        raise ValueError("정신기 설명 순서 또는 최종 문구가 일본판 기준 고정값과 다릅니다")
    hidden_love_id = indexed_id(23, 29)
    if hidden_love_id in mapping:
        raise ValueError("일본판에서 ???로 숨긴 사랑 설명은 덮어쓰지 않습니다")
    if str(source_by_id[hidden_love_id]["japanese"]) != "？？？":
        raise ValueError("일본판 사랑 설명의 숨김 표식이 달라졌습니다")

    description_lines = [line for text in actual_descriptions for line in text.splitlines()]
    if any(len(text.splitlines()) > 2 for text in actual_descriptions):
        raise ValueError("정신기 설명이 일본판의 2줄 표시 한계를 넘습니다")
    max_columns = max(map(len, description_lines), default=0)
    maximum = int(document["maximum_description_line_columns"])
    if max_columns > maximum:
        raise ValueError(
            f"정신기 설명 한 줄이 표시 한계를 넘습니다: {max_columns} > {maximum}"
        )
    max_bytes = max(
        (sum(1 if ord(character) < 0x80 else 2 for character in line)
         for line in description_lines),
        default=0,
    )
    maximum_bytes = int(document["maximum_description_line_bytes"])
    if max_bytes > maximum_bytes:
        raise ValueError(
            f"정신기 설명 한 줄이 바이트 한계를 넘습니다: {max_bytes} > {maximum_bytes}"
        )

    output_dir.mkdir(parents=True)
    for filename in MAP_FILES:
        source_path = input_dir / filename
        if filename == "add02_replacements.json":
            (output_dir / filename).write_text(
                json.dumps(mapping, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        elif filename == "dol_all_replacements.json":
            (output_dir / filename).write_text(
                json.dumps(dol_mapping, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        else:
            shutil.copy2(source_path, output_dir / filename)
    quality_report = input_dir / "translation_quality_report.json"
    if quality_report.exists():
        shutil.copy2(quality_report, output_dir / quality_report.name)

    report = {
        "schema": "srw-gc-spirit-command-correction-report-v1",
        "status": "pass",
        "input_add02_sha256": sha256(add02_path),
        "output_add02_sha256": sha256(output_dir / "add02_replacements.json"),
        "input_dol_all_sha256": sha256(dol_all_path),
        "output_dol_all_sha256": sha256(output_dir / "dol_all_replacements.json"),
        "full_names_checked": len(actual_names),
        "short_labels_checked": len(actual_short),
        "descriptions_checked": len(description_ids),
        "corrected_records": len(reports),
        "corrected_short_labels": kinds["short_label"],
        "corrected_descriptions": kinds["description"],
        "corrected_dol_labels": kinds["dol_label"],
        "maximum_description_line_columns": max_columns,
        "maximum_description_line_bytes": max_bytes,
        "hidden_love_description_preserved": True,
        "records": reports,
    }
    (output_dir / "spirit_command_correction_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
