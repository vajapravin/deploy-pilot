"""Git update and revision integrity service."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path


class GitServiceError(Exception):
    """Base exception for Git operations."""


class NotAGitRepositoryError(GitServiceError):
    """Raised when target path is not a valid Git repository."""


class DirtyWorkingTreeError(GitServiceError):
    """Raised when repository has uncommitted changes or untracked files."""

    def __init__(self, modified_files: list[str]) -> None:
        self.modified_files = modified_files
        files_str = ", ".join(modified_files[:5])
        suffix = f" (and {len(modified_files) - 5} more)" if len(modified_files) > 5 else ""
        super().__init__(
            f"Deployment aborted: working tree is dirty. Modified files: {files_str}{suffix}"
        )


class UnexpectedRemoteError(GitServiceError):
    """Raised when configured remote does not match actual Git origin."""


class CommitNotReachableError(GitServiceError):
    """Raised when requested commit is not part of the target branch history."""


class GitTimeoutError(GitServiceError):
    """Raised when Git command exceeds bounded timeout."""


class GitCommandError(GitServiceError):
    """Raised when Git command exits with non-zero status."""

    def __init__(self, command: list[str], exit_code: int, stderr: str) -> None:
        self.command = command
        self.exit_code = exit_code
        self.stderr = stderr.strip()
        super().__init__(
            f"Git command '{' '.join(command)}' failed with exit code {exit_code}: {self.stderr}"
        )


class GitService:
    """Service providing safe, non-destructive Git operations with bounded timeouts."""

    def __init__(self, default_timeout_seconds: int = 60) -> None:
        self.default_timeout_seconds = default_timeout_seconds

    async def _run_git_command(
        self,
        repo_path: Path,
        args: list[str],
        timeout_seconds: float | None = None,
    ) -> str:
        """Run a git command in the repository path within its own process group."""
        timeout = self.default_timeout_seconds if timeout_seconds is None else timeout_seconds
        cmd = ["git", "-c", "safe.directory=*", "-C", repo_path.as_posix(), *args]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=os.setsid,  # Create new process group for clean signal handling
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except TimeoutError as err:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                await asyncio.sleep(0.1)
                if proc.returncode is None:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                await proc.wait()
            except (ProcessLookupError, OSError):
                pass
            raise GitTimeoutError(
                f"Git command '{' '.join(cmd)}' timed out after {timeout} seconds"
            ) from err

        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            raise GitCommandError(command=cmd, exit_code=proc.returncode or 1, stderr=stderr)

        return stdout

    async def verify_repository(self, repo_path: Path) -> None:
        """Verify that repo_path exists and is a valid Git working directory."""
        if not repo_path.is_dir():
            raise NotAGitRepositoryError(f"Repository directory does not exist: {repo_path}")

        try:
            res = await self._run_git_command(repo_path, ["rev-parse", "--is-inside-work-tree"])
            if res != "true":
                raise NotAGitRepositoryError(f"Path is not a Git repository: {repo_path}")
        except GitCommandError as err:
            raise NotAGitRepositoryError(f"Path is not a Git repository: {repo_path}") from err

    async def check_clean_working_tree(self, repo_path: Path) -> None:
        """Verify working tree is clean.

        Raises DirtyWorkingTreeError if any uncommitted changes or untracked files exist.
        """
        output = await self._run_git_command(repo_path, ["status", "--porcelain"])
        if output:
            lines = [line.strip() for line in output.splitlines() if line.strip()]
            raise DirtyWorkingTreeError(lines)

    async def verify_remote(self, repo_path: Path, expected_remote: str) -> None:
        """Verify repository origin remote matches expected remote."""
        try:
            actual_remote = await self._run_git_command(repo_path, ["remote", "get-url", "origin"])
        except GitCommandError as err:
            raise UnexpectedRemoteError(
                f"Failed to get remote 'origin' in {repo_path}: {err}"
            ) from err

        def normalize(url: str) -> str:
            u = url.strip()
            return u[:-4] if u.endswith(".git") else u

        if normalize(actual_remote) != normalize(expected_remote):
            raise UnexpectedRemoteError(
                f"Repository remote mismatch in {repo_path}. "
                f"Expected '{expected_remote}', found '{actual_remote}'"
            )

    async def fetch_branch(
        self,
        repo_path: Path,
        branch: str,
        timeout_seconds: float | None = None,
    ) -> None:
        """Fetch the latest revision of the configured branch from origin."""
        await self._run_git_command(
            repo_path,
            ["fetch", "--depth=100", "origin", branch],
            timeout_seconds=timeout_seconds,
        )

    async def verify_commit_in_branch(
        self,
        repo_path: Path,
        branch: str,
        commit_sha: str,
    ) -> str:
        """Verify that commit_sha exists and is reachable in origin/branch history.

        Returns the verified full 40-character commit SHA.
        """
        # Resolve requested commit
        try:
            resolved_sha = await self._run_git_command(
                repo_path,
                ["rev-parse", "--verify", f"{commit_sha}^{{commit}}"],
            )
        except GitCommandError as err:
            raise CommitNotReachableError(
                f"Commit '{commit_sha}' cannot be resolved in {repo_path}"
            ) from err

        # Verify ancestor relationship with origin/<branch>
        try:
            await self._run_git_command(
                repo_path,
                ["merge-base", "--is-ancestor", resolved_sha, f"origin/{branch}"],
            )
        except GitCommandError as err:
            raise CommitNotReachableError(
                f"Commit '{resolved_sha}' is not an ancestor of origin/{branch}"
            ) from err

        return resolved_sha

    async def get_current_head(self, repo_path: Path) -> str:
        """Return the current 40-character HEAD commit SHA."""
        return await self._run_git_command(repo_path, ["rev-parse", "HEAD"])

    async def checkout_and_fast_forward(
        self,
        repo_path: Path,
        branch: str,
        target_commit_sha: str,
    ) -> str:
        """Safely checkout branch and fast-forward to target commit.

        Never runs destructive commands. Guarantees working tree remains clean.
        Returns the final deployed commit SHA.
        """
        # 1. Pre-flight check: tree must be clean
        await self.check_clean_working_tree(repo_path)

        # 2. Switch to branch
        await self._run_git_command(repo_path, ["checkout", branch])

        # 3. Fast-forward merge origin/branch
        await self._run_git_command(
            repo_path,
            ["merge", "--ff-only", f"origin/{branch}"],
        )

        # 4. Post-flight check: tree must still be clean
        await self.check_clean_working_tree(repo_path)

        # 5. Verify HEAD matches expected commit
        deployed_sha = await self.get_current_head(repo_path)
        if deployed_sha != target_commit_sha:
            err_msg = (
                f"Deployed commit '{deployed_sha}' does not match "
                f"expected target '{target_commit_sha}'"
            )
            raise GitServiceError(err_msg)

        return deployed_sha
