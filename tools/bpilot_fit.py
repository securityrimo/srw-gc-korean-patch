"""Fit bpilot battle dialogue to the retail text window.

The Japanese retail battle text keeps every line at or under 19 cells (56 lines
reach 18, only 3 reach 19) and breaks lines with a raw ``\\n``. Much of the
Korean text came from a legacy translation CSV that stored each line as one
unbroken cell, so 11,961 records carry fewer lines than the Japanese they
replace and 3,670 have a line past 18 cells -- one runs to 82. That predates the
machine-translation pass: only 33 of the 3,670 were touched by it.

Unlike add01, nothing guards the ``\\n`` count (the override tool only pins
``KK`` and the runtime tokens), so the line structure can simply be rebuilt.
Each record is re-wrapped to the window width and is allowed at most the line
count the Japanese record itself uses, which is the layout the retail game is
known to render for that line.

Records whose Korean text cannot reach that shape -- usually because a legacy
cell holds two alternative translations concatenated -- are reported for
rewording rather than silently truncated.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dialogue_fit import cells, split_units, wrap  # noqa: E402

WIDTH = 18


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("master", type=Path)
    parser.add_argument("maps", type=Path, help="directory holding bpilot_replacements.json")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    master = load_json(args.master)
    records = {r["id"]: r for r in master["records"] if r.get("family") == "bpilot"}
    mapping = load_json(args.maps / "bpilot_replacements.json")

    fixed: list[dict] = []
    needs_rewording: list[dict] = []
    unchanged = 0

    for stable_id, payload in mapping.items():
        record = records[stable_id]
        lines = payload.split("\n")
        japanese = record.get("japanese", "")
        jp_lines = japanese.split("\n")
        # The retail record proves this many lines render in this window; never
        # ask for more than that, and never fewer than one.
        budget = max(len(jp_lines), 1)

        over_width = any(cells(line) > WIDTH for line in lines)
        # Some legacy cells also carry a blank line or break where the retail
        # text does not, pushing a record past the layout it is drawn with.
        over_budget = len(lines) > budget
        has_blank = any(not line.strip() for line in lines)
        if not (over_width or over_budget or has_blank):
            unchanged += 1
            continue

        units = split_units(" ".join(line.strip() for line in lines if line.strip()))
        wrapped = wrap(units, WIDTH, budget)
        if wrapped is None:
            needs_rewording.append(
                {
                    "id": stable_id,
                    "member": record.get("member"),
                    "japanese": japanese,
                    "japanese_lines": len(jp_lines),
                    "width_limit": WIDTH,
                    "line_budget": budget,
                    "current": payload,
                    "widest": max(cells(line) for line in lines),
                    "english_reference": record.get("english_reference", ""),
                }
            )
            continue

        new_payload = "\n".join(wrapped)
        if new_payload == payload:
            unchanged += 1
            continue
        fixed.append(
            {
                "id": stable_id,
                "japanese": japanese,
                "before": payload,
                "after": new_payload,
            }
        )

    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "bpilot_rewrapped.json").write_text(
        json.dumps(fixed, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (args.output / "bpilot_needs_rewording.json").write_text(
        json.dumps(needs_rewording, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    summary = {
        "records": len(mapping),
        "already_fitting": unchanged,
        "rewrapped": len(fixed),
        "needs_rewording": len(needs_rewording),
    }
    (args.output / "bpilot_fit_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
