"""Cryptographic security utilities for webhook signature verification."""

from __future__ import annotations

import hashlib
import hmac


def verify_github_signature(
    raw_body: bytes,
    signature_header: str | None,
    secret: str,
) -> bool:
    """Verify GitHub HMAC-SHA256 signature using constant-time comparison.

    Args:
        raw_body: The unparsed raw HTTP request payload bytes.
        signature_header: Value of the `X-Hub-Signature-256` header.
        secret: The configured webhook secret.

    Returns:
        True if the signature is valid and authentic, False otherwise.
    """
    if not signature_header or not secret:
        return False

    stripped_header = signature_header.strip()
    prefix = "sha256="
    if not stripped_header.startswith(prefix):
        return False

    received_digest = stripped_header[len(prefix) :]
    # A standard SHA256 hex digest is exactly 64 hexadecimal characters
    if len(received_digest) != 64:
        return False

    try:
        expected_digest = hmac.new(
            key=secret.encode("utf-8"),
            msg=raw_body,
            digestmod=hashlib.sha256,
        ).hexdigest()
    except Exception:
        return False

    return hmac.compare_digest(expected_digest, received_digest)
