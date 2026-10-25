"""Messages between the service and a worker process (JSON lines). Stdlib only."""

from __future__ import annotations

import json
from pathlib import PurePath
from typing import Any

PROTOCOL_VERSION = 1
MAX_PAYLOAD_BYTES = 1024 * 1024


def encode(message: dict[str, Any]) -> bytes:
    return (json.dumps(message, ensure_ascii=False, separators=(",", ":"), default=str) + "\n").encode("utf-8")


def decode_lines(data: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse JSON lines. Returns (messages, lines that were not JSON objects)."""
    messages: list[dict[str, Any]] = []
    bad: list[str] = []
    for raw in data.decode("utf-8", errors="replace").splitlines():
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            bad.append(raw)
            continue
        if isinstance(value, dict):
            messages.append(value)
        else:
            bad.append(raw)
    return messages, bad


def clip(text: str, limit: int) -> tuple[str, bool]:
    """Keep at most about `limit` characters: the head and the tail, with a marker between."""
    if len(text) <= limit:
        return text, False
    half = max(limit // 2, 1)
    omitted = len(text) - 2 * half
    return f"{text[:half]}\n…[{omitted} characters omitted]…\n{text[-half:]}", True


def _payload_default(value: Any) -> Any:
    if isinstance(value, PurePath):
        return str(value)
    raise TypeError(f"{type(value).__name__} is not JSON-serializable")


def normalize_payload(payload: Any) -> tuple[Any, str | None]:
    """Return a JSON-safe copy of payload (paths become strings), or (None, problem)."""
    try:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, default=_payload_default)
    except (TypeError, ValueError) as exc:
        return None, f"payload is not JSON-serializable: {exc}"
    size = len(encoded.encode("utf-8"))
    if size > MAX_PAYLOAD_BYTES:
        return None, f"payload is {size} bytes; the limit is {MAX_PAYLOAD_BYTES} (1 MiB)"
    return json.loads(encoded), None
