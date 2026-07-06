"""Unit tests for Docker Compose execution and health verification service."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.models.repository import HealthCheckConfig, RepositoryConfig
from app.services.docker_service import (
    DockerBuildError,
    DockerNotAvailableError,
    DockerService,
    DockerTimeoutError,
    HealthCheckFailedError,
    redact_sensitive_output,
)


def _sample_repo(path: Path, health_check: HealthCheckConfig | None = None) -> RepositoryConfig:
    return RepositoryConfig(
        id="test-app",
        name="test-app",
        owner="vajapravin",
        expected_remote="https://github.com/vajapravin/test-app.git",
        path=path,
        branch="main",
        compose_files=["docker-compose.yml"],
        compose_project_name="test-app-proj",
        timeout_seconds=60,
        health_check=health_check,
    )


def test_build_compose_command_structure(tmp_path: Path) -> None:
    """Ensure safe argument construction without untrusted shell interpolation."""
    service = DockerService(docker_binary="docker")
    repo = _sample_repo(tmp_path)
    cmd = service.build_compose_command(repo)

    assert cmd == [
        "docker",
        "compose",
        "-f",
        "docker-compose.yml",
        "-p",
        "test-app-proj",
        "up",
        "--build",
        "-d",
    ]


def test_redact_sensitive_output() -> None:
    """Ensure passwords, tokens, and secrets are redacted from command output."""
    raw = (
        "Building container...\n"
        "DATABASE_PASSWORD: 'super_secret_db_password'\n"
        "GITHUB_TOKEN=ghp_1234567890abcdef1234567890abcdef1234\n"
        "webhook signature: "
        "sha256=abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890\n"
        "Container started successfully."
    )
    redacted = redact_sensitive_output(raw)

    assert "super_secret_db_password" not in redacted
    assert "DATABASE_PASSWORD=[REDACTED]" in redacted
    assert "ghp_1234567890abcdef1234567890abcdef1234" not in redacted
    assert "[REDACTED]" in redacted


@pytest.mark.asyncio
async def test_verify_docker_available_success() -> None:
    """Ensure preflight check succeeds when docker binary returns 0."""
    service = DockerService()
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"Docker Compose version v2.20.0", b""))
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        await service.verify_docker_available()


@pytest.mark.asyncio
async def test_verify_docker_available_missing() -> None:
    """Ensure DockerNotAvailableError is raised when docker binary is missing."""
    service = DockerService(docker_binary="nonexistent_docker_binary")
    with (
        patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError()),
        pytest.raises(DockerNotAvailableError, match="not found on PATH"),
    ):
        await service.verify_docker_available()


@pytest.mark.asyncio
async def test_execute_compose_up_success(tmp_path: Path) -> None:
    """Ensure compose up executes and returns success result."""
    service = DockerService()
    repo = _sample_repo(tmp_path)

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(
        return_value=(b"Built image successfully\nStarted container", b"")
    )
    mock_proc.returncode = 0

    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        patch.object(service, "verify_deployment_health", new=AsyncMock(return_value="healthy")),
    ):
        res = await service.execute_compose_up(repo)
        assert res.exit_code == 0
        assert "Built image successfully" in res.output
        assert res.health_status == "healthy"


@pytest.mark.asyncio
async def test_execute_compose_up_failure_raises_error(tmp_path: Path) -> None:
    """Ensure non-zero exit code raises DockerBuildError with diagnostic output."""
    service = DockerService()
    repo = _sample_repo(tmp_path)

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(
        return_value=(b"", b"ERROR: failed to build image: syntax error")
    )
    mock_proc.returncode = 1

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        with pytest.raises(DockerBuildError) as exc_info:
            await service.execute_compose_up(repo)

        assert exc_info.value.exit_code == 1
        assert "failed to build image" in str(exc_info.value)


@pytest.mark.asyncio
async def test_execute_compose_up_timeout(tmp_path: Path) -> None:
    """Ensure timeout raises DockerTimeoutError and escalates process termination."""
    service = DockerService()
    repo = _sample_repo(tmp_path)

    mock_proc = AsyncMock()
    mock_proc.pid = 9999
    mock_proc.returncode = None
    mock_proc.wait = AsyncMock(return_value=0)

    async def _mock_wait_for(coro: object, timeout: float) -> tuple[bytes, bytes]:
        raise TimeoutError("Command timed out")

    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        patch("asyncio.wait_for", side_effect=_mock_wait_for),
        patch("os.getpgid", return_value=9999),
        patch("os.killpg") as mock_killpg,
        pytest.raises(DockerTimeoutError, match="timed out"),
    ):
        await service.execute_compose_up(repo, timeout_seconds=1.0)
        assert mock_killpg.called


@pytest.mark.asyncio
async def test_poll_http_health_check_success() -> None:
    """Ensure HTTP health check polling succeeds on 200 response."""
    service = DockerService()
    hc = HealthCheckConfig(endpoint="/ready", port=8080, timeout_seconds=5, interval_seconds=1)

    async def _mock_get(url: str) -> httpx.Response:
        return httpx.Response(status_code=200)

    with patch("httpx.AsyncClient.get", side_effect=_mock_get):
        status = await service._poll_http_health_check(hc)
        assert status == "healthy"


@pytest.mark.asyncio
async def test_poll_http_health_check_timeout() -> None:
    """Ensure HTTP health check raises HealthCheckFailedError on persistent failure."""
    service = DockerService()
    hc = HealthCheckConfig(endpoint="/ready", port=8080, timeout_seconds=1, interval_seconds=1)

    async def _mock_get_fail(url: str) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with (
        patch("httpx.AsyncClient.get", side_effect=_mock_get_fail),
        pytest.raises(HealthCheckFailedError, match="Health check failed"),
    ):
        await service._poll_http_health_check(hc)
