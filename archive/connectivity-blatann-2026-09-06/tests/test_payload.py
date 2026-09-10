from __future__ import annotations

import pytest

from .nrftest.payload import (
    MAX_BURST_COUNT,
    MAX_BURST_PAYLOAD_BYTES,
    MIN_BURST_PAYLOAD_BYTES,
    make_burst_payload,
    validate_burst_parameters,
)


def test_burst_payload_has_stable_batch_token_and_sequence_header():
    first = make_burst_payload("run-001", 0, 32)
    second = make_burst_payload("run-001", 1, 32)
    same_first = make_burst_payload("run-001", 0, 32)
    other_batch = make_burst_payload("run-002", 0, 32)

    assert first == same_first
    assert len(first) == 32
    assert first[:4] == second[:4]
    assert first[4:8] == (0).to_bytes(4, "little")
    assert second[4:8] == (1).to_bytes(4, "little")
    assert first[:4] != other_batch[:4]


def test_burst_parameters_accept_boundary_values():
    validate_burst_parameters(
        "run-001",
        MAX_BURST_COUNT,
        0,
        MAX_BURST_PAYLOAD_BYTES,
    )


@pytest.mark.parametrize(
    ("burst_id", "count", "interval_us", "payload_bytes", "message"),
    [
        ("", 1, 0, MIN_BURST_PAYLOAD_BYTES, "burst_id"),
        ("run", 0, 0, MIN_BURST_PAYLOAD_BYTES, "count"),
        ("run", MAX_BURST_COUNT + 1, 0, MIN_BURST_PAYLOAD_BYTES, "count"),
        ("run", 1, -1, MIN_BURST_PAYLOAD_BYTES, "interval_us"),
        ("run", 1, 0, MIN_BURST_PAYLOAD_BYTES - 1, "payload_bytes"),
        ("run", 1, 0, MAX_BURST_PAYLOAD_BYTES + 1, "payload_bytes"),
    ],
)
def test_burst_parameters_reject_invalid_values(
    burst_id: str,
    count: int,
    interval_us: int,
    payload_bytes: int,
    message: str,
):
    with pytest.raises(ValueError, match=message):
        validate_burst_parameters(burst_id, count, interval_us, payload_bytes)


def test_payload_rejects_sequence_overflow():
    with pytest.raises(OverflowError):
        make_burst_payload("run-001", 2**32, MIN_BURST_PAYLOAD_BYTES)
