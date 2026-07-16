"""Audit every dialogue record against the retail text window.

This is the check that v1.0.6 shipped without: the dialogue window draws 18
cells per line (「 and 」 included) over at most 3 lines, and battle text follows
the same 18-cell width. A scene whose Japanese retail text is itself wider is
allowed that width, because the retail game demonstrably renders it there.

Exit code is non-zero when anything overflows, so a build can gate on it.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dialogue_fit import cells  # noqa: E402

WIDTH = 18
MAX_ADD01_LINES = 3


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("master", type=Path)
    parser.add_argument("maps", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    master = load_json(args.master)
    records = {r["id"]: r for r in master["records"]}

    # Per-block retail width for add01; per-record retail layout for bpilot.
    block_width: dict[int, int] = {}
    for r in records.values():
        if r.get("family") != "add01":
            continue
        text = r.get("display_text") or r.get("japanese", "")
        widest = max((len(l) for l in text.split("\n")), default=0)
        block = r.get("block")
        block_width[block] = max(block_width.get(block, WIDTH), widest, WIDTH)

    failures = []
    checked = collections.Counter()

    add01 = load_json(args.maps / "add01_replacements.json")
    for sid, payload in add01.items():
        record = records[sid]
        segs = payload.split("KK")
        body = segs[1:] if record.get("speaker") else segs
        limit = block_width.get(record.get("block"), WIDTH)
        checked["add01"] += 1
        if len(body) > MAX_ADD01_LINES:
            failures.append({"id": sid, "kind": "line_count", "value": len(body)})
        for line in body:
            if cells(line) > limit:
                failures.append(
                    {"id": sid, "kind": "width", "value": cells(line),
                     "limit": limit, "line": line}
                )

    bpilot = load_json(args.maps / "bpilot_replacements.json")
    for sid, payload in bpilot.items():
        record = records[sid]
        jp_lines = len(record.get("japanese", "").split("\n"))
        lines = payload.split("\n")
        checked["bpilot"] += 1
        if len(lines) > max(jp_lines, 1):
            failures.append(
                {"id": sid, "kind": "line_count", "value": len(lines), "limit": jp_lines}
            )
        for line in lines:
            if cells(line) > WIDTH:
                failures.append(
                    {"id": sid, "kind": "width", "value": cells(line),
                     "limit": WIDTH, "line": line}
                )

    summary = {
        "checked": dict(checked),
        "failures": len(failures),
        "width_failures": len([f for f in failures if f["kind"] == "width"]),
        "line_count_failures": len([f for f in failures if f["kind"] == "line_count"]),
        "status": "pass" if not failures else "fail",
    }
    if args.report:
        args.report.write_text(
            json.dumps({"summary": summary, "failures": failures[:200]},
                       ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if failures:
        for f in failures[:10]:
            print("  ", f)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
