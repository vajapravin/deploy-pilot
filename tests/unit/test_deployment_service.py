"""Unit tests for deployment orchestration, concurrency locks, and state machine."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.models.deployment import DeploymentState
from app.models.repository import RepositoryConfig
from app.services.deployment_lock import DeploymentLockManager
from app.services.deployment_service import DeploymentService
from app.services.docker_service import (
    DockerBuildError,
    DockerExecutionResult,
    DockerService,
    HealthCheckFailedError,
)
from app.services.git_service import DirtyWorkingTreeError, GitService
from app.storage.deployment_history import DeploymentHistoryStorage


def _sample_repo(path: Path) -> RepositoryConfig:
    return RepositoryConfig(
        id="test-app",
        name="test-app",
        owner="vajapravin",
        expected_remote="https://github.com/vajapravin/test-app.git",
        path=path,
        branch="main",
        compose_files=["docker-compose.yml"],
        timeout_seconds=60,
    )


@pytest.fixture
def mock_git() -> AsyncMock:
    git = AsyncMock(spec=GitService)
    git.verify_repository.return_value = None
    git.verify_remote.return_value = None
    git.check_clean_working_tree.return_value = None
    git.fetch_branch.return_value = None
    git.verify_commit_in_branch.return_value = "1" * 40
    git.checkout_and_fast_forward.return_value = "1" * 40
    return git


@pytest.fixture
def mock_docker() -> AsyncMock:
    docker = AsyncMock(spec=DockerService)
    docker.verify_docker_available.return_value = None
    docker.execute_compose_up.return_value = DockerExecutionResult(
        exit_code=0,
        output="Successfully built and started containers",
        duration_seconds=5.0,
        health_status="healthy",
    )
    return docker


@pytest.fixture
def storage(tmp_path: Path) -> DeploymentHistoryStorage:
    db_path = tmp_path / "test_deployments.db"
    return DeploymentHistoryStorage(db_path)


@pytest.fixture
def lock_manager() -> DeploymentLockManager:
    return DeploymentLockManager(max_concurrent_builds=1)


@pytest.mark.asyncio
async def test_successful_deployment_flow(
    tmp_path: Path,
    storage: DeploymentHistoryStorage,
    mock_git: AsyncMock,
    mock_docker: AsyncMock,
    lock_manager: DeploymentLockManager,
) -> None:
    """Ensure valid deployment transitions from QUEUED to SUCCEEDED with full history."""
    service = DeploymentService(storage, mock_git, mock_docker, lock_manager)
    repo = _sample_repo(tmp_path)

    record = service.queue_deployment(repo, requested_commit="1" * 40, delivery_id="del-001")
    assert record.state == DeploymentState.QUEUED

    # Wait for background task to complete
    await asyncio.gather(*service._active_tasks)

    updated = storage.get_deployment(record.id)
    assert updated is not None
    assert updated.state == DeploymentState.SUCCEEDED
    assert updated.deployed_commit == "1" * 40
    assert updated.health_status == "healthy"
    assert updated.docker_exit_code == 0
    assert updated.completed_at is not None


@pytest.mark.asyncio
async def test_git_dirty_tree_marks_failed(
    tmp_path: Path,
    storage: DeploymentHistoryStorage,
    mock_git: AsyncMock,
    mock_docker: AsyncMock,
    lock_manager: DeploymentLockManager,
) -> None:
    """Ensure dirty working tree aborts deployment and records FAILED status."""
    mock_git.check_clean_working_tree.side_effect = DirtyWorkingTreeError(["M README.md"])

    service = DeploymentService(storage, mock_git, mock_docker, lock_manager)
    repo = _sample_repo(tmp_path)

    record = service.queue_deployment(repo, requested_commit="1" * 40)
    await asyncio.gather(*service._active_tasks)

    updated = storage.get_deployment(record.id)
    assert updated is not None
    assert updated.state == DeploymentState.FAILED
    assert "working tree is dirty" in str(updated.error_summary)
    # Ensure docker compose was never invoked
    assert mock_docker.execute_compose_up.call_count == 0


@pytest.mark.asyncio
async def test_docker_build_failure_marks_failed(
    tmp_path: Path,
    storage: DeploymentHistoryStorage,
    mock_git: AsyncMock,
    mock_docker: AsyncMock,
    lock_manager: DeploymentLockManager,
) -> None:
    """Ensure Docker compose build error marks deployment as FAILED with error output."""
    mock_docker.execute_compose_up.side_effect = DockerBuildError(
        exit_code=1, error_output="ERROR: syntax error in Dockerfile"
    )

    service = DeploymentService(storage, mock_git, mock_docker, lock_manager)
    repo = _sample_repo(tmp_path)

    record = service.queue_deployment(repo, requested_commit="1" * 40)
    await asyncio.gather(*service._active_tasks)

    updated = storage.get_deployment(record.id)
    assert updated is not None
    assert updated.state == DeploymentState.FAILED
    assert "Docker compose failed" in str(updated.error_summary)


@pytest.mark.asyncio
async def test_healthcheck_failure_marks_failed(
    tmp_path: Path,
    storage: DeploymentHistoryStorage,
    mock_git: AsyncMock,
    mock_docker: AsyncMock,
    lock_manager: DeploymentLockManager,
) -> None:
    """Ensure failed container health checks mark deployment as FAILED."""
    mock_docker.execute_compose_up.side_effect = HealthCheckFailedError(
        "Health check timed out after 60 seconds"
    )

    service = DeploymentService(storage, mock_git, mock_docker, lock_manager)
    repo = _sample_repo(tmp_path)

    record = service.queue_deployment(repo, requested_commit="1" * 40)
    await asyncio.gather(*service._active_tasks)

    updated = storage.get_deployment(record.id)
    assert updated is not None
    assert updated.state == DeploymentState.FAILED
    assert "Health check timed out" in str(updated.error_summary)


@pytest.mark.asyncio
async def test_event_coalescing_supersedes_earlier_build(
    tmp_path: Path,
    storage: DeploymentHistoryStorage,
    mock_git: AsyncMock,
    mock_docker: AsyncMock,
    lock_manager: DeploymentLockManager,
) -> None:
    """Ensure an earlier queued deployment is superseded if a newer commit arrives while queued."""
    service = DeploymentService(storage, mock_git, mock_docker, lock_manager)
    repo = _sample_repo(tmp_path)

    # Queue commit 1
    dep1 = service.queue_deployment(repo, requested_commit="1" * 40)
    # Immediately queue commit 2 for the same repo (coalescing)
    dep2 = service.queue_deployment(repo, requested_commit="2" * 40)

    # Await both background tasks
    await asyncio.gather(*service._active_tasks)

    rec1 = storage.get_deployment(dep1.id)
    rec2 = storage.get_deployment(dep2.id)

    assert rec1 is not None and rec2 is not None
    # dep1 should have been superseded
    assert rec1.state == DeploymentState.SUPERSEDED
    # dep2 should have proceeded to success
    assert rec2.state == DeploymentState.SUCCEEDED


@pytest.mark.asyncio
async def test_reconcile_interrupted_deployments(storage: DeploymentHistoryStorage) -> None:
    """Ensure incomplete deployments from an unexpected server crash are reconciled on startup."""
    from app.models.deployment import DeploymentRecord

    record = DeploymentRecord(
        id="dep_interrupted_001",
        repository_id="test-app",
        branch="main",
        requested_commit="a" * 40,
        state=DeploymentState.BUILDING,  # Stuck in non-terminal state
        created_at="2026-10-28T00:00:00Z",
    )
    storage.create_deployment(record)

    # Run startup reconciliation sweep
    reconciled_count = storage.reconcile_interrupted_deployments()
    assert reconciled_count == 1

    updated = storage.get_deployment(record.id)
    assert updated is not None
    assert updated.state == DeploymentState.FAILED
    assert updated.error_summary == "interrupted_by_server_restart"
