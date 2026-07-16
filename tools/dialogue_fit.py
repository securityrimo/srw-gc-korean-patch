"""Fit add01 dialogue to the retail text window.

The dialogue window holds 18 cells per line -- the opening and closing brackets
count -- and at most 3 lines. The Japanese retail text corroborates the width:
of 883 add01 blocks, 866 keep every line at or under 18 cells.

Two groups of exceptions are real, not noise, so the limit is per block rather
than global:

* block 0 is the speakerless prologue narration, whose window is visibly wider
  (retail lines run to 25 cells);
* 16 blocks contain a single 19-cell retail line.

The limit for a block is therefore ``max(18, widest retail line in that block)``:
never tighter than the window the user measured, and never wider than what the
retail game itself demonstrably renders in that scene.

Re-wrapping, not re-translating, is the fix. The reviewed Korean wording is kept
verbatim and only the line breaks move, which also keeps the ``KK`` count that
the override tool refuses to see change. A record that cannot fit its own line
budget even when wrapped optimally is reported for rewording instead.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BASE_WIDTH = 18
STRUCTURE_TOKENS = ("<AA>", "<FF>", "<TT>")
# Runtime name tokens expand to a pilot name when drawn. Budget them at the
# widest canonical given name (아카츠키 -> 4 cells) so a wrapped line cannot
# overflow once substituted.
TOKEN_CELLS = 4


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def cells(text: str) -> int:
    """Display width of one line, counting runtime tokens at their drawn size."""
    stripped = text
    for token in STRUCTURE_TOKENS:
        stripped = stripped.replace(token, "￿" * TOKEN_CELLS)
    return len(stripped)


def split_units(text: str) -> list[str]:
    """Split a line into wrap units, keeping a trailing space with its word.

    Korean wraps at spaces. Structure tokens must never be split, so they are
    kept whole as their own unit.
    """
    pattern = "(" + "|".join(re.escape(token) for token in STRUCTURE_TOKENS) + ")"
    units: list[str] = []
    for part in re.split(pattern, text):
        if not part:
            continue
        if part in STRUCTURE_TOKENS:
            units.append(part)
            continue
        for word in re.findall(r"\S+\s*", part):
            units.append(word)
    return units


def wrap(units: list[str], width: int, max_lines: int) -> list[str] | None:
    """Greedy wrap; returns None when the text cannot fit the line budget."""
    lines: list[str] = []
    current = ""
    for unit in units:
        candidate = current + unit
        if not current:
            if cells(candidate.rstrip()) > width:
                return None  # a single unit is wider than the window
            current = candidate
        elif cells(candidate.rstrip()) <= width:
            current = candidate
        else:
            lines.append(current.rstrip())
            current = unit.lstrip()
    if current.strip():
        lines.append(current.rstrip())
    if not lines or len(lines) > max_lines:
        return None
    return lines


def block_limits(records: dict) -> dict[int, int]:
    """Widest retail line per block, floored at the measured window width."""
    limits: dict[int, int] = {}
    for record in records.values():
        block = record.get("block")
        text = record.get("display_text") or record.get("japanese", "")
        widest = max((len(line) for line in text.split("\n")), default=0)
        limits[block] = max(limits.get(block, BASE_WIDTH), widest, BASE_WIDTH)
    return limits


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("master", type=Path)
    parser.add_argument("maps", type=Path, help="directory holding add01_replacements.json")
    parser.add_argument("output", type=Path, help="report + corrections output directory")
    parser.add_argument("--max-lines", type=int, default=3)
    args = parser.parse_args()

    master = load_json(args.master)
    records = {r["id"]: r for r in master["records"] if r.get("family") == "add01"}
    mapping = load_json(args.maps / "add01_replacements.json")
    limits = block_limits(records)

    fixed: list[dict] = []
    needs_rewording: list[dict] = []
    unchanged = 0
    for stable_id, payload in mapping.items():
        record = records[stable_id]
        has_speaker = bool(record.get("speaker"))
        segments = payload.split("KK")
        speaker = segments[0] if has_speaker else None
        body = segments[1:] if has_speaker else segments
        width = limits[record.get("block")]

        if all(cells(line) <= width for line in body) and len(body) <= args.max_lines:
            unchanged += 1
            continue

        units = split_units(" ".join(line.strip() for line in body if line.strip()))
        wrapped = wrap(units, width, len(body))
        if wrapped is None or len(wrapped) != len(body):
            # Keeping the KK count is mandatory, so text that needs a different
            # number of lines than it has cannot be rewrapped mechanically.
            needs_rewording.append(
                {
                    "id": stable_id,
                    "block": record.get("block"),
                    "width_limit": width,
                    "required_lines": len(body),
                    "japanese": record.get("display_text") or record.get("japanese", ""),
                    "current_lines": body,
                    "widest": max(cells(line) for line in body),
                    "best_effort_lines": wrap(units, width, args.max_lines),
                }
            )
            continue

        new_payload = "KK".join(([speaker] if speaker else []) + wrapped)
        if new_payload == payload:
            unchanged += 1
            continue
        fixed.append(
            {
                "id": stable_id,
                "block": record.get("block"),
                "width_limit": width,
                "japanese": record.get("japanese", ""),
                "before": payload,
                "after": new_payload,
                "before_lines": body,
                "after_lines": wrapped,
            }
        )

    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "rewrapped.json").write_text(
        json.dumps(fixed, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (args.output / "needs_rewording.json").write_text(
        json.dumps(needs_rewording, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    summary = {
        "records": len(mapping),
        "already_fitting": unchanged,
        "rewrapped": len(fixed),
        "needs_rewording": len(needs_rewording),
        "wide_blocks": {str(b): w for b, w in sorted(limits.items()) if w > BASE_WIDTH},
    }
    (args.output / "fit_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
