"""Extract machine-translated records for LLM quality review.

Produces, under the output directory:
- mt_targets_add01.jsonl / mt_targets_bpilot.jsonl / mt_targets_add02.jsonl /
  mt_targets_dol.jsonl: one JSON object per reviewable record, ordered for
  scene continuity, with neighbouring context lines.
- allowed_chars.json: the exact character inventory a corrected payload may
  use (codebook Hangul + observed cp932-safe characters).
- glossary.json: canonical name/term pairs harvested from human-confirmed
  records (speakers, pilot names, robot names, add02 terminology).
- corpus_stats.json: line-width and line-count distributions from
  human-confirmed translations, used as rewrite constraints.
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import re
import sys
from pathlib import Path

MACHINE_SOURCES = {"machine_translated_missing", "machine_translated_missing_dol"}
HUMAN_SOURCES_PREFIX = ("existing_", "manual_")
JAPANESE_RE = re.compile(r"[々ぁ-ゖァ-ヺ㐀-鿿ｦ-ﾟ]")
HANGUL_RE = re.compile(r"[가-힣]")
MAP_FILES = {
    "add01": "add01_replacements.json",
    "add02": "add02_replacements.json",
    "bpilot": "bpilot_replacements.json",
    "dol": "dol_all_replacements.json",
}


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("master", type=Path)
    parser.add_argument("maps_root", type=Path)
    parser.add_argument("codebook", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--include-all",
        action="store_true",
        help=(
            "emit every record, not just the machine-translated ones. Needed when "
            "validating a change that also touches human-translated records, such "
            "as fitting existing dialogue to the text window."
        ),
    )
    args = parser.parse_args()

    out = args.output
    out.mkdir(parents=True, exist_ok=True)

    master = load_json(args.master)
    records = master["records"]
    by_id = {r["id"]: r for r in records}

    maps = {family: load_json(args.maps_root / name) for family, name in MAP_FILES.items()}
    id_to_payload = {}
    for family, mapping in maps.items():
        id_to_payload.update(mapping)

    # --- allowed characters ---------------------------------------------
    codebook_targets = set()
    with args.codebook.open(encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            codebook_targets.add(row["target"])
    observed = set()
    for mapping in maps.values():
        for payload in mapping.values():
            observed.update(payload)
    # Characters that are safe outside the codebook: seen in shipped maps and
    # not Hangul (Hangul must come from the codebook) and not Japanese script.
    cp932_extra = sorted(
        c for c in observed
        if not HANGUL_RE.match(c) and not JAPANESE_RE.match(c)
    )
    hangul_allowed = sorted(c for c in codebook_targets if HANGUL_RE.match(c))
    non_hangul_codebook = sorted(c for c in codebook_targets if not HANGUL_RE.match(c))
    (out / "allowed_chars.json").write_text(
        json.dumps(
            {
                "hangul_codebook": "".join(hangul_allowed),
                "non_hangul_codebook": "".join(non_hangul_codebook),
                "cp932_extra_observed": "".join(cp932_extra),
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )

    # --- glossary ---------------------------------------------------------
    speakers = {}
    for r in records:
        jp = r.get("speaker")
        ko = r.get("final_speaker_korean")
        if jp and ko and str(r.get("speaker_translation_source", "")).startswith("existing"):
            speakers.setdefault(jp, collections.Counter())[ko] += 1
    speaker_map = {jp: counter.most_common(1)[0][0] for jp, counter in speakers.items()}

    terms = {}
    for r in records:
        if r.get("family") != "add02":
            continue
        category = str(r.get("category", ""))
        src = str(r.get("translation_source", ""))
        if not src.startswith(HUMAN_SOURCES_PREFIX):
            continue
        jp = str(r.get("japanese", "")).strip()
        ko = str(r.get("final_korean", "")).strip()
        if jp and ko and jp != ko:
            terms.setdefault(category, {})[jp] = ko
    (out / "glossary.json").write_text(
        json.dumps({"speakers": speaker_map, "add02_terms": terms}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )

    # --- corpus line stats -------------------------------------------------
    def line_lengths(payload: str, family: str):
        if family == "add01":
            parts = payload.split("KK")
            # first part is the speaker prefix when a speaker exists; include
            # every part as display lines to stay conservative.
            return [len(p) for p in parts if p]
        return [len(p) for p in payload.split("\n") if p]

    stats = {}
    for family, mapping in maps.items():
        widths = collections.Counter()
        line_counts = collections.Counter()
        for stable_id, payload in mapping.items():
            record = by_id.get(stable_id)
            if record is None:
                continue
            src = str(record.get("translation_source", ""))
            if not src.startswith(HUMAN_SOURCES_PREFIX):
                continue
            lengths = line_lengths(payload, family)
            for value in lengths:
                widths[value] += 1
            line_counts[len(lengths)] += 1
        stats[family] = {
            "human_line_width_max": max(widths) if widths else 0,
            "human_line_width_p99": _percentile(widths, 0.99),
            "human_line_width_p95": _percentile(widths, 0.95),
            "human_line_count_max": max(line_counts) if line_counts else 0,
        }
    (out / "corpus_stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    # --- target extraction ---------------------------------------------------
    def sort_key(r):
        if r["family"] == "add01":
            return (r.get("block", 0), r.get("offset", 0))
        if r["family"] == "bpilot":
            return (r.get("member", ""), r.get("record_index", 0), r.get("text_index", 0))
        if r["family"] == "add02":
            return (r.get("block_index", 0), r.get("record_index", 0))
        return (r.get("id", ""),)

    families = {"add01": [], "bpilot": [], "add02": [], "dol": []}
    for r in records:
        fam = r.get("family")
        if fam not in families:
            continue
        if not args.include_all and str(r.get("translation_source", "")) not in MACHINE_SOURCES:
            continue
        stable_id = r["id"]
        payload = id_to_payload.get(stable_id)
        if payload is None:
            continue
        families[fam].append(r)

    ordered_all = {
        fam: sorted((r for r in records if r.get("family") == fam), key=sort_key)
        for fam in families
    }
    index_of = {
        fam: {r["id"]: i for i, r in enumerate(rows)} for fam, rows in ordered_all.items()
    }

    def context_rows(fam: str, r, span: int = 2):
        rows = ordered_all[fam]
        i = index_of[fam][r["id"]]
        neighbours = []
        for j in range(max(0, i - span), min(len(rows), i + span + 1)):
            if j == i:
                continue
            n = rows[j]
            if fam == "add01" and n.get("block") != r.get("block"):
                continue
            if fam == "bpilot" and n.get("member") != r.get("member"):
                continue
            neighbours.append(
                {
                    "position": "before" if j < i else "after",
                    "speaker": n.get("speaker", ""),
                    "japanese": n.get("display_text") or n.get("japanese", ""),
                    "korean": n.get("final_korean", ""),
                }
            )
        return neighbours

    counts = {}
    for fam, targets in families.items():
        targets.sort(key=sort_key)
        path = out / f"mt_targets_{fam}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for r in targets:
                stable_id = r["id"]
                payload = id_to_payload[stable_id]
                row = {
                    "id": stable_id,
                    "family": fam,
                    "japanese": r.get("display_text") or r.get("japanese", ""),
                    "japanese_raw": r.get("japanese", ""),
                    "speaker_jp": r.get("speaker", ""),
                    "speaker_ko": r.get("final_speaker_korean", ""),
                    "english_reference": r.get("english_reference", ""),
                    "current_korean": r.get("final_korean", ""),
                    "current_payload": payload,
                    "category": r.get("category", ""),
                    "block": r.get("block", r.get("block_index", "")),
                    "member": r.get("member", ""),
                    "context": context_rows(fam, r),
                }
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        counts[fam] = len(targets)

    print(json.dumps({"targets": counts, "output": str(out)}, ensure_ascii=False))
    return 0


def _percentile(counter: collections.Counter, q: float) -> int:
    total = sum(counter.values())
    if not total:
        return 0
    threshold = total * q
    running = 0
    for value in sorted(counter):
        running += counter[value]
        if running >= threshold:
            return value
    return max(counter)


if __name__ == "__main__":
    sys.exit(main())
