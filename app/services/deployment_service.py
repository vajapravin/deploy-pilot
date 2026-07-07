"""Deployment orchestration service coordinating Git, Docker, and concurrency."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime

from app.models.deployment import DeploymentRecord, DeploymentState
from app.models.repository import RepositoryConfig
from app.services.deployment_lock import DeploymentLockManager
from app.services.docker_service import DockerService, DockerServiceError
from app.services.git_service import GitService, GitServiceError
from app.storage.deployment_history import DeploymentHistoryStorage

logger = logging.getLogger(__name__)


class DeploymentService:
    """Orchestrates end-to-end Git checkout, Docker Compose build, and health checks."""

    def __init__(
        self,
        storage: DeploymentHistoryStorage,
        git_service: GitService,
        docker_service: DockerService,
        lock_manager: DeploymentLockManager,
    ) -> None:
        self.storage = storage
        self.git_service = git_service
        self.docker_service = docker_service
        self.lock_manager = lock_manager
        # Keep strong references to background tasks to prevent garbage collection
        self._active_tasks: set[asyncio.Task[None]] = set()

    def queue_deployment(
        self,
        repo: RepositoryConfig,
        requested_commit: str,
        delivery_id: str | None = None,
    ) -> DeploymentRecord:
        """Queue a validated deployment and launch background execution task."""
        dep_id = f"dep_{uuid.uuid4().hex[:12]}"
        now = datetime.now(UTC).isoformat()

        record = DeploymentRecord(
            id=dep_id,
            delivery_id=delivery_id,
            repository_id=repo.id,
            branch=repo.branch,
            requested_commit=requested_commit,
            state=DeploymentState.QUEUED,
            created_at=now,
        )

        self.storage.create_deployment(record)

        # Register immediately with lock manager for coalescing detection
        self.lock_manager.register_queued_deployment(repo.id, dep_id)

        # Spawn background task with strong reference
        task = asyncio.create_task(
            self._execute_deployment_lifecycle(record, repo, requested_commit)
        )
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)

        return record

    async def _execute_deployment_lifecycle(
        self,
        record: DeploymentRecord,
        repo: RepositoryConfig,
        requested_commit: str,
    ) -> None:
        """Execute the deployment state transitions and commands."""
        start_mono = time.monotonic()

        # Check if already superseded before waiting for locks
        if self.lock_manager.is_deployment_superseded(repo.id, record.id):
            record.state = DeploymentState.SUPERSEDED
            record.completed_at = datetime.now(UTC).isoformat()
            record.error_summary = "Superseded by newer commit while queued"
            self.storage.update_deployment(record)
            return

        async with self.lock_manager.acquire_build_slot(repo.id):
            # Check again after acquiring lock: did a newer push arrive while waiting?
            if self.lock_manager.is_deployment_superseded(repo.id, record.id):
                record.state = DeploymentState.SUPERSEDED
                record.completed_at = datetime.now(UTC).isoformat()
                record.error_summary = "Superseded by newer commit while waiting for build slot"
                self.storage.update_deployment(record)
                return

            record.started_at = datetime.now(UTC).isoformat()

            try:
                # 1. State: VALIDATING
                record.state = DeploymentState.VALIDATING
                self.storage.update_deployment(record)

                await self.git_service.verify_repository(repo.path)
                await self.git_service.verify_remote(repo.path, repo.expected_remote)
                await self.git_service.check_clean_working_tree(repo.path)
                await self.docker_service.verify_docker_available()

                # 2. State: FETCHING
                record.state = DeploymentState.FETCHING
                self.storage.update_deployment(record)

                await self.git_service.fetch_branch(repo.path, repo.branch)
                verified_sha = await self.git_service.verify_commit_in_branch(
                    repo.path, repo.branch, requested_commit
                )
                deployed_sha = await self.git_service.checkout_and_fast_forward(
                    repo.path, repo.branch, verified_sha
                )
                record.deployed_commit = deployed_sha

                # 3. State: BUILDING
                record.state = DeploymentState.BUILDING
                self.storage.update_deployment(record)

                docker_res = await self.docker_service.execute_compose_up(repo)
                record.docker_exit_code = docker_res.exit_code
                record.logs = docker_res.output

                # 4. State: VERIFYING
                record.state = DeploymentState.VERIFYING
                self.storage.update_deployment(record)

                record.health_status = docker_res.health_status
                record.state = DeploymentState.SUCCEEDED

            except (GitServiceError, DockerServiceError) as err:
                logger.error(
                    "Deployment %s failed for repository %s: %s",
                    record.id,
                    repo.id,
                    err,
                )
                record.state = DeploymentState.FAILED
                record.error_summary = str(err)
            except Exception as err:
                logger.exception("Unexpected error in deployment %s: %s", record.id, err)
                record.state = DeploymentState.FAILED
                record.error_summary = f"Unexpected error during deployment: {type(err).__name__}"
            finally:
                record.completed_at = datetime.now(UTC).isoformat()
                record.duration_seconds = round(time.monotonic() - start_mono, 2)
                self.storage.update_deployment(record)
