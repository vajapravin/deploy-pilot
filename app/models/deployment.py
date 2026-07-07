"""Deployment lifecycle models and state machine."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class DeploymentState(StrEnum):
    """Explicit states of a deployment lifecycle."""

    RECEIVED = "received"
    QUEUED = "queued"
    VALIDATING = "validating"
    FETCHING = "fetching"
    BUILDING = "building"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SUPERSEDED = "superseded"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """Return True if this state is a terminal lifecycle state."""
        return self in (
            DeploymentState.SUCCEEDED,
            DeploymentState.FAILED,
            DeploymentState.SUPERSEDED,
            DeploymentState.CANCELLED,
        )


class DeploymentRecord(BaseModel):
    """Complete record of a deployment execution."""

    id: str = Field(description="Unique deployment identifier (e.g. dep_...)")
    delivery_id: str | None = Field(default=None, description="GitHub delivery ID")
    repository_id: str = Field(description="Target repository identifier")
    branch: str = Field(description="Target Git branch")
    requested_commit: str = Field(description="Requested commit SHA from webhook")
    deployed_commit: str | None = Field(
        default=None, description="Actual checked-out and deployed commit SHA"
    )
    state: DeploymentState = Field(
        default=DeploymentState.QUEUED, description="Current lifecycle state"
    )
    created_at: str = Field(description="ISO timestamp when deployment was queued")
    started_at: str | None = Field(default=None, description="ISO timestamp when execution began")
    completed_at: str | None = Field(
        default=None, description="ISO timestamp when deployment completed"
    )
    duration_seconds: float | None = Field(
        default=None, description="Total execution duration in seconds"
    )
    docker_exit_code: int | None = Field(
        default=None, description="Exit code of docker compose up command"
    )
    health_status: str | None = Field(
        default=None, description="Health check outcome ('healthy', 'unhealthy')"
    )
    error_summary: str | None = Field(default=None, description="Sanitized summary of any failure")
    logs: str | None = Field(default=None, description="Bounded redacted execution logs")

    def to_summary_dict(self) -> dict[str, Any]:
        """Return safe summary representation excluding voluminous logs."""
        data = self.model_dump()
        data.pop("logs", None)
        return data
