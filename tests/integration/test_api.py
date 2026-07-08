"""Integration tests for DeployPilot FastAPI endpoints."""

import hashlib
import hmac
import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings, get_settings
from app.main import create_app, lifespan
from app.services.docker_service import DockerExecutionResult

TEST_SECRET = "integration_test_webhook_secret_32bytes"
API_TOKEN = "integration_admin_token_12345"


def _sign(body: bytes, secret: str = TEST_SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _sample_payload(repo_name: str = "test-app", branch: str = "main") -> dict[str, Any]:
    return {
        "ref": f"refs/heads/{branch}",
        "before": "0" * 40,
        "after": "1" * 40,
        "deleted": False,
        "repository": {
            "id": 999,
            "name": repo_name,
            "full_name": f"vajapravin/{repo_name}",
        },
        "commits": [{"id": "1" * 40, "message": "feat: test"}],
    }


@pytest.fixture
async def test_client(tmp_path: Path) -> AsyncGenerator[AsyncClient, None]:
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
    branch: "main"
    enabled: true
""")

    test_settings = Settings(
        config_file=config_yaml,
        allowed_deployment_root=deploy_root,
        webhook_secret=TEST_SECRET,
        api_token=API_TOKEN,
        database_path=tmp_path / "test_api.db",
    )

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: test_settings

    async with lifespan(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.mark.asyncio
async def test_liveness_probe(test_client: AsyncClient) -> None:
    """Ensure /api/v1/health returns 200 with service status."""
    response = await test_client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "deploy-pilot"}


@pytest.mark.asyncio
async def test_readiness_probe(test_client: AsyncClient) -> None:
    """Ensure /api/v1/ready returns 200 when dependencies are verified."""
    with patch(
        "app.services.docker_service.DockerService.verify_docker_available",
        new=AsyncMock(return_value=None),
    ):
        response = await test_client.get("/api/v1/ready")
        assert response.status_code == 200
        assert response.json()["status"] == "ready"


@pytest.mark.asyncio
async def test_webhook_endpoint_valid_push_flow(test_client: AsyncClient) -> None:
    """Ensure valid webhook push returns 202 Accepted and creates deployment."""
    body = json.dumps(_sample_payload()).encode()
    headers = {
        "X-GitHub-Event": "push",
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "del-int-001",
    }

    mock_docker_res = DockerExecutionResult(
        exit_code=0,
        output="Successfully built",
        duration_seconds=2.0,
        health_status="healthy",
    )

    with (
        patch(
            "app.services.git_service.GitService.verify_repository",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.services.git_service.GitService.verify_remote", new=AsyncMock(return_value=None)
        ),
        patch(
            "app.services.git_service.GitService.check_clean_working_tree",
            new=AsyncMock(return_value=None),
        ),
        patch("app.services.git_service.GitService.fetch_branch", new=AsyncMock(return_value=None)),
        patch(
            "app.services.git_service.GitService.verify_commit_in_branch",
            new=AsyncMock(return_value="1" * 40),
        ),
        patch(
            "app.services.git_service.GitService.checkout_and_fast_forward",
            new=AsyncMock(return_value="1" * 40),
        ),
        patch(
            "app.services.docker_service.DockerService.verify_docker_available",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.services.docker_service.DockerService.execute_compose_up",
            new=AsyncMock(return_value=mock_docker_res),
        ),
    ):
        response = await test_client.post("/api/v1/webhooks/github", content=body, headers=headers)
        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "queued"
        assert data["deployment_id"].startswith("dep_")
        assert data["repository_id"] == "test-app"


@pytest.mark.asyncio
async def test_webhook_endpoint_invalid_signature_rejected(test_client: AsyncClient) -> None:
    """Ensure webhook with invalid signature returns 401."""
    body = json.dumps(_sample_payload()).encode()
    headers = {
        "X-GitHub-Event": "push",
        "X-Hub-Signature-256": "sha256=" + ("0" * 64),
        "X-GitHub-Delivery": "del-bad-sig",
    }

    response = await test_client.post("/api/v1/webhooks/github", content=body, headers=headers)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_webhook_endpoint_ping(test_client: AsyncClient) -> None:
    """Ensure webhook ping event returns 200 ping_acknowledged."""
    body = b'{"zen": "Practicality beats purity."}'
    headers = {
        "X-GitHub-Event": "ping",
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "del-ping-001",
    }

    response = await test_client.post("/api/v1/webhooks/github", content=body, headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "ping_acknowledged"


@pytest.mark.asyncio
async def test_deployments_api_authentication_and_query(test_client: AsyncClient) -> None:
    """Ensure /api/v1/deployments requires authentication and returns records."""
    # 1. Unauthenticated request -> 401
    res_unauth = await test_client.get("/api/v1/deployments")
    assert res_unauth.status_code == 401

    # 2. Authenticated request with Bearer token -> 200
    headers = {"Authorization": f"Bearer {API_TOKEN}"}
    res_auth = await test_client.get("/api/v1/deployments", headers=headers)
    assert res_auth.status_code == 200
    assert isinstance(res_auth.json(), list)
