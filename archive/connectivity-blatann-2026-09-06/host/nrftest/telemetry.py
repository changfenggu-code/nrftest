from __future__ import annotations

import sys
import threading
import time
from collections.abc import Mapping
from typing import Any, TextIO

from .protocol import PROTOCOL_VERSION, encode_message


class JsonLineEmitter:
    """Serialize responses and telemetry to one NDJSON stream."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stdout
        self._lock = threading.Lock()

    def write(self, message: Mapping[str, Any]) -> None:
        envelope = {
            "protocol_version": PROTOCOL_VERSION,
            "timestamp_ns": time.time_ns(),
            **message,
        }
        with self._lock:
            self._stream.write(encode_message(envelope) + "\n")
            self._stream.flush()

    def response(
        self,
        request_id: str,
        *,
        ok: bool = True,
        state: str | None = None,
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
    ) -> None:
        message: dict[str, Any] = {
            "type": "response",
            "request_id": request_id,
            "ok": ok,
        }
        if state is not None:
            message["state"] = state
        if result is not None:
            message["result"] = dict(result)
        if error is not None:
            message["error"] = dict(error)
        self.write(message)

    def telemetry(self, event: str, **fields: Any) -> None:
        self.write({"type": "telemetry", "event": event, **fields})
