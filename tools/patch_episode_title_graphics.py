"""Patch reviewed Korean scenario-title BMP6 graphics in a fixed-layout add00.

The title captions are standalone GX I4 bitmaps, not members of the shared UI
atlases.  This tool redraws only titles listed in the PDF translation-quality
override document.  Every block header, block size, outer offset, and unrelated
block remains byte-identical.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import add00_tools


REPOSITORY_DIR = Path(__file__).resolve().parent.parent
EXPECTED_SOURCE_SHA256 = "ED9E9F8AAD82D81CB2C9A7B1C8855D3F439CD90F65370D57086C9B3A528671D4"
EXPECTED_FONT_SHA256 = "E8CBC0B2AFCC14FB45DFB6086D5102C0B23A96E7B6E708F3122ACDE1B86C9082"
EXPECTED_TITLE_COUNT = 39
EXPECTED_TITLE_BLOCKS = {
    11: 2746,
    12: 2749,
    14: 2755,
    15: 2758,
    17: 2764,
    18: 2767,
    22: 2779,
    25: 2788,
    26: 2791,
    27: 2794,
    28: 2797,
    29: 2800,
    32: 2809,
    33: 2812,
    34: 2815,
    36: 2821,
    37: 2824,
    38: 2827,
    39: 2830,
    40: 2833,
    41: 2836,
    42: 2839,
    43: 2842,
    44: 2845,
    45: 2848,
    46: 2851,
    47: 2854,
    49: 2860,
    50: 2863,
    51: 2866,
    52: 2869,
    53: 2872,
    54: 2875,
    55: 2878,
    56: 2881,
    57: 2884,
    58: 2887,
    59: 2890,
    60: 2893,
}
EXPECTED_TITLE_SOURCE_BLOCK_SHA256 = {
    2746: "247E861C144519AD9DBE0B30C90CDAC4DEB59B606A13E60B16E6AF9A561445A7",
    2749: "D6C1DEDD1D68D67CFF01CEBF4B9A74CBAD8F09C68E9B804F274704BF96BA5E18",
    2755: "7D363561D446A9BAEA84ACDC7090BE951D96080EF17CD0325D7309E63AEDA2F8",
    2758: "9DAEAF876B9CF54E17F68286363AAF1601A3452A3D8248FF804FFA996AED8DB0",
    2764: "5D1D48B4689DC8036F93561572553B159C087055A4A88A55EB32B9BFFBB5D816",
    2767: "4440BA5F9BFF3813FB0EC3A6CE70A9E0306F32662F4B863BDD67FC4CE8E7E84A",
    2779: "2BF0183E215B3CA9D3E6B99E2E444C9BBC4E934D5868F9ACCBBB35D2D9B675A0",
    2788: "9F730247636D5D40399CB59ED140B94F11DFBE1CB863709E1C137AB7477E4DAB",
    2791: "0B0E4D58DA1265F77BBE67B40FDC92F738E21EB5B0BA6A35F71A839BEABC71F6",
    2794: "3349F0CDAF7E22DC31859951C369DC6DDE60C9FFA445061A6D7E7184F5B94B6B",
    2797: "1193F37BADD07300B464C5DD4C08243CA0150BFB750E44A498DF5BE3B5F5BB60",
    2800: "CAC845C3CB748D33EC191C07F05AD4C5AE2BD8FF6ADB01C038AC4C6C7C948206",
    2809: "C3BE079D4767AD30CAF788E2178FFBB1E82272963ED97D19CE03AE806591B933",
    2812: "1068BF9ABDCBFFEFEEFC170CD5E7064B8FB5C65B2C805539C1C06D9BD6BAF6A0",
    2815: "788EBE764E880435437BCDE47E4B76BE7BDF354FC1BE33110A18CD84C74FE853",
    2821: "755241B9502DE687DD7D94913E815245A94AF9BC4E967C351959496467BA4DBF",
    2824: "FD68970A9FA35119FECB2D6FE455AB2F4F7C95A8F1AD5BB910320F3B3F814B53",
    2827: "B4902C3874A34303D970708688B3BE71C84A9C031BCEABDA58F27BA030A2B827",
    2830: "0195EEA93CF0397D6FA1D179B99035D024F84F74C033354C44C75B73FD4C0709",
    2833: "766221ED43505AF7E0421D7E3FEF6FB84154FD5913AFF6B6B4A2B5E5C5364933",
    2836: "509B25F9804B61AA46F38673697B8B5FB54E89EE46BAA3D596AB1847A69D2F03",
    2839: "EF7E3EB336BFA1BCDB94A999159B001C850103B47588F498C3C499092EF68FF2",
    2842: "95B8F445C50572A7454A70199E9F0A10A5DDA8EF3CA0085F697990106E0F5202",
    2845: "84B8C8DB5A8283AD3EFBB5EB5085F3378FCC49B57390E257A7795CE3E2B800B6",
    2848: "0D70D15508486526A725156853BD9CED2DC31C7C3BDBD9A402AA598409D418FF",
    2851: "0B338FCC816B163EA87EB553CF6CA8091EC0F65AA39FA9948AFA9AE5E83035C4",
    2854: "D195F8548A7643ED3098DFEECE3CD525DAE47EB117B7357F6BB3023F53A430B0",
    2860: "EA3109A463B78E64D362FED0168CFFC1CE1FCFB3B906B9BE458BCFF0D433E9D4",
    2863: "528D8837B5CA36A39B50D9E3107B2053CD3E80037CCF379A9034A70EC0255D44",
    2866: "E9BBBA7D294423DD25D7633B3AAC55F8CC252D888C0408EA7F3E96E2549BED1C",
    2869: "D2EF1567E4C3E8B146658B99CB2A32DD09970490BCE811F8D7F602FFC9577CB2",
    2872: "CBED713D0E7761178426A984CDA9E1DB9171A40459F438229ACEA0459D8565F7",
    2875: "00DA61B0C662CAFE339E2CE938566A054F108FA4C882E126BF7532641E655596",
    2878: "41957C1F55E9E7FA169F710D7340AD12021DEC06674F5288D5D4210C2DA09885",
    2881: "A11B216C41461349AD3395FF54B7F3FFB7BE5EF31B8AAF5400A3107DB185DCF4",
    2884: "20498DE878A1FAEDB04040E9C153A17A176A0B4B3AD212E4030913DC4844A5E6",
    2887: "D2C2D24364427616C1502C797BAE575E3F2332B04BB130F850E6F6C50E1D9C77",
    2890: "C754B21BCBC9836395B00B4CA8DDBC7B54B7CBC105DA7AFC0C6D6BF9081EE3A3",
    2893: "85524EFAF705300FAC19A7B47A902A382E6E419DCBE9174B6697CF2FC690EF59",
}
JAPANESE_RE = re.compile(
    r"[\u3005\u303b\u3041-\u3096\u309d-\u309e\u30a1-\u30fa"
    r"\u30fc-\u30fe\u3400-\u9fff\uff66-\uff9f]"
)


def sha256(data_or_path: bytes | Path) -> str:
    if isinstance(data_or_path, Path):
        data = data_or_path.read_bytes()
    else:
        data = data_or_path
    return hashlib.sha256(data).hexdigest().upper()


def split_balanced(text: str, line_count: int) -> list[str]:
    if line_count <= 1:
        return [text]
    words = text.split()
    if len(words) >= line_count:
        lines: list[str] = []
        remaining = list(words)
        for line_index in range(line_count - 1):
            remaining_chars = sum(len(word) for word in remaining) + max(0, len(remaining) - 1)
            target = max(1, round(remaining_chars / (line_count - line_index)))
            current: list[str] = []
            length = 0
            while remaining:
                word = remaining[0]
                proposed = length + len(word) + (1 if current else 0)
                if current and proposed > target:
                    break
                current.append(remaining.pop(0))
                length = proposed
            lines.append(" ".join(current))
        lines.append(" ".join(remaining))
        return lines
    chunk = math.ceil(len(text) / line_count)
    return [text[index : index + chunk] for index in range(0, len(text), chunk)]


def render_title(text: str, size: tuple[int, int], font_path: Path) -> tuple[Image.Image, dict[str, object]]:
    width, height = size
    canvas = Image.new("L", size, 0)
    draw = ImageDraw.Draw(canvas)
    max_lines = 2 if height >= 40 else 1
    best: tuple[int, list[str], ImageFont.FreeTypeFont, int] | None = None
    for line_count in range(1, max_lines + 1):
        lines = split_balanced(text, line_count)
        for font_size in range(min(32, max(12, height // len(lines))), 9, -1):
            font = ImageFont.truetype(str(font_path), font_size)
            spacing = max(0, font_size // 7)
            boxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
            text_width = max(box[2] - box[0] for box in boxes)
            text_height = sum(box[3] - box[1] for box in boxes) + spacing * (len(lines) - 1)
            if text_width <= width - 4 and text_height <= height - 4:
                candidate = (font_size, lines, font, text_height)
                if best is None or font_size > best[0] or (
                    font_size == best[0] and len(lines) < len(best[1])
                ):
                    best = candidate
                break
    if best is None:
        raise ValueError(f"cannot fit scenario title {text!r} into {size}")

    font_size, lines, font, text_height = best
    spacing = max(0, font_size // 7)
    y = (height - text_height) // 2
    for line in lines:
        box = draw.textbbox((0, 0), line, font=font)
        x = (width - (box[2] - box[0])) // 2
        draw.text((x + 1, y + 1 - box[1]), line, font=font, fill=72)
        draw.text((x, y - box[1]), line, font=font, fill=255)
        y += box[3] - box[1] + spacing
    return canvas, {"font_size": font_size, "lines": lines, "dimensions": [width, height]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--preview-dir", type=Path)
    parser.add_argument(
        "--overrides",
        type=Path,
        default=REPOSITORY_DIR / "data" / "pdf_translation_quality_overrides.json",
    )
    parser.add_argument("--font", type=Path, default=Path(r"C:\Windows\Fonts\malgunbd.ttf"))
    args = parser.parse_args()

    source = args.source.resolve()
    output = args.output.resolve()
    report_path = args.report.resolve()
    overrides_path = args.overrides.resolve()
    font = args.font.resolve()
    paths = [source, output, report_path, overrides_path, font]
    if len(set(paths)) != len(paths):
        raise ValueError("source, output, report, overrides, and font paths must differ")
    for path in (output, report_path):
        if path.exists():
            raise FileExistsError(path)
    if args.preview_dir and args.preview_dir.exists() and any(args.preview_dir.iterdir()):
        raise FileExistsError(f"preview directory is not empty: {args.preview_dir}")
    if sha256(source) != EXPECTED_SOURCE_SHA256:
        raise ValueError(f"unexpected source add00 SHA-256: {sha256(source)}")
    if sha256(font) != EXPECTED_FONT_SHA256:
        raise ValueError(f"unexpected font SHA-256: {sha256(font)}")

    document = json.loads(overrides_path.read_text(encoding="utf-8"))
    if document.get("schema") != "srw-gc-pdf-translation-quality-v1":
        raise ValueError("unsupported translation-quality override schema")
    rows = [row for row in document["payload_overrides"] if "add00_block_index" in row]
    if len(rows) != EXPECTED_TITLE_COUNT:
        raise ValueError(f"scenario title count drift: {len(rows)}")
    blocks = [int(row["add00_block_index"]) for row in rows]
    if len(set(blocks)) != len(blocks):
        raise ValueError("duplicate add00 scenario-title block")
    episode_blocks = {
        int(row["episode"]): int(row["add00_block_index"]) for row in rows
    }
    if episode_blocks != EXPECTED_TITLE_BLOCKS:
        raise ValueError(
            f"scenario-title episode/block mapping drift: {episode_blocks}"
        )

    container = add00_tools.parse_container(source)
    rendered: dict[int, Image.Image] = {}
    render_rows: list[dict[str, object]] = []
    preview_dir = args.preview_dir.resolve() if args.preview_dir else None
    if preview_dir:
        preview_dir.mkdir(parents=True, exist_ok=True)
    for row in rows:
        block_index = int(row["add00_block_index"])
        if block_index >= len(container.blocks):
            raise ValueError(f"scenario-title block out of range: {block_index}")
        block = container.blocks[block_index]
        if block[:4] != add00_tools.BMP_I4:
            raise ValueError(f"scenario-title block {block_index} is not BMP6")
        old_block_sha256 = sha256(block)
        if old_block_sha256 != EXPECTED_TITLE_SOURCE_BLOCK_SHA256[block_index]:
            raise ValueError(
                f"scenario-title source block {block_index} drift: "
                f"{old_block_sha256}"
            )
        episode = int(row["episode"])
        expected_id = f"add02:b033:r{episode:04d}:f0"
        if str(row.get("id")) != expected_id:
            raise ValueError(
                f"scenario-title stable ID drift for episode {episode}: "
                f"{row.get('id')!r}"
            )
        title = str(row["after"]).strip()
        if not title or JAPANESE_RE.search(title):
            raise ValueError(f"invalid Korean scenario title in block {block_index}: {title!r}")
        size = add00_tools.i4_dimensions(block)
        image, details = render_title(title, size, font)
        rendered[block_index] = image
        if preview_dir:
            image.save(preview_dir / f"episode_{int(row['episode']):02d}_block_{block_index}.png")
        render_rows.append(
            {
                "episode": int(row["episode"]),
                "block_index": block_index,
                "japanese": row["japanese"],
                "before": row["before"],
                "after": title,
                "pdf": row["pdf"],
                "page": int(row["page"]),
                "render": details,
                "old_block_sha256": old_block_sha256,
            }
        )

    rebuilt = add00_tools.patch_images(source, rendered)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(rebuilt)
    verification = add00_tools.verify_fixed_layout(source, rebuilt)
    rebuilt_container = add00_tools.parse_container(rebuilt)
    expected_changed_blocks = sorted(
        block_index
        for block_index in blocks
        if container.blocks[block_index] != rebuilt_container.blocks[block_index]
    )
    if verification["changed_blocks"] != expected_changed_blocks:
        raise RuntimeError(
            "changed-block guard failed: "
            f"{verification['changed_blocks']} != {expected_changed_blocks}"
        )
    for row in render_rows:
        row["new_block_sha256"] = sha256(
            rebuilt_container.blocks[int(row["block_index"])]
        )

    report = {
        "status": "pass",
        "source": str(source),
        "source_sha256": sha256(source),
        "output": str(output),
        "output_sha256": sha256(rebuilt),
        "overrides": str(overrides_path),
        "overrides_sha256": sha256(overrides_path),
        "font": str(font),
        "font_sha256": sha256(font),
        "scenario_title_count": len(rows),
        "changed_title_count": len(expected_changed_blocks),
        "unchanged_title_blocks": sorted(set(blocks) - set(expected_changed_blocks)),
        "verification": verification,
        "titles": render_rows,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": "pass",
                "title_count": len(rows),
                "changed_blocks": verification["changed_blocks"],
                "output_sha256": report["output_sha256"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
