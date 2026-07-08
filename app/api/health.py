"""Liveness and readiness health check endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.config import Settings, get_settings
from app.services.docker_service import DockerNotAvailableError, DockerService

router = APIRouter(tags=["Health"])


@router.get("/health")
async def liveness() -> dict[str, str]:
    """Lightweight liveness probe ensuring manager process is responsive.

    Never performs expensive I/O or blocks on external dependencies.
    """
    return {"status": "ok", "service": "deploy-pilot"}


@router.get("/ready")
async def readiness(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    """Readiness probe verifying configuration and Docker CLI availability."""
    # 1. Verify repositories configuration
    try:
        settings.load_repositories()
    except Exception as err:
        raise HTTPException(
            status_code=503,
            detail=f"Configuration error: {err}",
        ) from err

    # 2. Verify Docker CLI availability
    docker_service: DockerService = getattr(request.app.state, "docker_service", DockerService())
    try:
        await docker_service.verify_docker_available()
    except DockerNotAvailableError as err:
        raise HTTPException(
            status_code=503,
            detail=f"Docker unavailable: {err}",
        ) from err

    return {"status": "ready", "service": "deploy-pilot"}
