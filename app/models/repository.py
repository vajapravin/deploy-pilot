"""Repository configuration and authorization models."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

IDENTIFIER_REGEX = re.compile(r"^[a-zA-Z0-9._-]+$")
BRANCH_REGEX = re.compile(r"^(?!.*\.\.)[a-zA-Z0-9._-]+(/[a-zA-Z0-9._-]+)*$")


class HealthCheckConfig(BaseModel):
    """Configuration for verifying application health after deployment."""

    endpoint: str = Field(default="/health", description="HTTP health check path")
    port: int = Field(default=80, ge=1, le=65535, description="Port to query on target container")
    timeout_seconds: int = Field(default=60, ge=1, le=300, description="Health check timeout")
    interval_seconds: int = Field(default=5, ge=1, le=60, description="Polling interval")
    expected_status: int = Field(default=200, ge=200, le=399, description="Expected HTTP status")


class RepositoryConfig(BaseModel):
    """Configuration for an explicitly authorized deployment target."""

    id: str = Field(description="Unique stable repository identifier")
    name: str = Field(description="GitHub repository name")
    owner: str = Field(description="GitHub owner/organization")
    expected_remote: str = Field(description="Expected Git remote URL (HTTPS or SSH)")
    path: Path = Field(description="Absolute filesystem path to deployment directory")
    branch: str = Field(default="main", description="Authorized branch for deployment")
    compose_files: list[str] = Field(
        default_factory=lambda: ["docker-compose.yml"],
        description="Relative paths to Compose files within repository",
    )
    compose_project_name: str | None = Field(
        default=None, description="Explicit Docker Compose project name"
    )
    enabled: bool = Field(default=False, description="Whether automated deployments are authorized")
    timeout_seconds: int = Field(
        default=600, ge=30, le=3600, description="Max deployment execution duration"
    )
    health_check: HealthCheckConfig | None = Field(
        default=None, description="Optional post-deployment health check"
    )
    allowed_validation_commands: list[str] = Field(
        default_factory=list, description="Approved pre- or post-flight validation commands"
    )

    @field_validator("id", "name", "owner")
    @classmethod
    def validate_safe_identifiers(cls, value: str, info: object) -> str:
        """Ensure identifiers contain only safe characters (prevent injection)."""
        if not IDENTIFIER_REGEX.match(value):
            field_name = getattr(info, "field_name", "field")
            raise ValueError(
                f"Field '{field_name}' contains unsafe characters: '{value}'. "
                "Must match [a-zA-Z0-9._-]+"
            )
        return value

    @field_validator("branch")
    @classmethod
    def validate_safe_branch(cls, value: str) -> str:
        """Ensure branch names contain only safe Git ref characters and valid hierarchy."""
        if not BRANCH_REGEX.match(value) or value.endswith(".lock"):
            raise ValueError(
                f"Field 'branch' contains unsafe characters: '{value}'. "
                "Must be a valid branch name matching [a-zA-Z0-9._-]+(/[a-zA-Z0-9._-]+)*"
            )
        return value

    @field_validator("compose_files")
    @classmethod
    def validate_compose_files(cls, files: list[str]) -> list[str]:
        """Ensure compose file paths are relative, safe, and have YAML extensions."""
        if not files:
            raise ValueError("compose_files cannot be empty")
        for f in files:
            if Path(f).is_absolute() or ".." in Path(f).parts:
                raise ValueError(f"Compose file '{f}' must be a safe relative path without '..'")
            if not (f.endswith(".yml") or f.endswith(".yaml")):
                raise ValueError(f"Compose file '{f}' must have a .yml or .yaml extension")
        return files

    def resolve_and_verify_path(self, allowed_root: Path) -> Path:
        """Resolve path and verify it remains strictly within allowed deployment root."""
        resolved = self.path.resolve()
        allowed = allowed_root.resolve()
        try:
            resolved.relative_to(allowed)
        except ValueError as err:
            raise ValueError(
                f"Repository path '{resolved}' escapes allowed deployment root '{allowed}'"
            ) from err
        return resolved


class RepositoriesConfigFile(BaseModel):
    """Root model for repositories.yaml."""

    version: int = Field(default=1, ge=1, le=1)
    repositories: list[RepositoryConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_repositories(self) -> RepositoriesConfigFile:
        """Ensure no duplicate IDs or overlapping deployment paths exist."""
        seen_ids: set[str] = set()
        seen_paths: set[Path] = set()

        for repo in self.repositories:
            if repo.id in seen_ids:
                raise ValueError(f"Duplicate repository id detected: '{repo.id}'")
            seen_ids.add(repo.id)

            resolved_path = repo.path.resolve()
            if resolved_path in seen_paths:
                raise ValueError(
                    f"Duplicate deployment path detected: '{resolved_path}' for repo '{repo.id}'"
                )
            seen_paths.add(resolved_path)

        return self
