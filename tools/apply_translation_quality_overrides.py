"""Apply reviewed PDF-backed translation corrections without mutating build inputs.

The production text builders consume stable-ID-to-payload JSON maps.  This tool
overlays narrowly reviewed terminology and dialogue corrections on those maps,
while using the extracted Japanese/Korean master only as read-only context.  It
refuses input drift, duplicate IDs, line-control changes, unexpected match
counts, and output overwrites.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


MAP_FILES = (
    "add01_replacements.json",
    "add02_replacements.json",
    "bpilot_replacements.json",
    "dol_all_replacements.json",
)
NAME_INPUT_FILE = "dol_name_input_replacements.json"
# Japanese syllables/ideographs only.  U+30FB KATAKANA MIDDLE DOT is also
# conventional Korean game punctuation, so the broader Katakana block would
# incorrectly reject otherwise Korean payloads such as ``프라우・보우``.
JAPANESE_RE = re.compile(
    r"[\u3005\u3041-\u3096\u30a1-\u30fa\u3400-\u9fff\uff66-\uff9f]"
)
FORBIDDEN_PLACEHOLDER_RE = re.compile(r"__SRWG_")
HANGUL_RANGE = r"\uac00-\ud7a3"
STRUCTURE_TOKENS = ("<AA>", "<FF>", "<TT>")
REPOSITORY_DIR = Path(__file__).resolve().parent.parent


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_speaker(value: object) -> str:
    speaker = str(value or "")
    return speaker[2:] if speaker.startswith("TT") else speaker


def compile_rule(before: str, after: str, mode: str) -> re.Pattern[str]:
    escaped = re.escape(before)
    if mode == "literal":
        return re.compile(escaped)
    if mode == "korean_word":
        return re.compile(
            rf"(?<![{HANGUL_RANGE}]){escaped}(?![{HANGUL_RANGE}])"
        )
    if mode == "guarded_literal":
        # Machine translations frequently attach Korean particles directly to
        # a truncated personal name (for example ``코우군`` for ``甲児くん``),
        # or glue the name to a surname (``카부토코우``).  A Korean word
        # boundary therefore misses genuine errors.  Replace the literal form
        # everywhere in an already Japanese-source-constrained record, while
        # protecting a canonical spelling that begins with the same syllables
        # (``코우지`` must not become ``코우지지``).
        canonical_suffix = after[len(before) :] if after.startswith(before) else ""
        suffix_guard = rf"(?!{re.escape(canonical_suffix)})" if canonical_suffix else ""
        return re.compile(rf"{escaped}{suffix_guard}")
    raise ValueError(f"unsupported replacement mode: {mode!r}")


def context_matches(record: dict[str, Any], rule: dict[str, Any]) -> bool:
    if str(record.get("id")) in {str(value) for value in rule.get("exclude_ids", [])}:
        return False

    families = {str(value) for value in rule.get("families", [])}
    if families and str(record.get("family")) not in families:
        return False

    japanese = str(record.get("japanese", ""))
    speaker = normalize_speaker(record.get("speaker"))
    japanese_any = [str(value) for value in rule.get("japanese_any", [])]
    speaker_any = {str(value) for value in rule.get("speaker_any", [])}
    if not japanese_any and not speaker_any:
        return True
    return any(value in japanese for value in japanese_any) or speaker in speaker_any


def apply_context_replacements(
    payload: str,
    record: dict[str, Any],
    compiled_rules: list[tuple[dict[str, Any], re.Pattern[str], str]],
) -> str:
    """Apply the same canonicalization pass to a direct override payload.

    Direct payload/dialogue overrides deliberately retain the pre-context text
    in their ``before`` fields so their provenance remains readable.  When a
    broader name rule is improved later, both sides must still be compared and
    emitted in that canonical context; otherwise a direct override can either
    fail its drift guard or reintroduce the spelling that was just fixed.
    """

    corrected = payload
    for rule, pattern, after in compiled_rules:
        if context_matches(record, rule):
            corrected = pattern.sub(after, corrected)
    return corrected


def validate_payload_change(before: str, after: str, stable_id: str) -> None:
    if before.count("KK") != after.count("KK"):
        raise ValueError(f"{stable_id}: KK line-control count changed")
    for token in STRUCTURE_TOKENS:
        if before.count(token) != after.count(token):
            raise ValueError(f"{stable_id}: {token} structure-token count changed")
    if JAPANESE_RE.search(after):
        raise ValueError(f"{stable_id}: Japanese remains in corrected payload")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("master", type=Path, help="read-only all-text master JSON")
    parser.add_argument("root", type=Path, help="directory containing production maps")
    parser.add_argument("output", type=Path, help="new directory for corrected maps")
    parser.add_argument(
        "--overrides",
        type=Path,
        default=REPOSITORY_DIR / "data" / "pdf_translation_quality_overrides.json",
    )
    args = parser.parse_args()

    master = args.master.resolve()
    root = args.root.resolve()
    output = args.output.resolve()
    overrides_path = args.overrides.resolve()
    if output.exists():
        raise FileExistsError(f"output directory already exists: {output}")
    if output in {master.parent, root, overrides_path.parent}:
        raise ValueError("output directory must be separate from every input")

    document = load_json(overrides_path)
    if document.get("schema") != "srw-gc-pdf-translation-quality-v1":
        raise ValueError("unsupported translation-quality override schema")

    expected = document["expected_inputs"]
    input_paths = {"master": master}
    input_paths.update({name: root / name for name in (*MAP_FILES, NAME_INPUT_FILE)})
    for name, path in input_paths.items():
        if not path.is_file():
            raise FileNotFoundError(path)
        actual = sha256(path)
        wanted = str(expected[name]).upper()
        if actual != wanted:
            raise ValueError(
                f"input SHA-256 drift for {name}: expected {wanted}, got {actual}"
            )

    master_document = load_json(master)
    records_list = master_document.get("records")
    if not isinstance(records_list, list) or not records_list:
        raise ValueError("master JSON has no records")
    records: dict[str, dict[str, Any]] = {}
    for row in records_list:
        stable_id = str(row["id"])
        if stable_id in records:
            raise ValueError(f"duplicate master stable ID: {stable_id}")
        records[stable_id] = row

    maps: dict[str, dict[str, str]] = {}
    id_to_file: dict[str, str] = {}
    for filename in MAP_FILES:
        mapping = load_json(root / filename)
        if not isinstance(mapping, dict) or not mapping:
            raise ValueError(f"{filename}: expected a non-empty object")
        normalized: dict[str, str] = {}
        for stable_id, value in mapping.items():
            if stable_id in id_to_file:
                raise ValueError(f"stable ID appears in multiple maps: {stable_id}")
            if stable_id not in records:
                raise ValueError(f"map stable ID missing from master: {stable_id}")
            id_to_file[stable_id] = filename
            normalized[str(stable_id)] = str(value)
        maps[filename] = normalized

    rule_reports: list[dict[str, Any]] = []
    compiled_context_rules: list[
        tuple[dict[str, Any], re.Pattern[str], str]
    ] = []
    changed_ids: set[str] = set()
    for index, rule in enumerate(document.get("context_replacements", [])):
        before = str(rule["before"])
        after = str(rule["after"])
        if not before or before == after:
            raise ValueError(f"context rule {index}: invalid before/after")
        pattern = compile_rule(before, after, str(rule.get("mode", "literal")))
        compiled_context_rules.append((rule, pattern, after))
        occurrences = 0
        affected: list[str] = []
        for filename in MAP_FILES:
            mapping = maps[filename]
            for stable_id, payload in list(mapping.items()):
                record = records[stable_id]
                if not context_matches(record, rule):
                    continue
                found = len(pattern.findall(payload))
                if not found:
                    continue
                corrected = pattern.sub(after, payload)
                validate_payload_change(payload, corrected, stable_id)
                mapping[stable_id] = corrected
                occurrences += found
                affected.append(stable_id)
                changed_ids.add(stable_id)

        wanted_occurrences = int(rule["expected_occurrences"])
        wanted_records = int(rule["expected_records"])
        if occurrences != wanted_occurrences or len(affected) != wanted_records:
            raise ValueError(
                f"context rule {index} match drift: occurrences={occurrences}/"
                f"{wanted_occurrences}, records={len(affected)}/{wanted_records}"
            )
        rule_reports.append(
            {
                "index": index,
                "before": before,
                "after": after,
                "occurrences": occurrences,
                "record_count": len(affected),
                "record_ids": affected,
            }
        )

    payload_reports: list[dict[str, Any]] = []
    seen_payload_ids: set[str] = set()
    for row in document.get("payload_overrides", []):
        stable_id = str(row["id"])
        if stable_id in seen_payload_ids:
            raise ValueError(f"duplicate payload override ID: {stable_id}")
        seen_payload_ids.add(stable_id)
        filename = id_to_file.get(stable_id)
        if filename is None:
            raise ValueError(f"payload override ID is not in a production map: {stable_id}")
        record = records[stable_id]
        if str(record.get("japanese")) != str(row["japanese"]):
            raise ValueError(f"{stable_id}: Japanese source drift")
        source_before = str(row["before"])
        source_after = str(row["after"])
        before = apply_context_replacements(
            source_before, record, compiled_context_rules
        )
        after = apply_context_replacements(
            source_after, record, compiled_context_rules
        )
        actual = maps[filename][stable_id]
        if actual != before:
            raise ValueError(
                f"{stable_id}: payload before-text drift: expected {before!r}, got {actual!r}"
            )
        validate_payload_change(before, after, stable_id)
        maps[filename][stable_id] = after
        if actual != after:
            changed_ids.add(stable_id)
        payload_reports.append(
            {
                "id": stable_id,
                "pdf": row["pdf"],
                "page": int(row["page"]),
                "before": before,
                "after": after,
            }
        )

    if len(payload_reports) != int(document["expected_payload_override_count"]):
        raise ValueError("payload override count drift")

    dialogue_reports: list[dict[str, Any]] = []
    seen_dialogue_ids: set[str] = set()
    for row in document.get("dialogue_overrides", []):
        stable_id = str(row["id"])
        if stable_id in seen_dialogue_ids:
            raise ValueError(f"duplicate dialogue override ID: {stable_id}")
        seen_dialogue_ids.add(stable_id)
        if id_to_file.get(stable_id) != "add01_replacements.json":
            raise ValueError(f"dialogue override is not an add01 record: {stable_id}")
        record = records[stable_id]
        if str(record.get("japanese")) != str(row["japanese"]):
            raise ValueError(f"{stable_id}: Japanese source drift")

        payload = maps["add01_replacements.json"][stable_id]
        if "KK" not in payload:
            raise ValueError(f"{stable_id}: add01 payload has no speaker separator")
        prefix, body = payload.split("KK", 1)
        source_before = str(row["before"])
        source_after = str(row["after"])
        before = apply_context_replacements(
            source_before, record, compiled_context_rules
        )
        after = apply_context_replacements(
            source_after, record, compiled_context_rules
        )
        if body != before:
            raise ValueError(
                f"{stable_id}: dialogue before-text drift: expected {before!r}, got {body!r}"
            )
        validate_payload_change(before, after, stable_id)
        corrected_payload = prefix + "KK" + after
        maps["add01_replacements.json"][stable_id] = corrected_payload
        if payload != corrected_payload:
            changed_ids.add(stable_id)
        dialogue_reports.append(
            {
                "id": stable_id,
                "pdf": row["pdf"],
                "page": int(row["page"]),
                "before": before,
                "after": after,
            }
        )

    if len(dialogue_reports) != int(document["expected_dialogue_override_count"]):
        raise ValueError("dialogue override count drift")
    if len(changed_ids) != int(document["expected_changed_record_count"]):
        raise ValueError(
            f"changed record count drift: {len(changed_ids)} != "
            f"{document['expected_changed_record_count']}"
        )

    placeholder_residuals = [
        {"file": filename, "id": stable_id}
        for filename in MAP_FILES
        for stable_id, payload in maps[filename].items()
        if FORBIDDEN_PLACEHOLDER_RE.search(payload)
    ]
    if placeholder_residuals:
        raise ValueError(
            f"internal translation placeholders remain: {placeholder_residuals}"
        )
    japanese_residuals = [
        {"file": filename, "id": stable_id}
        for filename in MAP_FILES
        for stable_id, payload in maps[filename].items()
        if JAPANESE_RE.search(payload)
    ]
    if japanese_residuals:
        raise ValueError(
            f"Japanese script remains in corrected maps: {japanese_residuals}"
        )

    output.mkdir(parents=True)
    output_hashes: dict[str, str] = {}
    for filename in MAP_FILES:
        path = output / filename
        path.write_text(
            json.dumps(maps[filename], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        output_hashes[filename] = sha256(path)
    name_document = load_json(root / NAME_INPUT_FILE)
    name_output = output / NAME_INPUT_FILE
    name_output.write_text(
        json.dumps(name_document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    output_hashes[NAME_INPUT_FILE] = sha256(name_output)

    report = {
        "status": "pass",
        "schema": document["schema"],
        "master": str(master),
        "master_sha256": sha256(master),
        "overrides": str(overrides_path),
        "overrides_sha256": sha256(overrides_path),
        "input_root": str(root),
        "output_root": str(output),
        "context_rules": rule_reports,
        "payload_overrides": payload_reports,
        "dialogue_overrides": dialogue_reports,
        "changed_record_count": len(changed_ids),
        "changed_record_ids": sorted(changed_ids),
        "internal_placeholder_residual_count": len(placeholder_residuals),
        "japanese_script_residual_count": len(japanese_residuals),
        "map_record_counts": {name: len(maps[name]) for name in MAP_FILES},
        "output_sha256": output_hashes,
    }
    report_path = output / "translation_quality_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "status": "pass",
        "changed_record_count": len(changed_ids),
        "context_rule_count": len(rule_reports),
        "payload_override_count": len(payload_reports),
        "dialogue_override_count": len(dialogue_reports),
        "output": str(output),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
