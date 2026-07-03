"""Unit tests for cryptographic signature verification."""

import hashlib
import hmac

from app.core.security import verify_github_signature


def _sign(body: bytes, secret: str) -> str:
    """Helper to compute valid GitHub signature header."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_valid_signature_accepted() -> None:
    """Ensure a correctly signed body verifies successfully."""
    secret = "my_super_secret_webhook_key_123"
    body = b'{"ref": "refs/heads/main"}'
    header = _sign(body, secret)
    assert verify_github_signature(body, header, secret) is True


def test_tampered_body_rejected() -> None:
    """Ensure altering even a single byte in the payload fails verification."""
    secret = "my_super_secret_webhook_key_123"
    body = b'{"ref": "refs/heads/main"}'
    header = _sign(body, secret)
    tampered_body = b'{"ref": "refs/heads/prod"}'
    assert verify_github_signature(tampered_body, header, secret) is False


def test_wrong_secret_rejected() -> None:
    """Ensure signing with a different secret fails verification."""
    body = b'{"ref": "refs/heads/main"}'
    header = _sign(body, "attacker_secret")
    assert verify_github_signature(body, header, "actual_secret") is False


def test_missing_or_empty_header_rejected() -> None:
    """Ensure None or empty signature header is safely rejected."""
    secret = "secret"
    body = b"data"
    assert verify_github_signature(body, None, secret) is False
    assert verify_github_signature(body, "", secret) is False
    assert verify_github_signature(body, "   ", secret) is False


def test_empty_secret_rejected() -> None:
    """Ensure an unconfigured or empty secret rejects verification."""
    body = b"data"
    header = _sign(body, "secret")
    assert verify_github_signature(body, header, "") is False


def test_invalid_header_prefix_rejected() -> None:
    """Ensure non-sha256 prefix formats are rejected."""
    secret = "secret"
    body = b"data"
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_github_signature(body, f"sha1={digest}", secret) is False
    assert verify_github_signature(body, digest, secret) is False


def test_truncated_or_malformed_digest_rejected() -> None:
    """Ensure digests that are not 64 hex characters are rejected."""
    secret = "secret"
    body = b"data"
    assert verify_github_signature(body, "sha256=abcdef", secret) is False
    assert verify_github_signature(body, f"sha256={'g' * 64}", secret) is False
