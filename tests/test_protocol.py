from pathlib import Path

from keepwatch.protocol import MAX_PAYLOAD_BYTES, clip, decode_lines, encode, normalize_payload


def test_encode_and_decode_round_trip():
    data = encode({"type": "log", "message": "héllo"}) + b"not json\n" + encode({"type": "result"})
    messages, bad = decode_lines(data)
    assert messages == [{"type": "log", "message": "héllo"}, {"type": "result"}]
    assert bad == ["not json"]


def test_clip_keeps_head_and_tail():
    text, cut = clip("a" * 50 + "b" * 50, 20)
    assert cut is True
    assert text.startswith("a" * 10) and text.endswith("b" * 10)
    assert "80 characters omitted" in text
    assert clip("short", 20) == ("short", False)


def test_normalize_payload_converts_paths_and_tuples():
    value, problem = normalize_payload({"files": (Path("/in/a.tar.gz"), "b")})
    assert problem is None
    assert value == {"files": ["/in/a.tar.gz", "b"]}


def test_normalize_payload_rejects_unserializable_and_oversized():
    assert normalize_payload({1, 2})[1] == "payload is not JSON-serializable: set is not JSON-serializable"
    value, problem = normalize_payload("x" * MAX_PAYLOAD_BYTES)
    assert value is None and problem.startswith("payload is ")
