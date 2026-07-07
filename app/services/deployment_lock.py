"""Deployment concurrency control and event coalescing manager."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager


class DeploymentLockManager:
    """Manages per-repository locks, global build concurrency, and event coalescing."""

    def __init__(self, max_concurrent_builds: int = 1) -> None:
        self.max_concurrent_builds = max_concurrent_builds
        self._repo_locks: dict[str, asyncio.Lock] = {}
        self._global_semaphore: asyncio.Semaphore = asyncio.Semaphore(max_concurrent_builds)
        self._latest_queued_deployments: dict[str, str] = {}

    def get_repo_lock(self, repo_id: str) -> asyncio.Lock:
        """Retrieve or create an asyncio.Lock for the specified repository."""
        if repo_id not in self._repo_locks:
            self._repo_locks[repo_id] = asyncio.Lock()
        return self._repo_locks[repo_id]

    def register_queued_deployment(self, repo_id: str, deployment_id: str) -> None:
        """Register a newly queued deployment for immediate event coalescing."""
        self._latest_queued_deployments[repo_id] = deployment_id

    def is_deployment_superseded(self, repo_id: str, deployment_id: str) -> bool:
        """Check if a newer deployment has superseded this deployment before it began building."""
        latest = self._latest_queued_deployments.get(repo_id)
        return latest is not None and latest != deployment_id

    @asynccontextmanager
    async def acquire_build_slot(self, repo_id: str) -> AsyncGenerator[None, None]:
        """Acquire both the per-repository lock and a global concurrency semaphore slot."""
        repo_lock = self.get_repo_lock(repo_id)

        # Acquire per-repository lock and global build semaphore simultaneously
        async with repo_lock, self._global_semaphore:
            yield
