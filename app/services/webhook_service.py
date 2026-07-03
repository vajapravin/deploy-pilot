"""GitHub webhook validation service."""

from __future__ import annotations

import json
from typing import NamedTuple

from pydantic import ValidationError

from app.core.config import Settings
from app.core.security import verify_github_signature
from app.models.repository import RepositoryConfig
from app.models.webhook import GitHubPushPayload
from app.storage.deployment_history import DeploymentHistoryStorage


class WebhookValidationResult(NamedTuple):
    """Result of webhook payload and security validation."""

    status: str  # 'accepted', 'rejected', 'duplicate'
    http_status: int
    reason: str
    delivery_id: str | None = None
    repository: RepositoryConfig | None = None
    payload: GitHubPushPayload | None = None


class WebhookService:
    """Service for authenticating and validating incoming GitHub webhooks."""

    def __init__(
        self,
        settings: Settings,
        storage: DeploymentHistoryStorage,
    ) -> None:
        self.settings = settings
        self.storage = storage

    def validate_webhook(
        self,
        raw_body: bytes,
        headers: dict[str, str],
    ) -> WebhookValidationResult:
        """Execute full security and payload validation on an incoming webhook request."""
        # Normalize header keys to lowercase for reliable lookup
        header_map = {k.lower(): v for k, v in headers.items()}

        # 1. Enforce payload size limit
        if len(raw_body) > self.settings.max_payload_size_bytes:
            return WebhookValidationResult(
                status="rejected",
                http_status=413,
                reason="payload_too_large",
            )

        # 2. Validate X-Hub-Signature-256
        signature_header = header_map.get("x-hub-signature-256")
        if not verify_github_signature(
            raw_body=raw_body,
            signature_header=signature_header,
            secret=self.settings.webhook_secret,
        ):
            return WebhookValidationResult(
                status="rejected",
                http_status=401,
                reason="invalid_signature",
            )

        # 3. Validate X-GitHub-Event
        event_type = header_map.get("x-github-event")
        if not event_type:
            return WebhookValidationResult(
                status="rejected",
                http_status=400,
                reason="missing_event_header",
            )

        if event_type == "ping":
            return WebhookValidationResult(
                status="accepted",
                http_status=200,
                reason="ping_acknowledged",
            )

        if event_type != "push":
            return WebhookValidationResult(
                status="rejected",
                http_status=400,
                reason=f"unsupported_event_{event_type}",
            )

        # 4. Extract delivery ID
        delivery_id = header_map.get("x-github-delivery")
        if not delivery_id:
            return WebhookValidationResult(
                status="rejected",
                http_status=400,
                reason="missing_delivery_id",
            )

        # 5. Check delivery deduplication
        if self.storage.is_duplicate_delivery(delivery_id):
            return WebhookValidationResult(
                status="duplicate",
                http_status=200,
                reason="duplicate_delivery_ignored",
                delivery_id=delivery_id,
            )

        # 6. Parse and validate JSON payload
        try:
            data = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return WebhookValidationResult(
                status="rejected",
                http_status=400,
                reason="malformed_json_payload",
                delivery_id=delivery_id,
            )

        try:
            payload = GitHubPushPayload.model_validate(data)
        except ValidationError:
            return WebhookValidationResult(
                status="rejected",
                http_status=400,
                reason="invalid_payload_schema",
                delivery_id=delivery_id,
            )

        # 7. Check for deleted branch
        if payload.is_branch_deletion:
            return WebhookValidationResult(
                status="rejected",
                http_status=400,
                reason="branch_deletion_rejected",
                delivery_id=delivery_id,
                payload=payload,
            )

        # 8. Verify ref is a branch
        target_branch = payload.target_branch
        if not target_branch:
            return WebhookValidationResult(
                status="rejected",
                http_status=400,
                reason="non_branch_ref_rejected",
                delivery_id=delivery_id,
                payload=payload,
            )

        # 9. Match repository against configured allowlist
        repos = self.settings.load_repositories()
        repo_name = payload.repository.name
        full_name = payload.repository.full_name

        matched_repo: RepositoryConfig | None = None
        for repo in repos.values():
            if repo.name == repo_name or f"{repo.owner}/{repo.name}" == full_name:
                matched_repo = repo
                break

        if not matched_repo:
            return WebhookValidationResult(
                status="rejected",
                http_status=404,
                reason=f"unauthorized_repository_{repo_name}",
                delivery_id=delivery_id,
                payload=payload,
            )

        # 10. Check if target repository is enabled
        if not matched_repo.enabled:
            return WebhookValidationResult(
                status="rejected",
                http_status=403,
                reason=f"repository_disabled_{matched_repo.id}",
                delivery_id=delivery_id,
                repository=matched_repo,
                payload=payload,
            )

        # 11. Validate branch authorization
        if target_branch != matched_repo.branch:
            return WebhookValidationResult(
                status="rejected",
                http_status=400,
                reason=f"branch_not_allowed_{target_branch}",
                delivery_id=delivery_id,
                repository=matched_repo,
                payload=payload,
            )

        # 12. Record delivery as accepted
        self.storage.record_delivery(
            delivery_id=delivery_id,
            event_type=event_type,
            repository_name=matched_repo.id,
            status="accepted",
        )

        return WebhookValidationResult(
            status="accepted",
            http_status=202,
            reason="webhook_accepted_for_deployment",
            delivery_id=delivery_id,
            repository=matched_repo,
            payload=payload,
        )
