from __future__ import annotations

import io
import json

from .nrftest.telemetry import JsonLineEmitter


def test_emitter_writes_versioned_json_lines():
    stream = io.StringIO()
    emitter = JsonLineEmitter(stream)

    emitter.response("r1", state="new", result={"ok_value": True})
    message = json.loads(stream.getvalue())

    assert message["protocol_version"] == 1
    assert message["type"] == "response"
    assert message["request_id"] == "r1"
    assert message["result"]["ok_value"] is True
    assert isinstance(message["timestamp_ns"], int)
