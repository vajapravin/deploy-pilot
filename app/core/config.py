"""Application configuration and startup validation."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.models.repository import RepositoriesConfigFile, RepositoryConfig


class Settings(BaseSettings):
    """Global application settings loaded from environment or .env."""

    model_config = SettingsConfigDict(
        env_prefix="DEPLOY_PILOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Server configuration
    host: str = Field(default="0.0.0.0", description="Bind address")  # noqa: S104
    port: int = Field(default=8088, ge=1, le=65535, description="Listen port")
    log_level: str = Field(default="INFO", description="Log level")
    environment: str = Field(default="production", description="Environment name")

    # Security
    webhook_secret: str = Field(default="", description="Secret used to sign GitHub push payloads")
    api_token: str | None = Field(default=None, description="Admin API token for internal querying")
    max_payload_size_bytes: int = Field(
        default=1024 * 1024,
        ge=1024,
        le=10 * 1024 * 1024,
        description="Max webhook body size (1MB)",
    )

    # Filesystem & Storage
    config_file: Path = Field(
        default=Path("config/repositories.yaml"),
        description="Path to repository definitions YAML file",
    )
    database_path: Path = Field(
        default=Path("data/deploypilot.db"),
        description="Path to SQLite deployment database",
    )
    allowed_deployment_root: Path = Field(
        default=Path("/volume1/docker"),
        description="Root directory strictly containing all deployable projects",
    )

    # Concurrency & Timeouts
    max_concurrent_builds: int = Field(
        default=1, ge=1, le=5, description="Global maximum concurrent Docker builds"
    )
    default_timeout_seconds: int = Field(
        default=600, ge=30, le=3600, description="Default overall deployment timeout"
    )
    git_timeout_seconds: int = Field(default=60, ge=5, le=300, description="Git network timeout")
    healthcheck_timeout_seconds: int = Field(
        default=120, ge=5, le=600, description="Max time to wait for container health checks"
    )

    @field_validator("webhook_secret")
    @classmethod
    def validate_webhook_secret(cls, v: str) -> str:
        """Validate webhook secret is not empty when in production."""
        stripped = v.strip()
        return stripped

    def load_repositories(self) -> dict[str, RepositoryConfig]:
        """Load and strictly validate the repositories configuration file.

        Returns a dictionary mapping repository ID to validated RepositoryConfig.
        Raises ValueError or FileNotFoundError on configuration errors.
        """
        if not self.config_file.is_file():
            raise FileNotFoundError(
                f"Repositories configuration file not found at: {self.config_file.resolve()}"
            )

        with open(self.config_file, encoding="utf-8") as f:
            try:
                data = yaml.safe_load(f)
            except yaml.YAMLError as err:
                raise ValueError(f"Malformed YAML in {self.config_file}: {err}") from err

        if not isinstance(data, dict):
            type_name = type(data).__name__
            raise ValueError(
                f"Invalid format in {self.config_file}: expected root dictionary, got {type_name}"
            )

        parsed = RepositoriesConfigFile.model_validate(data)

        repo_map: dict[str, RepositoryConfig] = {}
        for repo in parsed.repositories:
            # Enforce path traversal safeguard against allowed deployment root
            repo.resolve_and_verify_path(self.allowed_deployment_root)
            repo_map[repo.id] = repo

        return repo_map


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the cached application settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Reset cached settings (primarily used in tests)."""
    global _settings
    _settings = None
