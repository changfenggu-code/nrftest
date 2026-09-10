from __future__ import annotations

import hashlib


MAX_BURST_COUNT = 10_000
MIN_BURST_PAYLOAD_BYTES = 8
MAX_BURST_PAYLOAD_BYTES = 512


def validate_burst_parameters(
    burst_id: str,
    count: int,
    interval_us: int,
    payload_bytes: int,
) -> None:
    """Validate the provider-neutral parameters of a paced burst."""
    if not isinstance(burst_id, str):
        raise TypeError("burst_id must be a string")
    if not burst_id:
        raise ValueError("burst_id must be non-empty")
    if isinstance(count, bool) or not isinstance(count, int):
        raise TypeError("count must be an integer")
    if not 1 <= count <= MAX_BURST_COUNT:
        raise ValueError(f"count must be between 1 and {MAX_BURST_COUNT}")
    if isinstance(interval_us, bool) or not isinstance(interval_us, int):
        raise TypeError("interval_us must be an integer")
    if interval_us < 0:
        raise ValueError("interval_us must be non-negative")
    if isinstance(payload_bytes, bool) or not isinstance(payload_bytes, int):
        raise TypeError("payload_bytes must be an integer")
    if not MIN_BURST_PAYLOAD_BYTES <= payload_bytes <= MAX_BURST_PAYLOAD_BYTES:
        raise ValueError(
            f"payload_bytes must be between {MIN_BURST_PAYLOAD_BYTES} and "
            f"{MAX_BURST_PAYLOAD_BYTES}"
        )


def make_burst_payload(burst_id: str, sequence: int, payload_bytes: int) -> bytes:
    """Build a deterministic payload with a burst token and little-endian sequence."""
    if not isinstance(burst_id, str):
        raise TypeError("burst_id must be a string")
    if not burst_id:
        raise ValueError("burst_id must be non-empty")
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        raise TypeError("sequence must be an integer")
    if sequence < 0:
        raise ValueError("sequence must be non-negative")
    if isinstance(payload_bytes, bool) or not isinstance(payload_bytes, int):
        raise TypeError("payload_bytes must be an integer")
    if not MIN_BURST_PAYLOAD_BYTES <= payload_bytes <= MAX_BURST_PAYLOAD_BYTES:
        raise ValueError(
            f"payload_bytes must be between {MIN_BURST_PAYLOAD_BYTES} and "
            f"{MAX_BURST_PAYLOAD_BYTES}"
        )

    burst_token = hashlib.sha256(burst_id.encode("utf-8")).digest()[:4]
    header = burst_token + sequence.to_bytes(4, "little")
    filler = hashlib.sha256(burst_id.encode("utf-8") + header).digest()
    repeated = (header + filler) * ((payload_bytes // len(header + filler)) + 1)
    return repeated[:payload_bytes]
