from __future__ import annotations

import pytest

from .nrftest.protocol import (
    ProtocolError,
    optional_int,
    parse_command,
    parse_hex,
    required_int,
)


def test_parses_versioned_command():
    command = parse_command('{"protocol_version":1,"request_id":"r1","op":"status"}')

    assert command.request_id == "r1"
    assert command.operation == "status"


def test_defaults_missing_protocol_version_for_initial_cli_compatibility():
    command = parse_command('{"request_id":"r1","op":"status"}')

    assert command.operation == "status"


def test_rejects_unknown_protocol_version():
    with pytest.raises(ProtocolError, match="unsupported protocol_version"):
        parse_command('{"protocol_version":2,"request_id":"r1","op":"status"}')


def test_parses_hex_payload():
    assert parse_hex("deadbeef") == b"\xde\xad\xbe\xef"


def test_rejects_invalid_hex_payload():
    with pytest.raises(ProtocolError, match="hex digits"):
        parse_hex("not-hex")


def test_validates_integer_command_fields_without_accepting_booleans():
    payload = {"count": 3}

    assert required_int(payload, "count", minimum=1) == 3
    assert optional_int(payload, "interval_us", default=0, minimum=0) == 0
    with pytest.raises(ProtocolError, match="must be an integer"):
        required_int({"count": True}, "count")
    with pytest.raises(ProtocolError, match="greater than or equal to 1"):
        required_int({"count": 0}, "count", minimum=1)
