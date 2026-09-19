#!/usr/bin/env python3
"""Regenerate tests/fixtures/attributed_body/*.bin with the system NSArchiver.

These are genuine typedstream blobs, produced by the same serializer Messages
uses, built from synthetic strings. Nothing is read from the real chat.db.
Run: uv run scripts/make_fixtures.py
"""

import json
from pathlib import Path

from Foundation import NSArchiver, NSAttributedString, NSMutableAttributedString

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "attributed_body"

CASES = {
    "short_ascii": "Hi",
    "len_127": "a" * 127,  # largest single-byte length
    "len_128": "b" * 128,  # first 0x81 int16 length
    "len_200": "x" * 200,
    "len_70000": "long " * 14000,  # 0x82 int32 length
    "emoji_non_ascii": "héllo 👋🏽 日本語 — ça va? 🇺🇸",
    "multiline": "line one\nline two\n\nline four",
    "attachment": "￼",
    "attachment_with_caption": "￼look at this",
    "nul_inside": "before\x00after",  # CAST(... AS TEXT) would stop at the NUL
    "url": "https://example.com/a?b=c&d=é",
}


def archive(text: str, *, mutable: bool = False, multi_run: bool = False) -> bytes:
    attrs = {"__kIMMessagePartAttributeName": 0}
    if mutable or multi_run:
        s = NSMutableAttributedString.alloc().initWithString_attributes_(text, attrs)
        if multi_run and len(text) > 4:
            s.addAttribute_value_range_("__kIMDataDetectedAttributeName", b"x", (0, 4))
    else:
        s = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
    return bytes(NSArchiver.archivedDataWithRootObject_(s))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    expected = {}
    for name, text in CASES.items():
        (OUT / f"{name}.bin").write_bytes(archive(text))
        expected[name] = text
    (OUT / "mutable_multi_run.bin").write_bytes(archive("Meet at 5pm today?", multi_run=True))
    expected["mutable_multi_run"] = "Meet at 5pm today?"
    (OUT / "expected.json").write_text(json.dumps(expected, ensure_ascii=False, indent=1))
    print(f"wrote {len(expected)} fixtures to {OUT}")


if __name__ == "__main__":
    main()
