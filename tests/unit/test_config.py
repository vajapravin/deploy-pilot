"""Unit tests for configuration loading and validation."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.models.repository import RepositoryConfig


def test_valid_repositories_file_loading(tmp_path: Path) -> None:
    """Test successful loading and parsing of a valid repositories configuration."""
    deploy_root = tmp_path / "docker"
    deploy_root.mkdir()
    app_dir = deploy_root / "my-app"
    app_dir.mkdir()

    config_yaml = tmp_path / "repos.yaml"
    config_yaml.write_text(f"""
version: 1
repositories:
  - id: "my-app"
    name: "my-app"
    owner: "octocat"
    expected_remote: "https://github.com/octocat/my-app.git"
    path: "{app_dir.as_posix()}"
    branch: "main"
    compose_files:
      - "docker-compose.yml"
    enabled: true
    timeout_seconds: 300
    health_check:
      endpoint: "/ready"
      port: 8080
      timeout_seconds: 30
      interval_seconds: 5
      expected_status: 200
""")

    settings = Settings(
        config_file=config_yaml,
        allowed_deployment_root=deploy_root,
        webhook_secret="test_secret_12345",
    )
    repos = settings.load_repositories()

    assert "my-app" in repos
    repo = repos["my-app"]
    assert repo.name == "my-app"
    assert repo.owner == "octocat"
    assert repo.enabled is True
    assert repo.branch == "main"
    assert repo.timeout_seconds == 300
    assert repo.health_check is not None
    assert repo.health_check.endpoint == "/ready"


def test_missing_repositories_file_raises_error(tmp_path: Path) -> None:
    """Ensure missing repositories.yaml raises FileNotFoundError."""
    missing_file = tmp_path / "does_not_exist.yaml"
    settings = Settings(config_file=missing_file, webhook_secret="dummy")
    with pytest.raises(FileNotFoundError, match="Repositories configuration file not found"):
        settings.load_repositories()


def test_malformed_yaml_raises_error(tmp_path: Path) -> None:
    """Ensure malformed YAML syntax raises ValueError."""
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text("repositories: [ unclosed list")
    settings = Settings(config_file=bad_yaml, webhook_secret="dummy")
    with pytest.raises(ValueError, match="Malformed YAML"):
        settings.load_repositories()


def test_path_traversal_escapes_allowed_root_raises_error(tmp_path: Path) -> None:
    """Ensure repository path outside allowed deployment root is rejected."""
    allowed_root = tmp_path / "docker"
    allowed_root.mkdir()
    outside_dir = tmp_path / "etc"
    outside_dir.mkdir()

    config_yaml = tmp_path / "repos.yaml"
    config_yaml.write_text(f"""
version: 1
repositories:
  - id: "malicious-app"
    name: "malicious"
    owner: "attacker"
    expected_remote: "https://github.com/attacker/malicious.git"
    path: "{outside_dir.as_posix()}"
    branch: "main"
    enabled: true
""")

    settings = Settings(
        config_file=config_yaml,
        allowed_deployment_root=allowed_root,
        webhook_secret="dummy",
    )
    with pytest.raises(ValueError, match="escapes allowed deployment root"):
        settings.load_repositories()


def test_duplicate_repository_id_raises_error(tmp_path: Path) -> None:
    """Ensure duplicate repository IDs in config raise ValueError."""
    deploy_root = tmp_path / "docker"
    deploy_root.mkdir()
    (deploy_root / "app1").mkdir()
    (deploy_root / "app2").mkdir()

    config_yaml = tmp_path / "repos.yaml"
    config_yaml.write_text(f"""
version: 1
repositories:
  - id: "duplicate-id"
    name: "app1"
    owner: "octocat"
    expected_remote: "https://github.com/octocat/app1.git"
    path: "{(deploy_root / "app1").as_posix()}"
  - id: "duplicate-id"
    name: "app2"
    owner: "octocat"
    expected_remote: "https://github.com/octocat/app2.git"
    path: "{(deploy_root / "app2").as_posix()}"
""")

    settings = Settings(
        config_file=config_yaml,
        allowed_deployment_root=deploy_root,
        webhook_secret="dummy",
    )
    with pytest.raises(ValueError, match="Duplicate repository id detected"):
        settings.load_repositories()


def test_duplicate_deployment_path_raises_error(tmp_path: Path) -> None:
    """Ensure two repositories targeting the same directory raise ValueError."""
    deploy_root = tmp_path / "docker"
    deploy_root.mkdir()
    (deploy_root / "shared-path").mkdir()

    config_yaml = tmp_path / "repos.yaml"
    config_yaml.write_text(f"""
version: 1
repositories:
  - id: "repo-a"
    name: "repo-a"
    owner: "octocat"
    expected_remote: "https://github.com/octocat/repo-a.git"
    path: "{(deploy_root / "shared-path").as_posix()}"
  - id: "repo-b"
    name: "repo-b"
    owner: "octocat"
    expected_remote: "https://github.com/octocat/repo-b.git"
    path: "{(deploy_root / "shared-path").as_posix()}"
""")

    settings = Settings(
        config_file=config_yaml,
        allowed_deployment_root=deploy_root,
        webhook_secret="dummy",
    )
    with pytest.raises(ValueError, match="Duplicate deployment path detected"):
        settings.load_repositories()


def test_unsafe_identifier_characters_rejected() -> None:
    """Ensure command-injection characters in IDs, branches or names are rejected."""
    with pytest.raises(ValidationError):
        RepositoryConfig(
            id="bad;rm -rf /",
            name="app",
            owner="octocat",
            expected_remote="https://github.com/octocat/app.git",
            path=Path("/tmp"),
        )

    with pytest.raises(ValidationError):
        RepositoryConfig(
            id="app",
            name="app",
            owner="octocat",
            expected_remote="https://github.com/octocat/app.git",
            path=Path("/tmp"),
            branch="main; reboot",
        )


def test_invalid_compose_file_extension_rejected() -> None:
    """Ensure compose files without .yml or .yaml are rejected."""
    with pytest.raises(ValidationError, match="extension"):
        RepositoryConfig(
            id="app",
            name="app",
            owner="octocat",
            expected_remote="https://github.com/octocat/app.git",
            path=Path("/tmp"),
            compose_files=["docker-compose.sh"],
        )


def test_compose_file_with_parent_traversal_rejected() -> None:
    """Ensure compose files with '..' are rejected."""
    with pytest.raises(ValidationError, match="without '..'"):
        RepositoryConfig(
            id="app",
            name="app",
            owner="octocat",
            expected_remote="https://github.com/octocat/app.git",
            path=Path("/tmp"),
            compose_files=["../compose.yml"],
        )


def test_timeout_bounds_enforced() -> None:
    """Ensure timeouts below 30s or above 3600s are rejected."""
    with pytest.raises(ValidationError):
        RepositoryConfig(
            id="app",
            name="app",
            owner="octocat",
            expected_remote="https://github.com/octocat/app.git",
            path=Path("/tmp"),
            timeout_seconds=10,  # Below minimum (30s)
        )

    with pytest.raises(ValidationError):
        RepositoryConfig(
            id="app",
            name="app",
            owner="octocat",
            expected_remote="https://github.com/octocat/app.git",
            path=Path("/tmp"),
            timeout_seconds=5000,  # Above maximum (3600s)
        )
