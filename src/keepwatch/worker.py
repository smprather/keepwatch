"""Child side of one Python hook call: `python -m keepwatch.worker`. Stdlib only.

Reads one request (JSON) from the request pipe (`--request-fd`, or `--request-handle` on Windows),
imports the watch's watch.py, calls one function with a Ctx, and writes JSON-line messages to the
result pipe.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from keepwatch import __version__, platform
from keepwatch.ctx import Ctx, Unknown
from keepwatch.hooks import CHECK, HOOK_NAMES, WATCH_PY
from keepwatch.protocol import PROTOCOL_VERSION, encode, normalize_payload

Send = Callable[[dict[str, Any]], None]
_STANDARD_RECORD_KEYS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}


class _PipeHandler(logging.Handler):
    """Forwards plugin log records to the service as `log` messages."""

    def __init__(self, send: Send) -> None:
        super().__init__(logging.DEBUG)
        self._send = send

    def emit(self, record: logging.LogRecord) -> None:
        try:
            fields = {key: value for key, value in vars(record).items() if key not in _STANDARD_RECORD_KEYS}
            message: dict[str, Any] = {
                "type": "log",
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "fields": fields,
            }
            if record.exc_info:
                message["traceback"] = "".join(traceback.format_exception(*record.exc_info))
            self._send(message)
        except Exception:
            self.handleError(record)


def _exception_info(exc: BaseException) -> dict[str, str]:
    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback": "".join(traceback.format_exception(exc)),
    }


def _load_module(watch_dir: Path) -> Any:
    spec = importlib.util.spec_from_file_location("keepwatch_watch", watch_dir / WATCH_PY)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {watch_dir / WATCH_PY}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["keepwatch_watch"] = module
    spec.loader.exec_module(module)
    return module


def interpret_check_return(value: Any) -> dict[str, Any]:
    """Turn check()'s return value into a result (see `keepwatch docs python`)."""
    if value is True or value is False:
        return {"status": "answer", "answer": value, "payload": None}
    if value is None:
        return {"status": "unknown", "reason": None, "payload": None}
    if isinstance(value, tuple) and len(value) == 2 and (value[0] is None or isinstance(value[0], bool)):
        answer, raw_payload = value
        payload, problem = normalize_payload(raw_payload)
        if problem is not None:
            return {"status": "error", "reason": problem}
        if answer is None:
            return {"status": "unknown", "reason": None, "payload": payload}
        return {"status": "answer", "answer": answer, "payload": payload}
    shown = repr(value)[:80]
    return {
        "status": "error",
        "reason": f"check returned {type(value).__name__} {shown}; "
        "expected True, False, None, or a tuple (answer, payload)",
    }


def run_request(request: dict[str, Any], send: Send) -> dict[str, Any]:
    """Perform one request and return the result fields (without "type")."""
    watch_dir = Path(request["watch_dir"])
    hook = request["hook"]
    os.chdir(watch_dir)
    sys.path.insert(0, str(watch_dir))
    logger = logging.getLogger(f"keepwatch.watch.{request['watch']}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(_PipeHandler(send))
    try:
        module = _load_module(watch_dir)
    except BaseException as exc:
        return {
            "status": "error",
            "reason": f"importing watch.py failed: {type(exc).__name__}: {exc}",
            "exception": _exception_info(exc),
        }
    if request.get("mode") == "describe":
        return {"status": "ok", "hooks": sorted(name for name in HOOK_NAMES if callable(getattr(module, name, None)))}
    function = getattr(module, hook, None)
    if not callable(function):
        return {"status": "error", "reason": f"watch.py has no function {hook}()"}
    ctx = Ctx(
        watch=request["watch"],
        hook=hook,
        poll_id=request["poll_id"],
        condition=request["condition"],
        payload=request.get("payload"),
        settings=request.get("settings") or {},
        watch_dir=watch_dir,
        data_dir=Path(request["data_dir"]),
        run_dir=Path(request["run_dir"]),
        deadline=request["deadline"],
        capture_bytes=request.get("capture_bytes", 65_536),
        emit=send,
        shell=request.get("shell"),
    )
    try:
        value = function(ctx)
    except Unknown as exc:
        if hook == CHECK:
            return {"status": "unknown", "reason": exc.reason or None, "payload": None}
        return {
            "status": "error",
            "reason": "keepwatch.Unknown only has a meaning in check()",
            "exception": _exception_info(exc),
        }
    except BaseException as exc:
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}", "exception": _exception_info(exc)}
    if hook == CHECK:
        return interpret_check_return(value)
    return {"status": "ok"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m keepwatch.worker")
    parser.add_argument("--request-fd", type=int)
    parser.add_argument("--result-fd", type=int)
    parser.add_argument("--request-handle", type=int)
    parser.add_argument("--result-handle", type=int)
    args = parser.parse_args(argv)
    by_handle = args.request_handle is not None
    request_source = args.request_handle if by_handle else args.request_fd
    result_target = args.result_handle if by_handle else args.result_fd
    if request_source is None or result_target is None:
        parser.error("give --request-fd/--result-fd or --request-handle/--result-handle")
    with platform.open_inherited(request_source, handle=by_handle, mode="rb") as handle:
        request = json.loads(handle.read())
    out = platform.open_inherited(result_target, handle=by_handle, mode="wb")

    def send(message: dict[str, Any]) -> None:
        out.write(encode(message))

    send({"type": "hello", "version": __version__, "protocol": PROTOCOL_VERSION})
    result = run_request(request, send)
    send({"type": "result", **result})
    out.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
