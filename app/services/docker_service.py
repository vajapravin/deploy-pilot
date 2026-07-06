"""Docker Compose execution and post-deployment health verification service."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import time
from dataclasses import dataclass

import httpx

from app.models.repository import HealthCheckConfig, RepositoryConfig

# Patterns and replacements for sensitive tokens, passwords, and secrets
SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"(?i)(password|passwd|secret|token|api[_-]?key|auth|bearer)\s*[:=]\s*['\"]?([^\s'\"]+)"
        ),
        r"\1=[REDACTED]",
    ),
    (re.compile(r"ghp_[0-9a-zA-Z]{36}"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"github_pat_[0-9a-zA-Z_]{82}"), "[REDACTED_GITHUB_PAT]"),
    (re.compile(r"sha256=[0-9a-fA-F]{64}"), "sha256=[REDACTED_SIGNATURE]"),
]


def redact_sensitive_output(text: str) -> str:
    """Redact sensitive passwords, keys, and tokens from diagnostic logs."""
    redacted = text
    for pattern, replacement in SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


class DockerServiceError(Exception):
    """Base exception for Docker execution errors."""


class DockerNotAvailableError(DockerServiceError):
    """Raised when Docker CLI or Docker Compose plugin is not installed/reachable."""


class DockerTimeoutError(DockerServiceError):
    """Raised when Docker Compose execution exceeds bounded timeout."""


class DockerBuildError(DockerServiceError):
    """Raised when Docker Compose build or startup command exits with non-zero status."""

    def __init__(self, exit_code: int, error_output: str) -> None:
        self.exit_code = exit_code
        self.error_output = error_output.strip()
        super().__init__(f"Docker compose failed with exit code {exit_code}: {self.error_output}")


class HealthCheckFailedError(DockerServiceError):
    """Raised when target application container fails configured health checks."""


@dataclass
class DockerExecutionResult:
    """Result of Docker Compose deployment and verification."""

    exit_code: int
    output: str
    duration_seconds: float
    health_status: str  # 'healthy', 'unhealthy', 'no_health_check'


class DockerService:
    """Service for running Docker Compose commands with safety bounds and health verification."""

    def __init__(self, docker_binary: str = "docker") -> None:
        self.docker_binary = docker_binary

    def build_compose_command(self, repo: RepositoryConfig) -> list[str]:
        """Construct safe command argument list without untrusted string interpolation."""
        cmd = [self.docker_binary, "compose"]

        for cf in repo.compose_files:
            cmd.extend(["-f", cf])

        if repo.compose_project_name:
            cmd.extend(["-p", repo.compose_project_name])

        cmd.extend(["up", "--build", "-d"])
        return cmd

    async def verify_docker_available(self) -> None:
        """Verify that Docker executable and Compose plugin are installed and working."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.docker_binary,
                "compose",
                "version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise DockerNotAvailableError(
                    f"Docker Compose plugin returned non-zero code: {stderr.decode()}"
                )
        except FileNotFoundError as err:
            raise DockerNotAvailableError(
                f"Docker executable '{self.docker_binary}' not found on PATH"
            ) from err

    async def execute_compose_up(
        self,
        repo: RepositoryConfig,
        timeout_seconds: float | None = None,
    ) -> DockerExecutionResult:
        """Execute docker compose up --build -d in repository directory.

        Guarantees process group termination on timeout and bounded, redacted output.
        """
        timeout = timeout_seconds or repo.timeout_seconds
        cmd = self.build_compose_command(repo)
        start_time = time.monotonic()

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=repo.path.as_posix(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=os.setsid,  # Run in distinct process group
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except TimeoutError as err:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                await asyncio.sleep(0.5)
                if proc.returncode is None:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                await proc.wait()
            except (ProcessLookupError, OSError):
                pass
            raise DockerTimeoutError(
                f"Docker compose command timed out after {timeout} seconds"
            ) from err

        duration = round(time.monotonic() - start_time, 2)
        out_str = stdout_bytes.decode(errors="replace")
        err_str = stderr_bytes.decode(errors="replace")
        raw_output = f"{out_str}\n{err_str}".strip()
        redacted_output = redact_sensitive_output(raw_output)

        # Keep bounded diagnostic log (last 50 KB max)
        max_chars = 50 * 1024
        if len(redacted_output) > max_chars:
            redacted_output = "[...TRUNCATED PREVIOUS LOGS...]\n" + redacted_output[-max_chars:]

        if proc.returncode != 0:
            raise DockerBuildError(exit_code=proc.returncode or 1, error_output=redacted_output)

        # Post-deployment health verification
        health_status = await self.verify_deployment_health(repo)

        return DockerExecutionResult(
            exit_code=0,
            output=redacted_output,
            duration_seconds=duration,
            health_status=health_status,
        )

    async def verify_deployment_health(self, repo: RepositoryConfig) -> str:
        """Poll application health check or verify container running status."""
        if repo.health_check is None:
            return await self._check_container_running_status(repo)

        return await self._poll_http_health_check(repo.health_check)

    async def _poll_http_health_check(self, hc: HealthCheckConfig) -> str:
        """Poll HTTP health check endpoint until healthy or timeout."""
        url = f"http://127.0.0.1:{hc.port}{hc.endpoint}"
        deadline = time.monotonic() + hc.timeout_seconds

        async with httpx.AsyncClient(timeout=5.0) as client:
            while time.monotonic() < deadline:
                try:
                    response = await client.get(url)
                    if response.status_code == hc.expected_status:
                        return "healthy"
                except (httpx.ConnectError, httpx.TimeoutException):
                    pass
                await asyncio.sleep(hc.interval_seconds)

        raise HealthCheckFailedError(
            f"Health check failed for {url}: did not return status {hc.expected_status} "
            f"within {hc.timeout_seconds} seconds"
        )

    async def _check_container_running_status(self, repo: RepositoryConfig) -> str:
        """Fallback check when no HTTP healthcheck configured: ensure containers are running."""
        cmd = [self.docker_binary, "compose"]
        for cf in repo.compose_files:
            cmd.extend(["-f", cf])
        if repo.compose_project_name:
            cmd.extend(["-p", repo.compose_project_name])
        cmd.extend(["ps", "--status", "running", "-q"])

        with contextlib.suppress(Exception):
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=repo.path.as_posix(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0 and stdout.strip():
                return "healthy"

        return "no_health_check"
