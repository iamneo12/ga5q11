import hashlib
import json
import os
import secrets


def request_hash(body: dict) -> str:
    """Stable hash of an inbound request body, used to detect changed-content
    replays (same runId/receiptId, different payload -> 409)."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def sorted_json_digest(obj) -> str:
    """SHA-256 hex over recursively key-sorted compact JSON. Used for
    approval argumentsDigest."""
    def sort_recursive(o):
        if isinstance(o, dict):
            return {k: sort_recursive(o[k]) for k in sorted(o.keys())}
        if isinstance(o, list):
            return [sort_recursive(v) for v in o]
        return o

    canonical = json.dumps(sort_recursive(obj), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def new_hex_id(nbytes: int = 8) -> str:
    """Opaque nonempty string, at least 8 chars, lowercase hex."""
    return secrets.token_hex(nbytes)


def new_trace_id() -> str:
    return secrets.token_hex(16)  # 32 hex chars


def new_span_id() -> str:
    return secrets.token_hex(8)  # 16 hex chars


REDACT_KEYS = {"authorization", "accessToken", "privateNote"}


def redact(value):
    """Replace sensitive values with [REDACTED] marker; used only if we ever
    need to show that a field existed without exposing its value."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in REDACT_KEYS:
                out[k] = "[REDACTED]"
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value
