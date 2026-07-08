"""Deployment inspection and history endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from app.core.config import Settings, get_settings
from app.models.deployment import DeploymentRecord
from app.storage.deployment_history import DeploymentHistoryStorage

router = APIRouter(prefix="/deployments", tags=["Deployments"])


def verify_api_token(
    authorization: str | None = Header(default=None),
    x_api_token: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """Verify API token if configured in settings.

    Accepts token via Authorization: Bearer <token> or X-API-Token: <token>.
    """
    if not settings.api_token:
        # If no API token is configured, access is unrestricted locally
        return

    provided_token: str | None = None
    if authorization and authorization.lower().startswith("bearer "):
        provided_token = authorization[7:].strip()
    elif x_api_token:
        provided_token = x_api_token.strip()

    if not provided_token or provided_token != settings.api_token:
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "message": "Invalid or missing API token"},
        )


@router.get("", dependencies=[Depends(verify_api_token)])
async def list_deployments(
    request: Request,
    repository_id: str | None = Query(default=None, description="Filter by repository ID"),
    limit: int = Query(default=20, ge=1, le=100, description="Max records to return"),
) -> list[dict[str, Any]]:
    """List recent deployments with summary information."""
    storage: DeploymentHistoryStorage = request.app.state.storage
    records = storage.list_deployments(repository_id=repository_id, limit=limit)
    return [rec.to_summary_dict() for rec in records]


@router.get("/{deployment_id}", dependencies=[Depends(verify_api_token)])
async def get_deployment(
    request: Request,
    deployment_id: str,
) -> DeploymentRecord:
    """Retrieve full details and diagnostic logs for a specific deployment."""
    storage: DeploymentHistoryStorage = request.app.state.storage
    record = storage.get_deployment(deployment_id)
    if not record:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Deployment '{deployment_id}' not found"},
        )
    return record
