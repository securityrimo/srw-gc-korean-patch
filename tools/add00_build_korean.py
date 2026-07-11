#!/usr/bin/env python3
"""Build the fixed-layout Korean add00 graphic patch.

Safe automatic scope:

* 79 scenario-title bitmaps (blocks 2716..2950, step 3).  Their Japanese,
  English, and existing Korean translations align exactly with add02 block 33
  records 1..79.
* 203 location-caption bitmaps (blocks 3513..3917, step 2).  Japanese text is
  reconstructed with each retail SCR tile map; English is OCR'd from the
  public patch and translated to Korean with a resumable cache.

Every patched image keeps the English BMP6 dimensions/header/byte length.  No
SCR, SPR, C8 logo, or outer pointer is changed.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import struct
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont
from rapidfuzz import fuzz, process

import add00_tools
import add02_dol_tools


STORY_BLOCKS = tuple(range(2716, 2951, 3))
LOCATION_BLOCKS = tuple(range(3513, 3918, 2))
SAFE_BLOCKS = set(STORY_BLOCKS) | set(LOCATION_BLOCKS)
JAPANESE_RE = re.compile(r"[\u3041-\u3096\u30a1-\u30fa\u3400-\u9fff]")
KOREAN_RE = re.compile(r"[\u3131-\u318e\uac00-\ud7a3]")
MARKER_RE = re.compile(r"__ADD00_([0-9A-F]{4})__")


OCR_CORRECTIONS = {
    "PIanet": "Planet",
    "PIains": "Plains",
    "PIaza": "Plaza",
    "PaIace": "Palace",
    "RoyaI": "Royal",
    "HaII": "Hall",
    "MiIitary": "Military",
    "MobiIe": "Mobile",
    "LittIe": "Little",
    "BuiIding": "Building",
    "EIementary": "Elementary",
    "AIIeyway": "Alleyway",
    "AIIiance": "Alliance",
    "CIass": "Class",
    "lndustries": "Industries",
    "lnterior": "Interior",
    "lnfirmary": "Infirmary",
    "W00d5": "Woods",
    "M0thership": "Mothership",
    "MåP": "Map",
    "FO 「 ce": "Force",
    "FO 「 ce": "Force",
    "Co 「 ⅱ do 「": "Corridor",
    "Co ido 「": "Corridor",
    "Co 「 ⅱ do 「": "Corridor",
    "Co 「 Ⅱ do 「": "Corridor",
    " 「": "r",
    "れ": "n",
    "冂": "n",
    "ⅲ": " in ",
    "ⅱ": "i",
    "′": "'",
    "0uS00": "Cusco",
    "Cor ii dor": "Corridor",
    "COr ii dor": "Corridor",
    "GustgaI": "Gustgal",
    "RebeI": "Rebel",
    "ViIlage": "Village",
    "CentraI": "Central",
    "HimaIayas": "Himalayas",
    "BardIand": "Bardland",
    "BaIcony": "Balcony",
    "SoIomon": "Solomon",
    "JungIe": "Jungle",
    "GuerriIIa": "Guerrilla",
    "WorId": "World",
    "HeII": "Hell",
    "CastIe": "Castle",
    "SeattIe": "Seattle",
    "BattIeship": "Battleship",
    "WasteIand": "Wasteland",
    "HalI": "Hall",
    "PoseidaI": "Poseidal",
    "lcelina": "Icelina",
}


LOCATION_ENGLISH_OVERRIDES = {
    3513: "Ondoron's Space Fortress",
    3543: "Japan: Ruins",
    3547: "Antarctica: Dinosaur Empire HQ",
    3597: "Luna II: Control Room",
    3607: "White Base: Corridor",
    3639: "Big Tray: Meeting Room",
    3651: "Party Hall: Outside",
    3677: "Side 3: Royal Palace",
    3689: "Side 7 Space Sector",
    3693: "Side 6: Town",
    3697: "Side 6: Town",
    3699: "Side 6: Port",
    3701: "Side 6: Woods",
    3703: "Side 6: White Base Port",
    3707: "Side 3: Royal Palace",
    3711: "Sarge Opus: Bridge",
    3713: "Colony Passageway",
    3723: "Gandor: Quarters",
    3727: "Gandor: Large Room",
    3737: "Gustgal Capital: Sveto",
    3745: "Icelina's House",
    3753: "A Baoa Qu",
    3755: "J9 Base",
    3759: "Pentagona World: Asteroid",
    3763: "Theart Star: Corridor",
    3769: "EFF: Far East Branch",
    3787: "Hell Castle",
    3799: "Emperor Gol's Hall",
    3811: "Chongqing Base: Airport",
    3813: "Chongqing Base: Hangar",
    3819: "Seattle: Ruins",
    3867: "Castle Muge",
    3869: "Belzeb's Mothership",
    3881: "Jaburo: Briefing Room",
    3885: "Al's House",
    3913: "Cyber Beast Base: Corridor",
}


# The supplied translation set contains the game's canonical location table in
# add02dat/1other_8.csv (CSV rows 798..1004).  OCR is noisy, so these overrides
# pin low-confidence assets to the correct existing row.  All other rows are
# accepted only after a normalized Japanese fuzzy match of at least 75%.
LOCATION_LEGACY_ROW_OVERRIDES = {
    3515: 995, 3521: 891, 3523: 897, 3539: 849, 3541: 848,
    3563: 850, 3573: 952, 3605: 956, 3607: 856, 3627: 853,
    3629: 942, 3635: 830, 3657: 920, 3661: 881, 3665: 882,
    3667: 883, 3669: 885, 3671: 887, 3683: 905, 3711: 971,
    3719: 979, 3741: 817, 3757: 1002, 3775: 844, 3779: 974,
    3805: 929, 3807: 981, 3809: 893, 3819: 808, 3855: 939,
    3859: 969, 3865: 953, 3867: 954, 3871: 1004, 3885: 917,
    3895: 884, 3897: 898, 3901: 821,
}


# Captions which are not exact members of the legacy location table.  These
# are short deterministic translations based on the reconstructed Japanese
# asset and the public English graphic, not fresh machine translations.
LOCATION_MANUAL = {
    3525: ("連邦軍基地　ジャブロー", "연방군 자브로 기지"),
    3529: ("空港", "공항"),
    3531: ("防衛隊本部", "방위대 본부"),
    3543: ("日本　廃墟", "일본 폐허"),
    3545: ("日本", "일본"),
    3555: ("広場", "광장"),
    3561: ("船室", "선실"),
    3565: ("雪山", "설산"),
    3567: ("最上重工周辺", "최상중공 주변"),
    3571: ("真ゲッターロボ実験場", "진 겟타로보 실험장"),
    3575: ("秋水重工そばの公園", "아키미 중공 인근 공원"),
    3585: ("中央アジア上空", "중앙아시아 상공"),
    3587: ("基地　部屋", "기지 내부 방"),
    3609: ("ホワイトベース　内部", "화이트 베이스 내부"),
    3615: ("ホワイトベース　大部屋", "화이트 베이스 큰 방"),
    3617: ("ホワイトベース　船室", "화이트 베이스 선실"),
    3631: ("ポセイダル軍基地　格納庫", "포세이달군 기지 격납고"),
    3647: ("バードランド", "버드랜드"),
    3649: ("パーティー会場　ベランダ", "연회장 발코니"),
    3659: ("スヴェート", "스베이트"),
    3679: ("ジオン軍秘密実験場", "지온군 비밀 실험장"),
    3685: ("ザンジバル　艦内", "잔지바르 함내"),
    3691: ("サイド７", "사이드 7"),
    3703: ("サイド６　ホワイトベースの港", "사이드 6 화이트 베이스 항구"),
    3723: ("ガンドール　部屋", "간도르 방"),
    3727: ("ガンドール　大部屋", "간도르 큰 방"),
    3745: ("イセリナ家", "이세리나의 집"),
    3767: ("ギワザ艦　ブリッジ", "기와자 함 브리지"),
    3769: ("地球連邦極東支部", "지구연방 극동지부"),
    3795: ("日本　山間部", "일본 산간부"),
    3823: ("クスコ　レジスタンス基地", "쿠스코 레지스탕스 기지"),
    3825: ("連邦軍基地", "연방군 기지"),
    3833: ("連邦軍基地", "연방군 기지"),
    3839: ("エドン星", "에돈행성"),
    3841: ("エドン星　街", "에돈행성 거리"),
    3869: ("ベルゼブ艦", "벨제브 함"),
    3881: ("ジャブロー　ブリーフィングルーム", "자브로 브리핑룸"),
    3903: ("新惑星連合戦艦", "신행성연합전함"),
    3905: ("ガディソード前衛要塞", "가디소드 전위요새"),
    3907: ("グワジン　ブリッジ", "그와진 브리지"),
    3909: ("グワジン　艦内", "그와진 함내"),
    3911: ("ア・バオア・クー宙域", "아・바오아・쿠 주역"),
    3913: ("獣戦機隊基地　通路", "수전기대기지 통로"),
}


ENGLISH_TERMS = {
    "White Base": "화이트 베이스",
    "Saotome Lab": "사오토메 연구소",
    "Photon Power Lab": "광자력 연구소",
    "Gadisword": "가디소드",
    "Jaburo": "자브로",
    "Gustgal": "가스트갈",
    "Gandor": "간도르",
    "Grados": "그라도스",
    "Giganos": "기가노스",
    "Gowahand": "고와핸드",
    "Midorigaoka": "미도리가오카",
    "Takeo General Company": "타케오 제너럴 컴퍼니",
    "Best Heavy Industries": "모가미 중공",
    "Ondoron": "온드론",
    "Little Saye": "리틀 세이",
    "A Baoa Qu": "아 바오아 쿠",
    "Side 3": "사이드 3",
    "Side 6": "사이드 6",
    "Side 7": "사이드 7",
    "Luna II": "루나 II",
    "Nubia": "누비아",
    "Zarl": "자르",
    "Muge": "무게",
    "Edon": "에돈",
    "Gustgal": "가스트갈",
    "Svet0": "스베토",
    "Sveto": "스베토",
}


def read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def clean_ocr(text: str) -> str:
    output = unicodedata.normalize("NFKC", text or "")
    output = re.sub(r"\s+", " ", output).strip()
    for source, target in OCR_CORRECTIONS.items():
        output = output.replace(source, target)
    # Windows OCR frequently reads lowercase Latin l as capital I inside a
    # word. Roman numerals such as "Luna II" are surrounded by spaces and are
    # therefore not affected by this conservative repair.
    output = re.sub(r"(?<=[a-z])I(?=[a-z]|\b)", "l", output)
    output = re.sub(r"\s+", " ", output).strip(" _")
    return output


def clean_japanese_ocr(text: str) -> str:
    # Windows OCR inserts spaces between every Japanese glyph in these pixel
    # titles.  The mapped source contains no intentional ASCII spacing.
    return re.sub(r"[\s\u3000]+", "", unicodedata.normalize("NFKC", text or ""))


def japanese_match_key(text: str) -> str:
    return re.sub(
        r"[\s\u3000・･:：、。,.\-_'’]",
        "",
        unicodedata.normalize("NFKC", text or ""),
    )


def _translate_google(text: str, source_language: str = "en", timeout: int = 45) -> str:
    payload = urllib.parse.urlencode(
        {"client": "gtx", "sl": source_language, "tl": "ko", "dt": "t", "q": text}
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://translate.googleapis.com/translate_a/single",
        data=payload,
        headers={"User-Agent": "Mozilla/5.0 SRW-GC-Korean-Patch/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.load(response)
        return "".join(part[0] or "" for part in data[0])
    except urllib.error.HTTPError as error:
        if error.code != 429:
            raise
        mobile_url = (
            f"https://translate.google.com/m?sl={source_language}&tl=ko&q="
            + urllib.parse.quote(text, safe="")
        )
        mobile_request = urllib.request.Request(
            mobile_url, headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(mobile_request, timeout=timeout) as response:
            page = response.read().decode("utf-8", errors="replace")
        match = re.search(r'class="result-container">(.*?)</div>', page, re.DOTALL)
        if not match:
            raise ValueError("mobile translation response lacked a result container")
        return html.unescape(match.group(1))


def _parse_batch(text: str, markers: list[str]) -> list[str] | None:
    matches = list(MARKER_RE.finditer(text))
    if [match.group(1) for match in matches] != markers:
        return None
    result: list[str] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        result.append(text[match.end() : end].strip())
    return result


def _protect_terms(text: str) -> tuple[str, dict[str, str]]:
    output = text
    protected: dict[str, str] = {}
    serial = 0
    for source, korean in sorted(ENGLISH_TERMS.items(), key=lambda item: -len(item[0])):
        pattern = re.compile(re.escape(source), re.IGNORECASE)
        if not pattern.search(output):
            continue
        marker = f"ZXQ{serial:03d}QXZ"
        output = pattern.sub(marker, output)
        protected[marker] = korean
        serial += 1
    return output, protected


def _restore_terms(text: str, protected: dict[str, str]) -> str:
    output = text
    for marker, korean in protected.items():
        variants = {marker, marker.lower(), marker.replace("QXZ", " QXZ")}
        for variant in variants:
            output = output.replace(variant, korean)
    return output


def translate_many_english(texts: Iterable[str], cache_path: Path) -> dict[str, str]:
    unique = list(dict.fromkeys(text for text in texts if text))
    cache: dict[str, str] = (
        json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    )
    pending = [text for text in unique if text not in cache]
    batches = [pending[index : index + 20] for index in range(0, len(pending), 20)]
    for batch_number, batch in enumerate(batches):
        markers = [f"{batch_number:02X}{index:02X}" for index in range(len(batch))]
        protected_rows: list[dict[str, str]] = []
        masked_rows: list[str] = []
        for source in batch:
            masked, protected = _protect_terms(source)
            masked_rows.append(masked)
            protected_rows.append(protected)
        payload = "\n".join(
            f"__ADD00_{marker}__\n{source}"
            for marker, source in zip(markers, masked_rows)
        )
        translated: list[str] | None = None
        for attempt in range(5):
            try:
                translated = _parse_batch(_translate_google(payload), markers)
                if translated is not None:
                    break
            except (OSError, urllib.error.URLError, ValueError, IndexError):
                pass
            time.sleep(1.2 * (attempt + 1))
        if translated is None:
            translated = []
            for source in masked_rows:
                for attempt in range(5):
                    try:
                        translated.append(_translate_google(source))
                        break
                    except (OSError, urllib.error.URLError, ValueError, IndexError):
                        time.sleep(1.5 * (attempt + 1))
                else:
                    raise RuntimeError(f"English translation failed: {source!r}")
        for source, target, protected in zip(batch, translated, protected_rows):
            target = _restore_terms(target, protected)
            target = re.sub(r"\s+", " ", html.unescape(target)).strip()
            cache[source] = target
        write_json(cache_path, dict(sorted(cache.items())))
        time.sleep(0.1)
    return {text: cache[text] for text in unique}


def _split_balanced(text: str, line_count: int) -> list[str]:
    if line_count <= 1:
        return [text]
    words = text.split()
    if len(words) >= line_count:
        lines: list[str] = []
        remaining = words[:]
        for line_index in range(line_count - 1):
            target = sum(len(word) for word in remaining) / (line_count - line_index)
            current: list[str] = []
            length = 0
            while remaining and (not current or length + 1 + len(remaining[0]) <= target + 1):
                word = remaining.pop(0)
                current.append(word)
                length += len(word) + (1 if len(current) > 1 else 0)
            lines.append(" ".join(current))
        lines.append(" ".join(remaining))
        return lines
    # Proper nouns occasionally translate without spaces.  Split by Hangul
    # syllable count only when the original asset clearly used multiple rows.
    chunk = math.ceil(len(text) / line_count)
    return [text[index : index + chunk] for index in range(0, len(text), chunk)]


def render_korean(text: str, size: tuple[int, int], original_line_count: int) -> tuple[Image.Image, dict[str, object]]:
    width, height = size
    canvas = Image.new("L", size, 0)
    draw = ImageDraw.Draw(canvas)
    font_path = Path(r"C:\Windows\Fonts\malgunbd.ttf")
    if not font_path.exists():
        font_path = Path(r"C:\Windows\Fonts\gulim.ttc")
    max_lines = max(1, min(3, original_line_count or round(height / 48)))
    best: tuple[int, list[str], ImageFont.FreeTypeFont, int, int] | None = None
    # Prefer the source line count, but allow one fewer line when Korean fits
    # at a visibly larger size.
    line_options = list(dict.fromkeys([max_lines] + list(range(1, max_lines + 1))))
    for line_count in line_options:
        lines = _split_balanced(text, line_count)
        if len(lines) > max_lines:
            continue
        for font_size in range(min(32, max(12, height // len(lines))), 9, -1):
            font = ImageFont.truetype(str(font_path), font_size)
            spacing = max(0, font_size // 7)
            boxes = [draw.textbbox((0, 0), line, font=font, stroke_width=0) for line in lines]
            text_width = max(box[2] - box[0] for box in boxes)
            text_height = sum(box[3] - box[1] for box in boxes) + spacing * (len(lines) - 1)
            if text_width <= width - 4 and text_height <= height - 4:
                candidate = (font_size, lines, font, text_width, text_height)
                if best is None or candidate[0] > best[0] or (
                    candidate[0] == best[0] and len(candidate[1]) < len(best[1])
                ):
                    best = candidate
                break
    if best is None:
        raise ValueError(f"cannot fit Korean text {text!r} into {size}")
    font_size, lines, font, _, text_height = best
    spacing = max(0, font_size // 7)
    y = (height - text_height) // 2
    for line in lines:
        box = draw.textbbox((0, 0), line, font=font)
        line_width = box[2] - box[0]
        line_height = box[3] - box[1]
        x = (width - line_width) // 2
        # The English patch uses a low-intensity one-pixel drop shadow.
        draw.text((x + 1, y + 1 - box[1]), line, font=font, fill=72)
        draw.text((x, y - box[1]), line, font=font, fill=255)
        y += line_height + spacing
    return canvas, {"font": str(font_path), "font_size": font_size, "lines": lines}


def build(args: argparse.Namespace) -> dict[str, object]:
    source_document = read_json(args.records)
    records = source_document["records"]
    by_block = {int(record["block_index"]): record for record in records}
    english_ocr = {int(row["block_index"]): row for row in read_json(args.english_ocr)["records"]}
    japanese_ocr = {int(row["block_index"]): row for row in read_json(args.japanese_ocr)["records"]}

    original_add02 = {
        row["id"]: row for row in add02_dol_tools.extract_records(args.original_add02)
    }
    english_add02 = {
        row["id"]: row for row in add02_dol_tools.extract_records(args.english_add02)
    }
    master_document = read_json(args.master)
    master = {row["id"]: row for row in master_document["records"]}
    legacy_locations = {
        int(row["csv_row"]): row
        for row in master_document["legacy_translation_rows"]
        if row["source_file"].endswith("1other_8.csv")
        and 798 <= int(row["csv_row"]) <= 1004
        and row.get("japanese")
        and row.get("korean")
    }
    legacy_by_key = {
        japanese_match_key(row["japanese"]): row for row in legacy_locations.values()
    }
    legacy_choices = list(legacy_by_key)

    # Scenario graphic order is exactly add02 block 33 record 1..79.
    for ordinal, block in enumerate(STORY_BLOCKS, start=1):
        stable_id = f"add02:b033:r{ordinal:04d}:f0"
        record = by_block[block]
        record["japanese_graphic_text"] = original_add02[stable_id]["japanese"].strip()
        record["english_ocr"] = english_add02[stable_id]["japanese"].strip()
        record["final_korean"] = master[stable_id]["final_korean"].strip()
        record["translation_source"] = "existing_master_add02_stable_id"
        record["translation_reference_id"] = stable_id

    location_sources: list[str] = []
    for block in LOCATION_BLOCKS:
        record = by_block[block]
        english = LOCATION_ENGLISH_OVERRIDES.get(
            block, clean_ocr(english_ocr[block]["text"])
        )
        japanese = clean_japanese_ocr(japanese_ocr[block]["text"])
        record["english_ocr"] = english
        record["japanese_graphic_text"] = japanese
        location_sources.append(english)
    translated = translate_many_english(location_sources, args.translation_cache)
    for block in LOCATION_BLOCKS:
        record = by_block[block]
        record["japanese_graphic_ocr"] = record["japanese_graphic_text"]
        if block in LOCATION_MANUAL:
            japanese, korean = LOCATION_MANUAL[block]
            record["japanese_graphic_text"] = japanese
            record["final_korean"] = korean
            record["existing_korean"] = ""
            record["translation_source"] = "manual_graphic_caption"
            record["translation_reference_id"] = ""
            record["legacy_match_confidence"] = 0.0
            continue

        legacy_row = None
        score = 0.0
        if block in LOCATION_LEGACY_ROW_OVERRIDES:
            legacy_row = legacy_locations[LOCATION_LEGACY_ROW_OVERRIDES[block]]
            score = 1.0
        else:
            key = japanese_match_key(record["japanese_graphic_text"])
            if key:
                found = process.extractOne(key, legacy_choices, scorer=fuzz.ratio)
                if found and found[1] >= 75.0:
                    legacy_row = legacy_by_key[found[0]]
                    score = found[1] / 100.0
        if legacy_row is not None:
            record["matched_legacy_japanese"] = legacy_row["japanese"]
            record["existing_korean"] = legacy_row["korean"]
            record["final_korean"] = legacy_row["korean"]
            record["translation_source"] = "existing_legacy_location"
            record["translation_reference_id"] = (
                f"legacy:1other_8.csv:{legacy_row['csv_row']}"
            )
            record["translation_provenance"] = {
                "file": legacy_row["source_file"],
                "row": legacy_row["csv_row"],
                "offset": legacy_row["csv_offset"],
            }
            record["legacy_match_confidence"] = score
        else:
            record["final_korean"] = translated[record["english_ocr"]]
            record["existing_korean"] = ""
            record["translation_source"] = "machine_translated_english_graphic"
            record["translation_reference_id"] = ""
            record["legacy_match_confidence"] = 0.0

    # Populate audit text for non-patched changed BMP6 atlases.
    for block, record in by_block.items():
        if not record.get("english_ocr"):
            record["english_ocr"] = clean_ocr(english_ocr[block]["text"])
        if not record.get("japanese_graphic_text"):
            record["japanese_graphic_text"] = clean_japanese_ocr(japanese_ocr[block]["text"])
        if block not in SAFE_BLOCKS:
            record["final_korean"] = ""
            record["translation_source"] = ""
            record["translation_reference_id"] = ""
            record["patch_status"] = "preserved_english_uncertain_graphic_atlas"

    english_container = add00_tools.parse_container(args.english_add00)
    korean_dir = args.graphic_dir / "korean"
    korean_dir.mkdir(parents=True, exist_ok=True)
    replacement_images: dict[int, Path] = {}
    render_details: dict[int, dict[str, object]] = {}
    for block in sorted(SAFE_BLOCKS):
        record = by_block[block]
        korean = record["final_korean"].strip()
        if not korean:
            raise ValueError(f"safe block {block} has no Korean translation")
        if JAPANESE_RE.search(korean):
            raise ValueError(f"safe block {block} Korean still contains Japanese: {korean!r}")
        image = add00_tools.decode_i4(english_container.blocks[block])
        line_count = max(1, len(english_ocr[block].get("lines", [])))
        rendered, details = render_korean(korean, image.size, line_count)
        output_path = korean_dir / f"add00_{block:04d}_ko.png"
        rendered.save(output_path)
        replacement_images[block] = output_path
        render_details[block] = details
        record["korean_preview"] = str(output_path)
        record["render"] = details
        record["patch_status"] = "patched_korean_fixed_layout"

    patched = add00_tools.patch_images(args.english_add00, replacement_images)
    args.output.write_bytes(patched)
    validation = add00_tools.verify_fixed_layout(args.english_add00, patched)
    validation.update(
        {
            "expected_english_size": 149_942_304,
            "size_matches_expected": len(patched) == 149_942_304,
            "scenario_titles_patched": len(STORY_BLOCKS),
            "location_captions_patched": len(LOCATION_BLOCKS),
            "total_korean_bmp6_blocks": len(SAFE_BLOCKS),
            "japanese_in_final_korean_fields": sum(
                bool(JAPANESE_RE.search(str(record.get("final_korean", "")))) for record in records
            ),
            "empty_safe_translations": sum(
                not str(by_block[block].get("final_korean", "")).strip() for block in SAFE_BLOCKS
            ),
        }
    )
    if not validation["size_matches_expected"]:
        raise ValueError("output does not match the required English add00 byte size")

    original_container = add00_tools.parse_container(args.original_add00)
    complex_records = []
    for index in (3466, 3467, 3506):
        complex_records.append(
            {
                "id": f"add00:complex:{index:04d}",
                "block_index": index,
                "block_type": original_container.blocks[index][:4].hex().upper(),
                "original_block_sha256": add00_tools.sha256(original_container.blocks[index]),
                "english_block_sha256": add00_tools.sha256(english_container.blocks[index]),
                "japanese_graphic_text": "スーパーロボット大戦" if index == 3467 else "",
                "english_graphic_text": "Super Robot Wars" if index == 3467 else "",
                "final_korean": "",
                "patch_status": "preserved_english_complex_logo_topology",
                "reason": (
                    "BMP9/C8 logo and linked SPR topology changes size and tile indices; "
                    "the English asset removes all Japanese while preserving known-good runtime layout."
                ),
            }
        )

    final_document = {
        "schema": "srw-gc-add00-graphic-korean-v1",
        "metadata": {
            "original_add00": str(args.original_add00.resolve()),
            "english_template_add00": str(args.english_add00.resolve()),
            "korean_add00": str(args.output.resolve()),
            "fixed_layout": True,
            "outer_block_count": len(english_container.blocks),
            "english_template_size": len(english_container.source),
            "policy": (
                "Patch only independently readable one-caption BMP6 assets; preserve public-English "
                "assets for uncertain atlases and complex logo resources so Japanese does not return."
            ),
        },
        "statistics": {
            "changed_bmp6_records": len(records),
            "scenario_titles_patched": len(STORY_BLOCKS),
            "location_captions_patched": len(LOCATION_BLOCKS),
            "total_korean_bmp6_blocks": len(SAFE_BLOCKS),
            "preserved_english_bmp6_blocks": len(records) - len(SAFE_BLOCKS),
            "complex_logo_records": len(complex_records),
        },
        "records": records,
        "complex_graphic_records": complex_records,
    }
    write_json(args.final_records, final_document)
    write_json(
        args.replacements,
        {
            "schema": "srw-gc-add00-replacements-v1",
            "template": str(args.english_add00.resolve()),
            "output": str(args.output.resolve()),
            "records": [
                {
                    "id": by_block[block]["id"],
                    "block_index": block,
                    "japanese": by_block[block]["japanese_graphic_text"],
                    "english": by_block[block]["english_ocr"],
                    "korean": by_block[block]["final_korean"],
                    "png": by_block[block]["korean_preview"],
                }
                for block in sorted(SAFE_BLOCKS)
            ],
        },
    )
    write_json(args.validation, validation)
    return validation


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parent
    workspace = root.parent
    parser.add_argument("--original-add00", type=Path, default=workspace / "srw_gc_iso_extract/original/add00dat.bin")
    parser.add_argument("--english-add00", type=Path, default=workspace / "srw_gc_iso_extract/english/add00dat.bin")
    parser.add_argument("--original-add02", type=Path, default=workspace / "srw_gc_iso_extract/original/add02dat.bin")
    parser.add_argument("--english-add02", type=Path, default=workspace / "srw_gc_iso_extract/english/add02dat.bin")
    parser.add_argument("--master", type=Path, default=root / "srw_gc_all_japanese_korean_master.json")
    parser.add_argument("--records", type=Path, default=root / "add00_graphic_records.json")
    parser.add_argument("--english-ocr", type=Path, default=root / "add00_english_ocr.json")
    parser.add_argument("--japanese-ocr", type=Path, default=root / "add00_japanese_ocr.json")
    parser.add_argument("--graphic-dir", type=Path, default=root / "add00_graphics")
    parser.add_argument("--translation-cache", type=Path, default=root / "add00_english_translation_cache.json")
    parser.add_argument("--output", type=Path, default=root / "add00dat.complete_ko.fixed_layout.bin")
    parser.add_argument("--final-records", type=Path, default=root / "add00_graphic_records.final.json")
    parser.add_argument("--replacements", type=Path, default=root / "add00_replacements.json")
    parser.add_argument("--validation", type=Path, default=root / "add00_validation_report.json")
    args = parser.parse_args(argv)
    report = build(args)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
