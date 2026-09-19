import json
from pathlib import Path

import pytest

from instinct.adapters.typedstream import TypedStreamError, decode_attributed_body, message_text

FIX = Path(__file__).parent / "fixtures" / "attributed_body"
EXPECTED = json.loads((FIX / "expected.json").read_text())


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_decodes_nsarchiver_fixtures(name):
    assert decode_attributed_body((FIX / f"{name}.bin").read_bytes()) == EXPECTED[name]


def test_length_prefix_boundaries_use_expected_encodings():
    one = (FIX / "len_127.bin").read_bytes()
    two = (FIX / "len_128.bin").read_bytes()
    four = (FIX / "len_70000.bin").read_bytes()
    assert b"\x01+\x7f" in one
    assert b"\x01+\x81\x80\x00" in two
    assert b"\x01+\x82" in four


def test_nul_byte_does_not_truncate():
    text = decode_attributed_body((FIX / "nul_inside.bin").read_bytes())
    assert text.endswith("after")


def test_message_text_prefers_text_column_and_renders_attachments():
    blob = (FIX / "attachment_with_caption.bin").read_bytes()
    assert message_text("plain", blob) == "plain"
    assert message_text(None, blob) == "[attachment]look at this"
    assert message_text(None, (FIX / "attachment.bin").read_bytes()) == "[attachment]"
    assert message_text(None, None) is None
    assert message_text("", None) is None


def test_rejects_garbage_and_truncation():
    with pytest.raises(TypedStreamError):
        decode_attributed_body(b"not a typedstream at all")
    good = (FIX / "len_200.bin").read_bytes()
    cut = good[: good.index(b"\x01+") + 10]  # length says 200, only a few bytes follow
    with pytest.raises(TypedStreamError):
        decode_attributed_body(cut)
    # message_text swallows decode errors instead of crashing a whole thread read
    assert message_text(None, cut) is None


def test_empty_blob():
    assert decode_attributed_body(b"") is None
    assert decode_attributed_body(None) is None
