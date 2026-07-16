"""Build rewording chunks for dialogue that cannot be re-wrapped into its window."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

CHUNK_SIZE = 30


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fit", type=Path, help="dialogue_fit / bpilot_fit output directory")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    rows: list[dict] = []
    for entry in load_json(args.fit / "needs_rewording.json"):
        rows.append(
            {
                "id": entry["id"],
                "family": "add01",
                "japanese": entry["japanese"],
                "current_lines": entry["current_lines"],
                "required_lines": entry["required_lines"],
                "width_limit": entry["width_limit"],
                "widest_now": entry["widest"],
                "instruction": (
                    f"정확히 {entry['required_lines']}줄로, 각 줄 {entry['width_limit']}자 이하로 "
                    f"줄이세요. 현재 가장 긴 줄이 {entry['widest']}자입니다."
                ),
            }
        )
    for entry in load_json(args.fit / "bpilot_needs_rewording.json"):
        rows.append(
            {
                "id": entry["id"],
                "family": "bpilot",
                "japanese": entry["japanese"],
                "current_lines": entry["current"].split("\n"),
                "required_lines": entry["line_budget"],
                "width_limit": entry["width_limit"],
                "widest_now": entry["widest"],
                "english_reference": entry.get("english_reference", ""),
                "instruction": (
                    f"{entry['line_budget']}줄 이하로, 각 줄 {entry['width_limit']}자 이하로 "
                    f"줄이세요. 현재 가장 긴 줄이 {entry['widest']}자입니다. "
                    "같은 대사가 두 번 반복되거나 후보 번역이 이어붙어 있으면 하나만 남기세요."
                ),
            }
        )

    rows.sort(key=lambda r: r["id"])
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = []
    for index in range(0, len(rows), CHUNK_SIZE):
        batch = rows[index : index + CHUNK_SIZE]
        name = f"chunk_fit_{index // CHUNK_SIZE:03d}.json"
        (args.output / name).write_text(
            json.dumps({"records": batch}, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        manifest.append({"chunk": name, "count": len(batch)})
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(json.dumps({"records": len(rows), "chunks": len(manifest)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
