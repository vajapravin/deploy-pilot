"""DeployPilot - Secure GitHub Webhook-Based Docker Deployment Automation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import deployments, health, webhooks
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.services.deployment_lock import DeploymentLockManager
from app.services.deployment_service import DeploymentService
from app.services.docker_service import DockerService
from app.services.git_service import GitService
from app.services.webhook_service import WebhookService
from app.storage.deployment_history import DeploymentHistoryStorage

logger = logging.getLogger("deploypilot")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application startup, dependency wiring, and graceful shutdown."""
    settings_fn = app.dependency_overrides.get(get_settings, get_settings)
    settings = settings_fn()
    setup_logging(settings.log_level)
    logger.info("Starting DeployPilot on %s:%s", settings.host, settings.port)

    # 1. Initialize persistent storage & perform crash recovery sweep
    storage = DeploymentHistoryStorage(settings.database_path)
    reconciled = storage.reconcile_interrupted_deployments()
    if reconciled > 0:
        logger.warning(
            "Reconciled %d interrupted deployment(s) from prior server restart",
            reconciled,
        )

    # 2. Initialize domain services
    git_service = GitService(default_timeout_seconds=settings.git_timeout_seconds)
    docker_service = DockerService()
    lock_manager = DeploymentLockManager(max_concurrent_builds=settings.max_concurrent_builds)
    webhook_service = WebhookService(settings=settings, storage=storage)
    deployment_service = DeploymentService(
        storage=storage,
        git_service=git_service,
        docker_service=docker_service,
        lock_manager=lock_manager,
    )

    # 3. Attach services to application state for route access
    app.state.settings = settings
    app.state.storage = storage
    app.state.git_service = git_service
    app.state.docker_service = docker_service
    app.state.lock_manager = lock_manager
    app.state.webhook_service = webhook_service
    app.state.deployment_service = deployment_service

    yield

    # 4. Graceful shutdown: await any in-flight background deployments up to 10s
    active_tasks = deployment_service._active_tasks
    if active_tasks:
        logger.info(
            "Graceful shutdown: awaiting %d active deployment task(s)...", len(active_tasks)
        )
        try:
            await asyncio.wait_for(
                asyncio.gather(*active_tasks, return_exceptions=True), timeout=10.0
            )
        except TimeoutError:
            logger.warning("Shutdown timeout reached: cancelling remaining deployment tasks")
            for task in active_tasks:
                task.cancel()

    logger.info("DeployPilot shutdown complete.")


def create_app() -> FastAPI:
    """FastAPI application factory."""
    app = FastAPI(
        title="DeployPilot",
        description="Secure GitHub Webhook-Based Docker Compose Deployment Automation",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Mount versioned API routes
    app.include_router(health.router, prefix="/api/v1")
    app.include_router(webhooks.router, prefix="/api/v1")
    app.include_router(deployments.router, prefix="/api/v1")

    return app


app = create_app()
