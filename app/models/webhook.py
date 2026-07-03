"""GitHub webhook payload schemas and validation models."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

SHA_REGEX = re.compile(r"^[0-9a-fA-F]{40}$")
ZERO_SHA = "0000000000000000000000000000000000000000"


class GitCommit(BaseModel):
    """Git commit information within a push payload."""

    id: str = Field(description="Full 40-character commit SHA")
    message: str = Field(default="", description="Commit message")
    timestamp: str | None = Field(default=None, description="ISO timestamp of the commit")
    author: dict[str, Any] | None = Field(default=None, description="Author information")
    url: str | None = Field(default=None, description="URL pointing to the commit on GitHub")

    @field_validator("id")
    @classmethod
    def validate_commit_sha(cls, value: str) -> str:
        """Validate commit SHA is a valid 40-character hex string."""
        if not SHA_REGEX.match(value):
            raise ValueError(f"Invalid commit SHA: '{value}'. Expected 40 hex characters.")
        return value.lower()


class GitRepository(BaseModel):
    """Repository identity and metadata in a push payload."""

    id: int = Field(description="Unique GitHub repository ID")
    name: str = Field(description="Repository name")
    full_name: str = Field(description="Full repository name including owner (owner/repo)")
    clone_url: str | None = Field(default=None, description="HTTPS clone URL")
    ssh_url: str | None = Field(default=None, description="SSH clone URL")
    default_branch: str | None = Field(default=None, description="Default repository branch")


class GitHubPushPayload(BaseModel):
    """Root model for GitHub push webhook payloads."""

    ref: str = Field(description="Full Git reference (e.g., refs/heads/main)")
    before: str = Field(description="Commit SHA before the push event")
    after: str = Field(description="Commit SHA after the push event")
    created: bool = Field(default=False, description="Whether this push created the ref")
    deleted: bool = Field(default=False, description="Whether this push deleted the ref")
    forced: bool = Field(default=False, description="Whether this push was a force push")
    repository: GitRepository = Field(description="Repository metadata")
    commits: list[GitCommit] = Field(
        default_factory=list, description="List of commits included in this push"
    )
    head_commit: GitCommit | None = Field(
        default=None, description="The latest commit in the push event"
    )

    @field_validator("after")
    @classmethod
    def validate_after_sha(cls, value: str) -> str:
        """Validate after SHA is either a 40-char SHA or zero SHA for deletions."""
        if value != ZERO_SHA and not SHA_REGEX.match(value):
            raise ValueError(f"Invalid 'after' commit SHA: '{value}'")
        return value.lower()

    @property
    def target_branch(self) -> str | None:
        """Extract the simple branch name if the ref points to a branch."""
        branch_prefix = "refs/heads/"
        if self.ref.startswith(branch_prefix):
            return self.ref[len(branch_prefix) :]
        return None

    @property
    def is_branch_deletion(self) -> bool:
        """Return True if this event represents a branch deletion."""
        return self.deleted or self.after == ZERO_SHA
