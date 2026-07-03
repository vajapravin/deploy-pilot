"""Security and failure-path tests for GitHub webhook processing."""

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.services.webhook_service import WebhookService
from app.storage.deployment_history import DeploymentHistoryStorage

SECRET = "super_secure_webhook_secret_32bytes_long"


def _sign(body: bytes, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _create_service(tmp_path: Path, enabled: bool = True, branch: str = "main") -> WebhookService:
    deploy_root = tmp_path / "docker"
    deploy_root.mkdir(parents=True, exist_ok=True)
    app_dir = deploy_root / "test-app"
    app_dir.mkdir(parents=True, exist_ok=True)

    config_yaml = tmp_path / "repos.yaml"
    config_yaml.write_text(f"""
version: 1
repositories:
  - id: "test-app"
    name: "test-app"
    owner: "vajapravin"
    expected_remote: "https://github.com/vajapravin/test-app.git"
    path: "{app_dir.as_posix()}"
    branch: "{branch}"
    enabled: {str(enabled).lower()}
""")

    settings = Settings(
        config_file=config_yaml,
        allowed_deployment_root=deploy_root,
        webhook_secret=SECRET,
        database_path=tmp_path / "deploypilot.db",
        max_payload_size_bytes=10000,
    )
    storage = DeploymentHistoryStorage(settings.database_path)
    return WebhookService(settings=settings, storage=storage)


def _sample_payload(
    repo_name: str = "test-app",
    branch: str = "main",
    deleted: bool = False,
    after: str = "1111111111111111111111111111111111111111",
) -> dict[str, Any]:
    return {
        "ref": f"refs/heads/{branch}",
        "before": "0000000000000000000000000000000000000000",
        "after": after,
        "deleted": deleted,
        "repository": {
            "id": 123456,
            "name": repo_name,
            "full_name": f"vajapravin/{repo_name}",
        },
        "commits": [
            {
                "id": after,
                "message": "feat: release new version",
            }
        ],
    }


def test_valid_webhook_accepted(tmp_path: Path) -> None:
    """Ensure a valid push event to an enabled repo and branch is accepted."""
    service = _create_service(tmp_path, enabled=True, branch="main")
    payload = _sample_payload()
    body = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Event": "push",
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "delivery-valid-001",
    }

    result = service.validate_webhook(raw_body=body, headers=headers)
    assert result.status == "accepted"
    assert result.http_status == 202
    assert result.repository is not None
    assert result.repository.id == "test-app"
    assert result.payload is not None
    assert result.payload.target_branch == "main"


def test_invalid_signature_rejected(tmp_path: Path) -> None:
    """Ensure request with invalid signature is rejected with 401."""
    service = _create_service(tmp_path)
    body = json.dumps(_sample_payload()).encode()
    bad_sig = "sha256=" + ("0" * 64)
    headers = {
        "X-GitHub-Event": "push",
        "X-Hub-Signature-256": bad_sig,
        "X-GitHub-Delivery": "delivery-bad-sig",
    }

    result = service.validate_webhook(raw_body=body, headers=headers)
    assert result.status == "rejected"
    assert result.http_status == 401
    assert result.reason == "invalid_signature"


def test_ping_event_acknowledged(tmp_path: Path) -> None:
    """Ensure ping events return 200 without triggering deployment."""
    service = _create_service(tmp_path)
    body = b'{"zen": "Keep it logically awesome."}'
    headers = {
        "X-GitHub-Event": "ping",
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "delivery-ping-001",
    }

    result = service.validate_webhook(raw_body=body, headers=headers)
    assert result.status == "accepted"
    assert result.http_status == 200
    assert result.reason == "ping_acknowledged"


def test_unsupported_event_type_rejected(tmp_path: Path) -> None:
    """Ensure non-push events like pull_request or issues are rejected with 400."""
    service = _create_service(tmp_path)
    body = b'{"action": "opened"}'
    headers = {
        "X-GitHub-Event": "pull_request",
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "delivery-pr-001",
    }

    result = service.validate_webhook(raw_body=body, headers=headers)
    assert result.status == "rejected"
    assert result.http_status == 400
    assert "unsupported_event" in result.reason


def test_duplicate_delivery_ignored(tmp_path: Path) -> None:
    """Ensure a replay of the same X-GitHub-Delivery is detected and ignored."""
    service = _create_service(tmp_path)
    body = json.dumps(_sample_payload()).encode()
    headers = {
        "X-GitHub-Event": "push",
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "delivery-duplicate-test",
    }

    # First request
    res1 = service.validate_webhook(raw_body=body, headers=headers)
    assert res1.status == "accepted"

    # Second identical request (duplicate delivery ID)
    res2 = service.validate_webhook(raw_body=body, headers=headers)
    assert res2.status == "duplicate"
    assert res2.http_status == 200
    assert res2.reason == "duplicate_delivery_ignored"


def test_unknown_repository_rejected(tmp_path: Path) -> None:
    """Ensure push to an unconfigured repository returns 404."""
    service = _create_service(tmp_path)
    body = json.dumps(_sample_payload(repo_name="unauthorized-repo")).encode()
    headers = {
        "X-GitHub-Event": "push",
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "delivery-unknown-repo",
    }

    result = service.validate_webhook(raw_body=body, headers=headers)
    assert result.status == "rejected"
    assert result.http_status == 404
    assert "unauthorized_repository" in result.reason


def test_disabled_repository_rejected(tmp_path: Path) -> None:
    """Ensure push to a configured but disabled repository returns 403."""
    service = _create_service(tmp_path, enabled=False)
    body = json.dumps(_sample_payload()).encode()
    headers = {
        "X-GitHub-Event": "push",
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "delivery-disabled-repo",
    }

    result = service.validate_webhook(raw_body=body, headers=headers)
    assert result.status == "rejected"
    assert result.http_status == 403
    assert "repository_disabled" in result.reason


def test_unauthorized_branch_rejected(tmp_path: Path) -> None:
    """Ensure push to an unauthorized branch returns 400."""
    service = _create_service(tmp_path, branch="main")
    body = json.dumps(_sample_payload(branch="feature-dev")).encode()
    headers = {
        "X-GitHub-Event": "push",
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "delivery-wrong-branch",
    }

    result = service.validate_webhook(raw_body=body, headers=headers)
    assert result.status == "rejected"
    assert result.http_status == 400
    assert "branch_not_allowed" in result.reason


def test_deleted_branch_rejected(tmp_path: Path) -> None:
    """Ensure branch deletion events are rejected with 400."""
    service = _create_service(tmp_path)
    body = json.dumps(
        _sample_payload(deleted=True, after="0000000000000000000000000000000000000000")
    ).encode()
    headers = {
        "X-GitHub-Event": "push",
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "delivery-deleted-branch",
    }

    result = service.validate_webhook(raw_body=body, headers=headers)
    assert result.status == "rejected"
    assert result.http_status == 400
    assert "branch_deletion_rejected" in result.reason


def test_tag_push_rejected(tmp_path: Path) -> None:
    """Ensure tag push events (refs/tags/v1.0) are rejected."""
    service = _create_service(tmp_path)
    payload = _sample_payload()
    payload["ref"] = "refs/tags/v1.0.0"
    body = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Event": "push",
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "delivery-tag-push",
    }

    result = service.validate_webhook(raw_body=body, headers=headers)
    assert result.status == "rejected"
    assert result.http_status == 400
    assert result.reason == "non_branch_ref_rejected"


def test_oversized_payload_rejected(tmp_path: Path) -> None:
    """Ensure payloads exceeding max_payload_size_bytes are rejected with 413."""
    service = _create_service(tmp_path)
    huge_body = b"x" * 15000  # max is 10000 in test fixture
    headers = {
        "X-GitHub-Event": "push",
        "X-Hub-Signature-256": _sign(huge_body),
        "X-GitHub-Delivery": "delivery-huge",
    }

    result = service.validate_webhook(raw_body=huge_body, headers=headers)
    assert result.status == "rejected"
    assert result.http_status == 413
    assert result.reason == "payload_too_large"


def test_malformed_json_rejected(tmp_path: Path) -> None:
    """Ensure malformed JSON payloads return 400."""
    service = _create_service(tmp_path)
    bad_body = b"{ unclosed json {"
    headers = {
        "X-GitHub-Event": "push",
        "X-Hub-Signature-256": _sign(bad_body),
        "X-GitHub-Delivery": "delivery-bad-json",
    }

    result = service.validate_webhook(raw_body=bad_body, headers=headers)
    assert result.status == "rejected"
    assert result.http_status == 400
    assert result.reason == "malformed_json_payload"
