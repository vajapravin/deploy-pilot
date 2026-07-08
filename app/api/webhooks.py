"""GitHub webhook ingestion endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from app.services.deployment_service import DeploymentService
from app.services.webhook_service import WebhookService

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


@router.post("/github", status_code=202)
async def handle_github_webhook(request: Request, response: Response) -> dict[str, Any]:
    """Receive, cryptographically verify, and queue GitHub push webhooks."""
    raw_body = await request.body()
    headers = dict(request.headers)

    webhook_service: WebhookService = request.app.state.webhook_service
    deployment_service: DeploymentService = request.app.state.deployment_service

    result = webhook_service.validate_webhook(raw_body=raw_body, headers=headers)

    # 1. Handle rejections
    if result.status == "rejected":
        raise HTTPException(
            status_code=result.http_status,
            detail={"error": result.reason},
        )

    # 2. Handle ping event
    if result.reason == "ping_acknowledged":
        response.status_code = 200
        return {"status": "ping_acknowledged"}

    # 3. Handle duplicates
    if result.status == "duplicate":
        response.status_code = 200
        return {
            "status": "duplicate_ignored",
            "delivery_id": result.delivery_id,
            "reason": result.reason,
        }

    # 4. Handle accepted push for deployment
    if not result.repository or not result.payload:
        raise HTTPException(status_code=400, detail={"error": "missing_payload_or_repository"})

    record = deployment_service.queue_deployment(
        repo=result.repository,
        requested_commit=result.payload.after,
        delivery_id=result.delivery_id,
    )

    response.status_code = 202
    return {
        "status": "queued",
        "deployment_id": record.id,
        "delivery_id": record.delivery_id,
        "repository_id": record.repository_id,
        "branch": record.branch,
        "requested_commit": record.requested_commit,
    }
